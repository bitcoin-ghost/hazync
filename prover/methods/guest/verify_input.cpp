// Verify one transaction input using Bitcoin Core's REAL VerifyScript + interpreter + sighash.
// General form: legacy, segwit v0 (BIP143), and taproot (BIP341) — the taproot sighash needs all
// spent outputs, supplied via PrecomputedTransactionData.
#include <primitives/transaction.h>
#include <script/interpreter.h>
#include <script/script.h>
#include <span.h>
#include <vector>
#include <cstring>
#include <cstdint>
#include <algorithm>
#include <secp256k1.h>
#include <crypto/sha256.h>
#include <consensus/tx_check.h>
#include <consensus/validation.h>
#include <consensus/amount.h>
#include <consensus/merkle.h>
#include <arith_uint256.h>
#include <hash.h>
#include <uint256.h>
#include <pubkey.h>   // hazync#139 differential: CPubKey::Verify is the authority side
#include <chain.h>              // real CBlockIndex (feeds the retarget)
#include <pow.h>                // real CalculateNextWorkRequired
#include <consensus/params.h>   // Consensus::Params (mainnet PoW parameters)
#include <consensus/consensus.h> // MAX_BLOCK_WEIGHT / MAX_BLOCK_SIGOPS_COST / WITNESS_SCALE_FACTOR
#include <kernel/chainparams.h> // real mainnet CChainParams::Main() (authoritative consensus constants)
#include <memory>
#include <string>

// Minimal byte reader satisfying the Stream interface Core's Unserialize needs (no streams.h).
struct MiniReader {
    const std::byte* p;
    const std::byte* e;
    // Fail closed (trap -> guest abort -> no proof) on any read/skip past the buffer end, so a truncated
    // or malformed blob can never OOB-read adjacent guest heap (the zkVM has no memory protection). Every
    // consensus value is recomputed + PoW-bound, so this is robustness, not a soundness fix — but it turns
    // a wild read into a clean rejection.
    void read(Span<std::byte> dst) {
        if (dst.size()) {
            if (dst.size() > static_cast<size_t>(e - p)) __builtin_trap();
            std::memcpy(dst.data(), p, dst.size()); p += dst.size();
        }
    }
    void ignore(size_t n) { if (n > static_cast<size_t>(e - p)) __builtin_trap(); p += n; }
    template <typename T> MiniReader& operator>>(T&& obj) { ::Unserialize(*this, obj); return *this; }
};

static void le(unsigned char* b, uint64_t v, int n) { for (int i = 0; i < n; i++) b[i] = (unsigned char)(v >> (8 * i)); }

// Domain-separation tags — MUST equal hazync-utreexo's TAG_LEAF/TAG_NODE (accumulator/src/lib.rs) and
// the guest Rust utreexo.rs. `scripts/check-utreexo.sh` gates all three.
//
// A leaf preimage is 57 + scriptPubKey bytes, so a 7-byte scriptPubKey yields a 64-byte preimage — the
// same length an interior node hashes. Without a tag the leaf and interior domains genuinely overlap,
// and the only barrier is that a leaf preimage opens with a txid. A txid is the hash of a transaction
// an attacker can construct and grind, so that is a cost argument, not a separation. One tag byte
// makes it a property of the construction instead.
static const unsigned char TAG_LEAF = 0x00;

// Canonical Hazync UTXO-leaf commitment for the coin spent by input `input_idx`:
//   SHA256( TAG_LEAF || txid || vout || value || scriptPubKey || coin_height || is_coinbase || coin_mtp ).
// Height, coinbase flag, and creation median-time-past are committed so maturity + BIP68 (height AND
// time) checks can't lie about the coin's age.
// Returns false without writing `out_leaf` when the witness cannot address the input; see the guard.
static bool coin_leaf(const CTransaction& tx, const std::vector<CTxOut>& spent, unsigned input_idx,
                      uint32_t coin_height, uint32_t coin_is_coinbase, uint32_t coin_mtp, uint8_t* out_leaf) {
    // The one unguarded index in this file until audit #5 (L-1). Every sibling entry point validates
    // the prevouts vector before indexing it — verify_input (-60), check_input_locks (-43),
    // tx_full_sigops (poison cost), tx_input_prevout_txid (0) — and this did not, so a witness whose
    // prevouts are shorter than vin produced a wild read. In a zkVM there is no memory protection to
    // turn that into a fault; it silently reads whatever is adjacent.
    //
    // No exposure was ever demonstrated: check_tx's -24 rejects the same tx in the same loop, and a
    // leaf built from garbage fails the accumulator delete regardless — every path already ended in a
    // reject. Fixed anyway, because "unreachable" here rests on the ORDER of two checks in a different
    // function, and that is a property of today's call sites rather than of this code.
    if (input_idx >= spent.size() || input_idx >= tx.vin.size()) return false;
    const COutPoint& op = tx.vin[input_idx].prevout;
    const CTxOut& coin = spent[input_idx];
    CSHA256 h;
    unsigned char b8[8];
    h.Write(&TAG_LEAF, 1);
    h.Write(reinterpret_cast<const unsigned char*>(op.hash.begin()), 32);
    le(b8, op.n, 4); h.Write(b8, 4);
    le(b8, (uint64_t)coin.nValue, 8); h.Write(b8, 8);
    // N2: length-prefix the only variable-length field so the preimage stays injective even if a future
    // change adds another variable field. MUST stay byte-identical to tx_out_leaves + the host coin_leaf.
    le(b8, coin.scriptPubKey.size(), 4); h.Write(b8, 4);
    h.Write(reinterpret_cast<const unsigned char*>(coin.scriptPubKey.data()), coin.scriptPubKey.size());
    le(b8, coin_height, 4); h.Write(b8, 4);
    unsigned char cb = (unsigned char)(coin_is_coinbase ? 1 : 0); h.Write(&cb, 1);
    le(b8, coin_mtp, 4); h.Write(b8, 4);
    h.Finalize(out_leaf);
    return true;
}

// Compute ONLY the coin leaf for an input (no VerifyScript) — cheap, used by the aggregation proof
// to bind each chunk's committed leaf to the right input without re-verifying the (expensive) script.
extern "C" void coin_leaf_only(const uint8_t* tx_bytes, unsigned tx_len, unsigned input_idx,
                               const uint8_t* prevouts, unsigned prevouts_len,
                               uint32_t coin_height, uint32_t coin_is_coinbase, uint32_t coin_mtp,
                               uint8_t* out_leaf) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx; r >> TX_WITH_WITNESS(mtx); CTransaction tx{mtx};
    MiniReader pr{reinterpret_cast<const std::byte*>(prevouts),
                  reinterpret_cast<const std::byte*>(prevouts) + prevouts_len};
    std::vector<CTxOut> spent; pr >> spent;
    // On an unaddressable input, ZERO the leaf rather than leaving the caller's buffer untouched.
    // Returning without writing would hand back whatever happened to be in that memory — a
    // non-deterministic result from a deterministic function, and the guest's whole contract is
    // determinism. Zero is a value no real coin hashes to, so it fails the accumulator delete.
    if (!coin_leaf(tx, spent, input_idx, coin_height, coin_is_coinbase, coin_mtp, out_leaf)) {
        for (int i = 0; i < 32; ++i) out_leaf[i] = 0;
    }
}

// Recompute the UTXO leaves a transaction CREATES — one per SPENDABLE output — so the guest can derive
// the block's output set from the real tx bytes instead of trusting a host-supplied list (soundness),
// and identify in-block-created coins. Provably-unspendable outputs (Core CScript::IsUnspendable():
// OP_RETURN, or script > MAX_SCRIPT_SIZE) never enter the UTXO set and are skipped. Each leaf uses the
// SAME commitment as a spent coin, so a created-output leaf equals the leaf later presented to spend it:
//   SHA256( txid || vout || value || scriptPubKey || height || is_coinbase || block_time ).
// Writes n*32 leaf bytes into `out` and the tx's txid (32 bytes) into `out_txid`; returns n.
extern "C" uint32_t tx_out_leaves(const uint8_t* tx_bytes, unsigned tx_len,
                                  uint32_t height, uint32_t is_coinbase, uint32_t block_time,
                                  uint8_t* out, uint8_t* out_txid) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx; r >> TX_WITH_WITNESS(mtx); CTransaction tx{mtx};
    const uint256 txid = tx.GetHash();
    std::memcpy(out_txid, txid.begin(), 32);
    uint32_t n = 0;
    unsigned char b8[8];
    for (uint32_t v = 0; v < tx.vout.size(); v++) {
        const CTxOut& o = tx.vout[v];
        if (o.scriptPubKey.IsUnspendable()) continue; // not part of the UTXO set (H3)
        CSHA256 h;
        h.Write(&TAG_LEAF, 1);
        h.Write(reinterpret_cast<const unsigned char*>(txid.begin()), 32);
        le(b8, v, 4); h.Write(b8, 4);
        le(b8, (uint64_t)o.nValue, 8); h.Write(b8, 8);
        // N2: length-prefix scriptPubKey (see coin_leaf) — keep byte-identical to the spend-side leaf.
        le(b8, o.scriptPubKey.size(), 4); h.Write(b8, 4);
        h.Write(reinterpret_cast<const unsigned char*>(o.scriptPubKey.data()), o.scriptPubKey.size());
        le(b8, height, 4); h.Write(b8, 4);
        unsigned char cb = (unsigned char)(is_coinbase ? 1 : 0); h.Write(&cb, 1);
        le(b8, block_time, 4); h.Write(b8, 4);
        h.Finalize(out + (size_t)n * 32);
        n++;
    }
    return n;
}

// Recompute a tx's BIP141 wtxid and whether it carries witness data, from the REAL tx bytes — so the
// guest derives has_witness + the witness merkle leaves itself instead of trusting a host-supplied
// wtxid list (SEC-1). Uses Core's own GetWitnessHash()/HasWitness(). Non-witness tx: wtxid == txid.
extern "C" uint32_t tx_wtxid_info(const uint8_t* tx_bytes, unsigned tx_len, uint8_t* out_wtxid) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx; r >> TX_WITH_WITNESS(mtx); CTransaction tx{mtx};
    const uint256 w = tx.GetWitnessHash();
    std::memcpy(out_wtxid, w.begin(), 32);
    return tx.HasWitness() ? 1u : 0u;
}

// Absolute locktime finality — exact Core IsFinalTx (consensus/tx_verify.cpp).
extern "C" int is_final_tx(const uint8_t* tx_bytes, unsigned tx_len, int64_t height, int64_t block_time) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    if (mtx.nLockTime == 0) return 1;
    const int64_t LOCKTIME_THRESHOLD = 500000000;
    int64_t thr = ((int64_t)mtx.nLockTime < LOCKTIME_THRESHOLD) ? height : block_time;
    if ((int64_t)mtx.nLockTime < thr) return 1;
    for (const auto& in : mtx.vin)
        if (in.nSequence != 0xffffffffu) return 0; // not final
    return 1;
}

// Coinbase maturity (100 blocks) + BIP68 relative locktime (height AND time based) for one input.
// The coin's height/coinbase/creation-MTP are leaf-committed (unforgeable); `spend_mtp` is the
// current block's median-time-past.
extern "C" int check_input_locks(const uint8_t* tx_bytes, unsigned tx_len, unsigned input_idx,
                                 uint32_t coin_height, uint32_t coin_is_coinbase, uint32_t coin_mtp,
                                 uint32_t spend_height, uint32_t spend_mtp) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    // Fail closed on an out-of-range input index (mirrors verify_input's -60 guard) — otherwise a
    // malicious witness triggers an OOB std::vector access below rather than a clean rejection.
    if (input_idx >= mtx.vin.size()) return -43;
    // Coinbase maturity: a coinbase output is unspendable for COINBASE_MATURITY (100) blocks.
    if (coin_is_coinbase && spend_height < coin_height + 100) return -40;
    // BIP68 relative locktime — only ENFORCED once CSV is active (Core sets LOCKTIME_VERIFY_SEQUENCE
    // from CSVHeight 419328; below that CalculateSequenceLocks imposes no constraint), and only for tx
    // version >= 2 with the disable bit clear. Gating on spend_height matches Core; without it the guest
    // rejects pre-CSV v2 txs with unmet relative locks that Core accepts.
    uint32_t seq = mtx.vin[input_idx].nSequence;
    const uint32_t DISABLE = 1u << 31, TYPE = 1u << 22, MASK = 0x0000ffff, GRANULARITY = 9;
    if (spend_height >= 419328 && mtx.version >= 2 && !(seq & DISABLE)) {
        if (seq & TYPE) {
            // Time-based: coin's creation MTP + (value << 9) seconds must have elapsed by this block's MTP.
            uint64_t required = (uint64_t)coin_mtp + (((uint64_t)(seq & MASK)) << GRANULARITY);
            if ((uint64_t)spend_mtp < required) return -42;
        } else {
            uint32_t required = coin_height + (seq & MASK);
            if (spend_height < required) return -41;
        }
    }
    return 1;
}

// Per-tx consensus checks with real Core code: structural (CheckTransaction) + the no-inflation
// amount rules (all values in MoneyRange, and for non-coinbase Σinputs ≥ Σoutputs so fee ≥ 0).
// `prevouts` is the full spent-outputs vector for the tx (all inputs). Returns 1 valid, else a
// negative code; `out_fee` gets Σin−Σout (0 for coinbase, handled at block level).
extern "C" int check_tx(const uint8_t* tx_bytes, unsigned tx_len,
                        const uint8_t* prevouts, unsigned prevouts_len,
                        int64_t* out_fee) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};
    MiniReader pr{reinterpret_cast<const std::byte*>(prevouts),
                  reinterpret_cast<const std::byte*>(prevouts) + prevouts_len};
    std::vector<CTxOut> spent;
    pr >> spent;
    if (!tx.IsCoinBase() && spent.size() != tx.vin.size()) return -24; // SEC-3: prevouts must match inputs

    TxValidationState state;
    if (!CheckTransaction(tx, state)) return -20; // structural consensus failure

    CAmount sum_out = 0;
    for (const auto& o : tx.vout) sum_out += o.nValue; // per-output ranges checked in CheckTransaction
    CAmount sum_in = 0;
    for (const auto& c : spent) {
        if (c.nValue < 0 || c.nValue > MAX_MONEY) return -21;
        sum_in += c.nValue;
        if (!MoneyRange(sum_in)) return -22;
    }
    if (!tx.IsCoinBase()) {
        if (sum_in < sum_out) return -23; // negative fee = inflation
        if (out_fee) *out_fee = sum_in - sum_out;
    } else if (out_fee) {
        *out_fee = 0; // coinbase: value bound is subsidy+fees, enforced at block level
    }
    return 1;
}

// Header proof-of-work: mirrors Core's CheckProofOfWorkImpl (pow.cpp) with mainnet powLimit
// (== SetCompact(0x1d00ffff)). Real arith_uint256 SetCompact + comparison. header = 80 bytes.
extern "C" int check_pow(const uint8_t* header) {
    unsigned char h1[32], h2[32];
    CSHA256().Write(header, 80).Finalize(h1);
    CSHA256().Write(h1, 32).Finalize(h2); // double-SHA256 block hash
    uint256 hash;
    std::memcpy(hash.begin(), h2, 32);
    uint32_t nBits = (uint32_t)header[72] | ((uint32_t)header[73] << 8) |
                     ((uint32_t)header[74] << 16) | ((uint32_t)header[75] << 24);
    bool neg, over, n2, o2;
    arith_uint256 target, powLimit;
    target.SetCompact(nBits, &neg, &over);
    powLimit.SetCompact(0x1d00ffff, &n2, &o2); // mainnet consensus powLimit
    if (neg || over || target == 0 || target > powLimit) return -30;
    if (UintToArith256(hash) > target) return -31;
    return 1;
}

// BIP141 witness commitment: the coinbase must commit to the witness merkle root. `wtxids` are the
// block's wtxids in order (coinbase wtxid = all-zero per BIP141). Returns 1 valid; if no commitment
// output is present, 1 only when the block has no witness data (has_witness==0), else negative.
extern "C" int check_witness_commitment(const uint8_t* cb, unsigned cb_len,
                                        const uint8_t* wtxids, uint32_t n, uint32_t has_witness) {
    MiniReader r{reinterpret_cast<const std::byte*>(cb), reinterpret_cast<const std::byte*>(cb) + cb_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};
    // Find the commitment output: LAST output with scriptPubKey >=38 bytes starting 6a24aa21a9ed.
    int found = -1;
    for (size_t i = 0; i < tx.vout.size(); i++) {
        const CScript& s = tx.vout[i].scriptPubKey;
        if (s.size() >= 38 && s[0] == 0x6a && s[1] == 0x24 &&
            s[2] == 0xaa && s[3] == 0x21 && s[4] == 0xa9 && s[5] == 0xed) found = (int)i;
    }
    if (found < 0) return has_witness ? -1 : 1; // segwit block with witness MUST carry a commitment
    // reserved value = the coinbase input's single 32-byte witness element. Guard the malformed empty-vin
    // case: a 0-input "coinbase" parses fine (CompactSize 0) but would read tx.vin[0] out of bounds — UB in a
    // zkVM with no memory protection. Such a block is rejected anyway (is_coinbase / CheckTransaction
    // bad-txns-vin-empty), but never compute a consensus flag from a wild read.
    if (tx.vin.empty()) return -2;
    const auto& stack = tx.vin[0].scriptWitness.stack;
    if (stack.size() != 1 || stack[0].size() != 32) return -2;
    // witness merkle root over the wtxids.
    std::vector<uint256> h(n);
    for (uint32_t i = 0; i < n; i++) std::memcpy(h[i].begin(), wtxids + 32 * i, 32);
    uint256 wroot = ComputeMerkleRoot(std::move(h), nullptr);
    // expected commitment = SHA256d( wroot || reserved ).
    unsigned char h1[32], h2[32];
    CSHA256 s1;
    s1.Write(reinterpret_cast<const unsigned char*>(wroot.begin()), 32);
    s1.Write(stack[0].data(), 32);
    s1.Finalize(h1);
    CSHA256().Write(h1, 32).Finalize(h2);
    const CScript& cs = tx.vout[found].scriptPubKey;
    return std::memcmp(&cs[6], h2, 32) == 0 ? 1 : -3;
}

// BIP34: from height 227931 the coinbase scriptSig must begin with a push of the block height.
// Compared against Core's own `CScript() << height` serialization (minimal push). 1 valid.
// Defined further down (it builds CChainParams once); declared here so the BIP34 gate can read the
// buried height out of Core rather than repeating it as a literal.
static const Consensus::Params& mainnet_params();

extern "C" int check_bip34(const uint8_t* cb, unsigned cb_len, uint32_t height) {
    // Read from Core's own compiled Consensus::Params rather than typed here — see core_bip34_height.
    if (height < (uint32_t)mainnet_params().BIP34Height) return 1;
    MiniReader r{reinterpret_cast<const std::byte*>(cb), reinterpret_cast<const std::byte*>(cb) + cb_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CScript expect = CScript() << (int64_t)height;
    if (mtx.vin.empty()) return -50; // empty-vin "coinbase": rejected elsewhere; don't read vin[0] out of bounds
    const CScript& ss = mtx.vin[0].scriptSig;
    if (ss.size() < expect.size()) return -50;
    if (!std::equal(expect.begin(), expect.end(), ss.begin())) return -51;
    return 1;
}

// Merkle root over the block's txids (internal byte order), via real Core ComputeMerkleRoot.
extern "C" void merkle_root(const uint8_t* txids, uint32_t n, uint8_t* out_root, uint8_t* out_mutated) {
    std::vector<uint256> hashes(n);
    for (uint32_t i = 0; i < n; i++) {
        std::memcpy(hashes[i].begin(), txids + 32 * i, 32);
    }
    bool mutated = false;
    uint256 r = ComputeMerkleRoot(std::move(hashes), &mutated); // mutated = CVE-2012-2459 malleability
    std::memcpy(out_root, r.begin(), 32);
    *out_mutated = mutated ? 1 : 0;
}

// Sum of a coinbase tx's output values (for the subsidy bound).
extern "C" int64_t coinbase_value(const uint8_t* tx_bytes, unsigned tx_len) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    // Detect the overflow instead of risking it (audit #5, N-2). Signed overflow is UB in C++, and
    // while Core's CheckTransaction independently rejects any tx whose output total leaves MoneyRange —
    // so no overflowed value has ever reached a decision — "unreachable" there is a property of another
    // function running first, not of this one.
    //
    // NOT __int128: the guest is a 32-BIT riscv target (rv32im) and GCC does not provide __int128
    // there. The first attempt at this fix used it and died with "expected primary-expression before
    // '__int128'" — inside the container, after the whole dependency tree had built, which is a slow
    // way to learn that host intuitions about integer widths do not survive the target change.
    //
    // __builtin_add_overflow is exact on any width, has no UB, and says what it means.
    int64_t s = 0;
    for (const auto& o : mtx.vout) {
        const int64_t v = static_cast<int64_t>(o.nValue);
        // Saturate on overflow, in the direction it overflowed: |s| <= INT64_MAX going in, so the sign
        // of the addend is the direction. A total outside int64 is nonsense the caller must reject, and
        // both saturations are how it reads it — validate_block requires coinbase_val >= 0 AND
        // coinbase_val <= subsidy + total_fee, so INT64_MIN fails the first and INT64_MAX the second.
        if (__builtin_add_overflow(s, v, &s)) {
            return v < 0 ? INT64_MIN : INT64_MAX;
        }
    }
    return s;
}

// Real Core CTransaction::IsCoinBase() on raw bytes: 1 iff exactly one input with a null prevout.
// (#4) Used so validate_block can assert the block's declared coinbase really is structurally a
// coinbase before trusting it for the subsidy/BIP34/witness-commitment checks.
extern "C" int is_coinbase_tx(const uint8_t* tx_bytes, unsigned tx_len) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};
    return tx.IsCoinBase() ? 1 : 0;
}

// Number of inputs (vin) of a transaction from its raw bytes — used to require exactly one
// accumulator-authenticated BlockInput per real input, so the host cannot pad the fee/sigop prevouts
// blob with phantom coins or omit an input.
extern "C" uint32_t tx_vin_count(const uint8_t* tx_bytes, unsigned tx_len) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    return (uint32_t)mtx.vin.size();
}

// The txid an input spends (prevout.hash), in internal byte order — hazync#54.
//
// The guest needs this to run the BIP30 transition itself rather than being told which coinbases a
// block spends. `coin_is_coinbase` is already leaf-committed and so cannot be lied about; this
// supplies the other half, the identity of the coinbase being spent, read out of the SAME
// Core-deserialised transaction the script verification runs against. Taking it from the witness
// instead would let a prover name a coinbase the block never touched and decrement it to zero,
// manufacturing a free slot for a later duplicate — which is the whole attack the SMT exists to stop.
//
// Returns 0 and writes nothing if `input_idx` is out of range; the caller treats that as invalid.
extern "C" int tx_input_prevout_txid(const uint8_t* tx_bytes, unsigned tx_len,
                                     uint32_t input_idx, uint8_t* out_txid) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    if (input_idx >= mtx.vin.size()) return 0;
    const uint256 h = mtx.vin[input_idx].prevout.hash;
    std::memcpy(out_txid, h.begin(), 32);
    return 1;
}

// Per-tx weight + legacy sigop cost (real Core: GetSerializeSize + CScript::GetSigOpCount).
// weight = base_size*(WITNESS_SCALE_FACTOR-1) + total_size; sigop cost = legacy count * WITNESS_SCALE_FACTOR.
extern "C" void tx_wu_sigops(const uint8_t* tx_bytes, unsigned tx_len, int64_t* out_weight, int64_t* out_sigops) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};
    int64_t base = (int64_t)::GetSerializeSize(TX_NO_WITNESS(tx));
    int64_t total = (int64_t)::GetSerializeSize(TX_WITH_WITNESS(tx));
    *out_weight = base * (WITNESS_SCALE_FACTOR - 1) + total;
    int64_t sigops = 0;
    for (const auto& in : tx.vin) sigops += in.scriptSig.GetSigOpCount(false);
    for (const auto& o : tx.vout) sigops += o.scriptPubKey.GetSigOpCount(false);
    *out_sigops = sigops * WITNESS_SCALE_FACTOR; // legacy sigop cost
}

// Full sigop cost for one tx (real Core GetTransactionSigOpCost logic): legacy*4, plus — when the
// deployment is active in `flags` — P2SH sigops and witness sigops, using the spent coins.
extern "C" int64_t tx_full_sigops(const uint8_t* tx_bytes, unsigned tx_len,
                                  const uint8_t* prevouts, unsigned prevouts_len, unsigned flags) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};
    int64_t legacy = 0;
    for (const auto& in : tx.vin) legacy += in.scriptSig.GetSigOpCount(false);
    for (const auto& o : tx.vout) legacy += o.scriptPubKey.GetSigOpCount(false);
    int64_t cost = legacy * WITNESS_SCALE_FACTOR;
    if (tx.IsCoinBase() || prevouts_len == 0) return cost;

    MiniReader pr{reinterpret_cast<const std::byte*>(prevouts),
                  reinterpret_cast<const std::byte*>(prevouts) + prevouts_len};
    std::vector<CTxOut> spent;
    pr >> spent;
    // N3: a non-coinbase tx with fewer spent coins than inputs is malformed. Fail CLOSED with a poison
    // sigop cost (>> MAX_BLOCK_SIGOPS_COST, but small enough that summing across a block can't overflow
    // i64) so sigops_ok becomes false — instead of silently returning the legacy-only cost and leaning on
    // verify_input's independent -60 rejection. (The coinbase / prevouts_len==0 case returned above.)
    if (spent.size() < tx.vin.size()) return 1LL << 40;
    for (size_t i = 0; i < tx.vin.size(); i++) {
        const CScript& spk = spent[i].scriptPubKey;
        // Match Core's GetP2SHSigOpCount: the redeemScript sigops are added ONLY for P2SH prevouts.
        // Without the IsPayToScriptHash() guard, spk.GetSigOpCount(scriptSig) falls through to
        // GetSigOpCount(true) for a non-P2SH prevout — the scriptPubKey's own sigops, which Core never
        // folds into the block total — over-counting every legacy input and rejecting valid blocks.
        if ((flags & SCRIPT_VERIFY_P2SH) && spk.IsPayToScriptHash()) {
            cost += spk.GetSigOpCount(tx.vin[i].scriptSig) * WITNESS_SCALE_FACTOR; // P2SH redeemScript
        }
        cost += CountWitnessSigOps(tx.vin[i].scriptSig, spk, &tx.vin[i].scriptWitness, flags);
    }
    return cost;
}

// kernel/cs_main.h declares `extern RecursiveMutex cs_main;` (pulled in via chain.h). The guest is
// single-threaded and never locks it (coreshim/sync.h makes LOCK/AssertLockHeld no-ops), but Core's
// ODR wants exactly one definition — supply it here. It is never read or written.
RecursiveMutex cs_main;

// chain.cpp's CBlockIndex::ToString / CBlockFileInfo::ToString reference this ISO-8601 log formatter
// (from util/time.cpp, which we do not compile — it is diagnostic output, not consensus). The guest
// never calls ToString; provide a trivial definition so the link resolves. Non-consensus glue only.
std::string FormatISO8601Date(int64_t) { return std::string(); }

// The authoritative mainnet consensus parameters, sourced from Bitcoin Core's OWN chainparams.cpp
// (compiled into the guest). CChainParams::Main() builds the real CMainParams — powLimit,
// nPowTargetTimespan/Spacing, the buried soft-fork heights, the halving interval — and asserts the
// genesis hash. Constructed once (function-static) and reused; every consensus constant below comes
// from here rather than a hand-typed literal, so there is nothing to mis-transcribe.
static const Consensus::Params& mainnet_params() {
    static const std::unique_ptr<const CChainParams> params = CChainParams::Main();
    return params->GetConsensus();
}

// Difficulty retarget: the expected nBits for the block after `prev_bits`, given the epoch's first
// block time and the last block's time. This drives Bitcoin Core's REAL, unmodified
// CalculateNextWorkRequired (compiled from src/pow.cpp), fed the real mainnet Consensus::Params from
// Core's own chainparams.cpp — the retarget math AND its parameters are Core's own, not a transcription.
extern "C" uint32_t calc_next_bits(uint32_t prev_bits, int64_t first_time, int64_t last_time) {
    CBlockIndex pindexLast;
    pindexLast.nBits   = prev_bits;
    pindexLast.nTime   = (uint32_t)last_time;
    pindexLast.nHeight = 0;  // unused on the mainnet retarget path (only the BIP94/min-difficulty
                             // branches read it, and both are disabled for mainnet params)
    return CalculateNextWorkRequired(&pindexLast, first_time, mainnet_params());
}

// Cumulative chainwork: cum += GetBlockProof(nBits) (real Core formula, chain.cpp), 256-bit.
// `cum` is an opaque 32-byte accumulator (uint256 internal order).
extern "C" void add_work(uint8_t* cum, uint32_t nBits) {
    uint256 u;
    std::memcpy(u.begin(), cum, 32);
    arith_uint256 c = UintToArith256(u);
    bool neg, over;
    arith_uint256 t;
    t.SetCompact(nBits, &neg, &over);
    arith_uint256 work = (neg || over || t == 0) ? arith_uint256(0) : ((~t / (t + 1)) + 1);
    c += work;
    uint256 r = ArithToUint256(c);
    std::memcpy(cum, r.begin(), 32);
}

// Block subsidy — Core's GetBlockSubsidy formula (the 6-line body lives in validation.cpp, which is
// un-carvable; this is that body verbatim). The one consensus constant, the halving interval, is read
// from Core's chainparams (mainnet_params().nSubsidyHalvingInterval) rather than hard-typed.
extern "C" int64_t block_subsidy(uint32_t height) {
    int halvings = height / mainnet_params().nSubsidyHalvingInterval;
    if (halvings >= 64) return 0;
    int64_t subsidy = 50LL * 100000000LL; // 50 * COIN
    subsidy >>= halvings;
    return subsidy;
}

// Consensus constants sourced from Core's own compiled source, exposed to the Rust guest so it can pin
// every hard-coded literal to Core's value at runtime (a mismatch aborts the proof). Heights + retarget
// interval come from chainparams (mainnet_params()); the weight/sigop limits from consensus/consensus.h;
// the SCRIPT_VERIFY_* bit positions from script/interpreter.h — so nothing consensus-relevant is a
// magic number we could have typed wrong.
extern "C" uint32_t core_bip66_height()            { return (uint32_t)mainnet_params().BIP66Height; }
extern "C" uint32_t core_bip65_height()            { return (uint32_t)mainnet_params().BIP65Height; }
extern "C" uint32_t core_csv_height()              { return (uint32_t)mainnet_params().CSVHeight; }
extern "C" uint32_t core_segwit_height()           { return (uint32_t)mainnet_params().SegwitHeight; }
// BIP34Height was the ONE buried height still hand-typed (audit #3 phase-2 sweep). Its four siblings
// were already read from Core; this one was a literal in two places, so the claim that nothing
// consensus-relevant is a hand-typed magic number did not hold for it.
extern "C" uint32_t core_bip34_height()            { return (uint32_t)mainnet_params().BIP34Height; }
extern "C" uint32_t core_retarget_interval()       { return (uint32_t)mainnet_params().DifficultyAdjustmentInterval(); }
extern "C" uint32_t core_subsidy_halving_interval(){ return (uint32_t)mainnet_params().nSubsidyHalvingInterval; }
extern "C" int64_t  core_max_block_weight()        { return (int64_t)MAX_BLOCK_WEIGHT; }
extern "C" int64_t  core_max_block_sigops_cost()   { return (int64_t)MAX_BLOCK_SIGOPS_COST; }
extern "C" int64_t  core_witness_scale_factor()    { return (int64_t)WITNESS_SCALE_FACTOR; }
extern "C" uint32_t core_flag_p2sh()               { return (uint32_t)SCRIPT_VERIFY_P2SH; }
extern "C" uint32_t core_flag_dersig()             { return (uint32_t)SCRIPT_VERIFY_DERSIG; }
extern "C" uint32_t core_flag_nulldummy()          { return (uint32_t)SCRIPT_VERIFY_NULLDUMMY; }
extern "C" uint32_t core_flag_cltv()               { return (uint32_t)SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY; }
extern "C" uint32_t core_flag_csv()                { return (uint32_t)SCRIPT_VERIFY_CHECKSEQUENCEVERIFY; }
extern "C" uint32_t core_flag_witness()            { return (uint32_t)SCRIPT_VERIFY_WITNESS; }
extern "C" uint32_t core_flag_taproot()            { return (uint32_t)SCRIPT_VERIFY_TAPROOT; }

// #135: verify SEVERAL of one transaction's inputs in a single call, so the transaction is
// deserialised once and PrecomputedTransactionData::Init runs once — which is what BIP143's
// precomputation is FOR. Calling verify_input per input repeated both for every input of the
// transaction: measured at 16.6M (deserialise) + 6.5M (Init) cycles on a 42-input chunk of block
// 741000, against 83.6M of actual VerifyScript.
//
// `out_results[k]` receives exactly what verify_input would have returned for `input_idx[k]`. The
// return value covers only the structural check that applies to the whole transaction.
extern "C" int verify_inputs_batch(const uint8_t* tx_bytes, unsigned tx_len,
                                   const uint8_t* prevouts, unsigned prevouts_len,
                                   unsigned flags, unsigned n,
                                   const uint32_t* input_idx,
                                   const uint32_t* coin_height,
                                   const uint32_t* coin_is_coinbase,
                                   const uint32_t* coin_mtp,
                                   int32_t* out_results,
                                   uint8_t* out_leaves /* 32*n bytes, may be null */) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};

    MiniReader pr{reinterpret_cast<const std::byte*>(prevouts),
                  reinterpret_cast<const std::byte*>(prevouts) + prevouts_len};
    std::vector<CTxOut> spent;
    pr >> spent;
    if (spent.size() != tx.vin.size()) return -60; // SEC-3: prevouts must match inputs

    // Once per transaction, not once per input.
    PrecomputedTransactionData txdata;
    txdata.Init(tx, std::vector<CTxOut>(spent), true);

    for (unsigned k = 0; k < n; k++) {
        const unsigned i = input_idx[k];
        if (i >= spent.size()) { out_results[k] = -60; continue; } // SEC-3, per input as before
        if (out_leaves) coin_leaf(tx, spent, i, coin_height[k], coin_is_coinbase[k], coin_mtp[k],
                                  out_leaves + 32 * k);
        const CTxIn& in = tx.vin[i];
        TransactionSignatureChecker checker(&tx, i, spent[i].nValue, txdata, MissingDataBehavior::FAIL);
        ScriptError err = SCRIPT_ERR_OK;
        bool ok = VerifyScript(in.scriptSig, spent[i].scriptPubKey, &in.scriptWitness, flags,
                               checker, &err);
        out_results[k] = ok ? 1 : -(int)err - 1;
    }
    return 1;
}

extern "C" int verify_input(const uint8_t* tx_bytes, unsigned tx_len,
                            unsigned input_idx,
                            const uint8_t* prevouts, unsigned prevouts_len,
                            unsigned flags,
                            uint32_t coin_height, uint32_t coin_is_coinbase, uint32_t coin_mtp,
                            uint8_t* out_leaf /* 32 bytes */) {
    MiniReader r{reinterpret_cast<const std::byte*>(tx_bytes),
                 reinterpret_cast<const std::byte*>(tx_bytes) + tx_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CTransaction tx{mtx};

    // Spent outputs (Core vector<CTxOut> serialization): value(8) + scriptPubKey per input.
    MiniReader pr{reinterpret_cast<const std::byte*>(prevouts),
                  reinterpret_cast<const std::byte*>(prevouts) + prevouts_len};
    std::vector<CTxOut> spent;
    pr >> spent;
    if (input_idx >= spent.size() || spent.size() != tx.vin.size()) return -60; // SEC-3: prevouts must match inputs

    // Canonical leaf of the coin being spent (binds VerifyScript's coin + its height/coinbase flag).
    if (out_leaf) coin_leaf(tx, spent, input_idx, coin_height, coin_is_coinbase, coin_mtp, out_leaf);

    // Precompute BIP143/BIP341 hashes with the spent outputs (needed for segwit + taproot).
    PrecomputedTransactionData txdata;
    txdata.Init(tx, std::vector<CTxOut>(spent), true);


    const CTxIn& in = tx.vin[input_idx];
    TransactionSignatureChecker checker(&tx, input_idx, spent[input_idx].nValue, txdata,
                                        MissingDataBehavior::FAIL);
    ScriptError err = SCRIPT_ERR_OK;
    bool ok = VerifyScript(in.scriptSig, spent[input_idx].scriptPubKey, &in.scriptWitness, flags,
                           checker, &err);
    return ok ? 1 : -(int)err - 1; // negative encodes the ScriptError code
}

// hazync#139 differential — the AUTHORITY side. Calls Core's real `CPubKey::Verify`, not a
// reconstruction of it, so the comparison is against the code that actually decides validity:
// pubkey parse, lax DER parse, low-S normalise, libsecp verify, in that order.
extern "C" int hz_cpubkey_verify(const uint8_t* pk, unsigned pk_len,
                                 const uint8_t* sig_der, unsigned sig_len,
                                 const uint8_t* msg32) {
    CPubKey pubkey(pk, pk + pk_len);
    // NOTE: IsValid() is checked INSIDE CPubKey::Verify, so it is deliberately not pre-checked here —
    // doing so would test a different function from the one the interpreter calls.
    std::vector<unsigned char> sig(sig_der, sig_der + sig_len);
    uint256 hash;
    memcpy(hash.begin(), msg32, 32);
    return pubkey.Verify(hash, sig) ? 1 : 0;
}


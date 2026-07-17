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
#include "ecdsa_vec.h"

// RISC0-accelerated ECDSA verify (Rust k256, guest side).
extern "C" int k256_ecdsa_verify(const uint8_t* msg, const uint8_t* sig, const uint8_t* pk, size_t pk_len);

// Minimal byte reader satisfying the Stream interface Core's Unserialize needs (no streams.h).
struct MiniReader {
    const std::byte* p;
    const std::byte* e;
    void read(Span<std::byte> dst) {
        if (dst.size()) { std::memcpy(dst.data(), p, dst.size()); p += dst.size(); }
    }
    void ignore(size_t n) { p += n; }
    template <typename T> MiniReader& operator>>(T&& obj) { ::Unserialize(*this, obj); return *this; }
};

static void le(unsigned char* b, uint64_t v, int n) { for (int i = 0; i < n; i++) b[i] = (unsigned char)(v >> (8 * i)); }

// Canonical Hazync UTXO-leaf commitment for the coin spent by input `input_idx`:
//   SHA256( txid || vout || value || scriptPubKey || coin_height || is_coinbase || coin_mtp ).
// Height, coinbase flag, and creation median-time-past are committed so maturity + BIP68 (height AND
// time) checks can't lie about the coin's age.
static void coin_leaf(const CTransaction& tx, const std::vector<CTxOut>& spent, unsigned input_idx,
                      uint32_t coin_height, uint32_t coin_is_coinbase, uint32_t coin_mtp, uint8_t* out_leaf) {
    const COutPoint& op = tx.vin[input_idx].prevout;
    const CTxOut& coin = spent[input_idx];
    CSHA256 h;
    unsigned char b8[8];
    h.Write(reinterpret_cast<const unsigned char*>(op.hash.begin()), 32);
    le(b8, op.n, 4); h.Write(b8, 4);
    le(b8, (uint64_t)coin.nValue, 8); h.Write(b8, 8);
    h.Write(reinterpret_cast<const unsigned char*>(coin.scriptPubKey.data()), coin.scriptPubKey.size());
    le(b8, coin_height, 4); h.Write(b8, 4);
    unsigned char cb = (unsigned char)(coin_is_coinbase ? 1 : 0); h.Write(&cb, 1);
    le(b8, coin_mtp, 4); h.Write(b8, 4);
    h.Finalize(out_leaf);
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
    coin_leaf(tx, spent, input_idx, coin_height, coin_is_coinbase, coin_mtp, out_leaf);
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
        h.Write(reinterpret_cast<const unsigned char*>(txid.begin()), 32);
        le(b8, v, 4); h.Write(b8, 4);
        le(b8, (uint64_t)o.nValue, 8); h.Write(b8, 8);
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
    // reserved value = the coinbase input's single 32-byte witness element.
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
extern "C" int check_bip34(const uint8_t* cb, unsigned cb_len, uint32_t height) {
    if (height < 227931) return 1; // pre-activation (Core mainnet consensus.BIP34Height = 227931)
    MiniReader r{reinterpret_cast<const std::byte*>(cb), reinterpret_cast<const std::byte*>(cb) + cb_len};
    CMutableTransaction mtx;
    r >> TX_WITH_WITNESS(mtx);
    CScript expect = CScript() << (int64_t)height;
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
    int64_t s = 0;
    for (const auto& o : mtx.vout) s += o.nValue;
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
    if (spent.size() < tx.vin.size()) return cost; // SEC-3: short prevouts — block rejected via verify_input
    for (size_t i = 0; i < tx.vin.size(); i++) {
        const CScript& spk = spent[i].scriptPubKey;
        if (flags & SCRIPT_VERIFY_P2SH) {
            cost += spk.GetSigOpCount(tx.vin[i].scriptSig) * WITNESS_SCALE_FACTOR; // P2SH redeemScript
        }
        cost += CountWitnessSigOps(tx.vin[i].scriptSig, spk, &tx.vin[i].scriptWitness, flags);
    }
    return cost;
}

// Difficulty retarget: the expected nBits for the block after `prev_bits`, given the epoch's first
// block time and the last block's time. Core's CalculateNextWorkRequired math (pow.cpp) — real
// arith_uint256, mainnet 2-week timespan, clamped 4x, capped at powLimit.
extern "C" uint32_t calc_next_bits(uint32_t prev_bits, int64_t first_time, int64_t last_time) {
    const int64_t timespan = 14 * 24 * 60 * 60; // nPowTargetTimespan (2 weeks)
    int64_t actual = last_time - first_time;
    if (actual < timespan / 4) actual = timespan / 4;
    if (actual > timespan * 4) actual = timespan * 4;
    bool neg, over, n2, o2;
    arith_uint256 bn, powLimit;
    bn.SetCompact(prev_bits, &neg, &over);
    bn *= (uint32_t)actual;
    bn /= (uint32_t)timespan;
    powLimit.SetCompact(0x1d00ffff, &n2, &o2);
    if (bn > powLimit) bn = powLimit;
    return bn.GetCompact();
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

// Block subsidy — exact Core GetBlockSubsidy formula (kept in validation.cpp, too heavy to carve;
// this is the halving schedule verbatim: 50 BTC >> (height / 210000)).
extern "C" int64_t block_subsidy(uint32_t height) {
    int halvings = height / 210000;
    if (halvings >= 64) return 0;
    int64_t subsidy = 50LL * 100000000LL; // 50 * COIN
    subsidy >>= halvings;
    return subsidy;
}

extern "C" int verify_input(const uint8_t* tx_bytes, unsigned tx_len,
                            unsigned input_idx,
                            const uint8_t* prevouts, unsigned prevouts_len,
                            unsigned flags,
                            uint32_t coin_height, uint32_t coin_is_coinbase, uint32_t coin_mtp,
                            uint8_t* out_leaf /* 32 bytes, may be null for bench modes */) {
    // BENCH: isolate one ECDSA verify — real libsecp256k1 (0xB0) vs accelerated k256 (0xB1).
    if (flags == 0xB0) {
        secp256k1_pubkey pubkey;
        secp256k1_ecdsa_signature sig;
        if (!secp256k1_ec_pubkey_parse(secp256k1_context_static, &pubkey, ECV_PK, sizeof(ECV_PK))) return -1;
        if (!secp256k1_ecdsa_signature_parse_compact(secp256k1_context_static, &sig, ECV_SIG)) return -2;
        return secp256k1_ecdsa_verify(secp256k1_context_static, &sig, ECV_MSG, &pubkey);
    }
    if (flags == 0xB1) {
        return k256_ecdsa_verify(ECV_MSG, ECV_SIG, ECV_PK, sizeof(ECV_PK));
    }

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

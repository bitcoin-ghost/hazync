// Guest: verify ONE transaction input using Bitcoin Core's REAL VerifyScript + interpreter + sighash
// + libsecp256k1 — all compiled into the guest via build.rs.
use risc0_zkvm::guest::env;
// COINBASE SMT — INCLUDED BY PATH, NOT DEPENDED ON AS A CRATE (hazync#88).
//
// As a Cargo path dependency this baked the dependency's ABSOLUTE path into the guest ELF
// ("/repo/coinbase-smt/src/lib.rs"), so the image id changed with the checkout location: the same tree
// produced four different ids, and a release host was built that would have rejected every proof from
// its own guest. Core's sources were already normalised by -ffile-prefix-map and the guest's own
// sources are relative; this was the only variable path left.
//
// Included, the file is guest source: the path is recorded relative to this crate and the id no longer
// depends on where the repo lives. THERE IS STILL ONE COPY — the same file is compiled into the host's
// crate for the bridge — so this buys path-independence without the drift a vendored copy invites.
//
// bip30.rs refers to `crate::{apply, verify, ...}`, which resolves here because of the re-export below
// and in the crate because of its own `pub use roots::*`. Both must keep working.
extern crate alloc;

#[path = "../../../../coinbase-smt/src/roots.rs"]
mod smt_roots;
// Re-exported under their ORIGINAL names because bip30.rs says `use crate::{apply, verify, Hash, Key,
// Proof}` and must compile unchanged in both contexts. Safe at this crate root: utreexo's Hash/Proof
// live inside that module, not here, so nothing collides. SmtProof is the alias main.rs uses, kept so
// the witness structs read clearly next to utreexo's own Proof.
pub use smt_roots::{apply, verify, Hash, Key, Proof};
use smt_roots::Proof as SmtProof;

#[path = "../../../../coinbase-smt/src/bip30.rs"]
mod bip30;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

mod utreexo;
mod script_flags;
use script_flags::block_script_flags;

// A byte blob that (de)serialises via risc0 serde's PACKED byte path (deserialize_bytes → 4 bytes/word)
// instead of the default seq path (serialize_u8 emits one u32 word PER byte → 4x bloat). Used for the
// shared, de-duplicated per-tx raw_tx / prevouts blobs in the witness — purely wire encoding, no
// consensus meaning, but it collapses the biggest source of witness I/O.
#[derive(Clone)]
struct PackedBytes(Vec<u8>);
impl serde::Serialize for PackedBytes {
    fn serialize<S: serde::Serializer>(&self, s: S) -> Result<S::Ok, S::Error> { s.serialize_bytes(&self.0) }
}
impl<'de> serde::Deserialize<'de> for PackedBytes {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> serde::de::Visitor<'de> for V {
            type Value = Vec<u8>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result { f.write_str("bytes") }
            fn visit_bytes<E: serde::de::Error>(self, v: &[u8]) -> Result<Vec<u8>, E> { Ok(v.to_vec()) }
            fn visit_byte_buf<E: serde::de::Error>(self, v: Vec<u8>) -> Result<Vec<u8>, E> { Ok(v) }
        }
        Ok(PackedBytes(d.deserialize_byte_buf(V)?))
    }
}

// H8: domain tags — the first committed field of every recursion-consumed journal. env::verify binds
// (image_id, journal) but not the journal's TYPE, so without a tag a mode-1 BlockOutput (which never
// aborts and commits no self_id) or a RangeState/ChunkOut could in principle be laundered in where a
// ChainState is expected, if its bytes happened to decode as one. Committing a distinct constant first
// and asserting it on every decode makes cross-mode confusion impossible.
const KIND_CHAIN: u32 = 0xC4A1_0002;
const KIND_RANGE: u32 = 0xC4A1_0006;
const KIND_CHUNK: u32 = 0xC4A1_0004;

extern "C" {
    // Runs C++ static/global constructors (e.g. Core's HASHER_TAPSIGHASH tagged-hash midstate).
    // The bare-metal guest never calls this on its own, so taproot sighashes would use an
    // uninitialised global. Call once at startup.
    fn __libc_init_array();
    // Core's real VerifyScript, via our thin wrapper (verify_input.cpp). `out_leaf` (32 bytes) is
    // filled with the spent coin's canonical accumulator leaf (null in bench modes).
    fn verify_input(
        tx: *const u8, tx_len: u32, input_idx: u32,
        prevouts: *const u8, prevouts_len: u32, flags: u32,
        coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32,
        out_leaf: *mut u8,
    ) -> i32;
    // Absolute locktime finality (real Core IsFinalTx). 1 = final.
    fn is_final_tx(tx: *const u8, tx_len: u32, height: i64, block_time: i64) -> i32;
    // Coinbase maturity + BIP68 relative locktime (height AND time based) for one input.
    fn check_input_locks(
        tx: *const u8, tx_len: u32, input_idx: u32,
        coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32,
        spend_height: u32, spend_mtp: u32,
    ) -> i32;
    // Real Core CheckTransaction + no-inflation amount rules; `out_fee` gets Σin−Σout.
    fn check_tx(
        tx: *const u8, tx_len: u32,
        prevouts: *const u8, prevouts_len: u32,
        out_fee: *mut i64,
    ) -> i32;
    // Header proof-of-work (real arith_uint256 SetCompact + compare, mainnet powLimit).
    fn check_pow(header: *const u8) -> i32;
    // Real Core ComputeMerkleRoot over `n` 32-byte txids (internal order) -> out_root[32].
    fn merkle_root(txids: *const u8, n: u32, out_root: *mut u8, out_mutated: *mut u8);
    // BIP141 witness commitment check (coinbase commits to the witness merkle root over `wtxids`).
    fn check_witness_commitment(cb: *const u8, cb_len: u32, wtxids: *const u8, n: u32, has_witness: u32) -> i32;
    // BIP34: coinbase scriptSig must encode the block height (from height 227931).
    fn check_bip34(cb: *const u8, cb_len: u32, height: u32) -> i32;
    // Real Core CTransaction::IsCoinBase() on the raw tx bytes: 1 iff exactly one input with a null
    // prevout (#4 — assert the block's "coinbase" really is structurally a coinbase).
    fn is_coinbase_tx(tx: *const u8, tx_len: u32) -> i32;
    // Number of inputs of a tx from its raw bytes (#5 — tie the flat BlockInput list to each tx's vin).
    fn tx_vin_count(tx: *const u8, tx_len: u32) -> u32;
    // The txid an input spends (prevout.hash, internal order) — #54. Read out of the same
    // Core-deserialised tx the scripts run against, so the guest names the coinbase a block spends
    // rather than being told. 0 = input_idx out of range.
    fn tx_input_prevout_txid(tx: *const u8, tx_len: u32, input_idx: u32, out_txid: *mut u8) -> i32;
    // Sum of a coinbase tx's outputs, and the height's block subsidy (exact halving formula).
    fn coinbase_value(tx: *const u8, tx_len: u32) -> i64;
    fn block_subsidy(height: u32) -> i64;
    // Cumulative chainwork: cum += GetBlockProof(nBits) (real Core 256-bit formula).
    fn add_work(cum: *mut u8, nbits: u32);
    // Per-tx weight + legacy sigop cost (real GetSerializeSize + GetSigOpCount).
    fn tx_wu_sigops(tx: *const u8, tx_len: u32, out_weight: *mut i64, out_sigops: *mut i64);
    // Full sigop cost incl P2SH + witness (real Core GetTransactionSigOpCost), needs the coins+flags.
    fn tx_full_sigops(tx: *const u8, tx_len: u32, prevouts: *const u8, prevouts_len: u32, flags: u32) -> i64;
    // Expected nBits after a retarget epoch (real Core CalculateNextWorkRequired math).
    fn calc_next_bits(prev_bits: u32, first_time: i64, last_time: i64) -> u32;
    // Consensus constants read from Core's OWN compiled source (chainparams.cpp buried heights +
    // DifficultyAdjustmentInterval; consensus/consensus.h weight/sigop limits; script/interpreter.h
    // SCRIPT_VERIFY_* bit positions). Used to pin our Rust literals to Core at runtime — see
    // assert_core_constants().
    fn core_bip66_height() -> u32;
    fn core_bip65_height() -> u32;
    fn core_csv_height() -> u32;
    fn core_segwit_height() -> u32;
    fn core_bip34_height() -> u32;
    fn core_retarget_interval() -> u32;
    fn core_max_block_weight() -> i64;
    fn core_max_block_sigops_cost() -> i64;
    fn core_flag_p2sh() -> u32;
    fn core_flag_dersig() -> u32;
    fn core_flag_nulldummy() -> u32;
    fn core_flag_cltv() -> u32;
    fn core_flag_csv() -> u32;
    fn core_flag_witness() -> u32;
    fn core_flag_taproot() -> u32;
    // Coin leaf ONLY (no VerifyScript) — for the aggregation proof to bind chunk results to inputs.
    fn coin_leaf_only(
        tx: *const u8, tx_len: u32, input_idx: u32, prevouts: *const u8, prevouts_len: u32,
        coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32, out_leaf: *mut u8,
    );
    // Recompute a tx's created output leaves (skips unspendable). Writes n*32 leaf bytes to `out` and
    // the txid to `out_txid`; returns n. `out` must hold up to (num outputs)*32 bytes.
    fn tx_out_leaves(
        tx: *const u8, tx_len: u32, height: u32, is_coinbase: u32, block_time: u32,
        out: *mut u8, out_txid: *mut u8,
    ) -> u32;
    // Recompute a tx's BIP141 wtxid (into out_wtxid) + return whether it carries witness data (SEC-1).
    fn tx_wtxid_info(tx: *const u8, tx_len: u32, out_wtxid: *mut u8) -> u32;
}

const MAX_BLOCK_WEIGHT: i64 = 4_000_000;
const MAX_BLOCK_SIGOPS_COST: i64 = 80_000;
const RETARGET_INTERVAL: u32 = 2016;

// Pin every hard-coded consensus literal in the Rust guest to Bitcoin Core's OWN compiled value
// (chainparams.cpp / consensus.h / interpreter.h, via the FFI getters above). Run on every proving
// path: a mismatch aborts the guest, so a proof can never be produced under a constant that has
// silently drifted from Core. Together with the retarget carve (real pow.cpp) and the C++ side reading
// powLimit/timespan/subsidy-interval straight from chainparams, this leaves no consensus magic number
// that isn't either Core's compiled code or runtime-verified equal to it.
fn assert_core_constants() {
    use script_flags as sf;
    unsafe {
        assert_eq!(RETARGET_INTERVAL, core_retarget_interval(), "RETARGET_INTERVAL != Core");
        assert_eq!(MAX_BLOCK_WEIGHT, core_max_block_weight(), "MAX_BLOCK_WEIGHT != Core");
        assert_eq!(MAX_BLOCK_SIGOPS_COST, core_max_block_sigops_cost(), "MAX_BLOCK_SIGOPS_COST != Core");
        assert_eq!(BIP34_HEIGHT, core_bip34_height(), "BIP34Height != Core");
        assert_eq!(sf::BIP66_HEIGHT, core_bip66_height(), "BIP66Height != Core");
        assert_eq!(sf::BIP65_HEIGHT, core_bip65_height(), "BIP65Height != Core");
        assert_eq!(sf::CSV_HEIGHT, core_csv_height(), "CSVHeight != Core");
        assert_eq!(sf::SEGWIT_HEIGHT, core_segwit_height(), "SegwitHeight != Core");
        assert_eq!(sf::P2SH, core_flag_p2sh(), "SCRIPT_VERIFY_P2SH != Core");
        assert_eq!(sf::DERSIG, core_flag_dersig(), "SCRIPT_VERIFY_DERSIG != Core");
        assert_eq!(sf::NULLDUMMY, core_flag_nulldummy(), "SCRIPT_VERIFY_NULLDUMMY != Core");
        assert_eq!(sf::CLTV, core_flag_cltv(), "SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY != Core");
        assert_eq!(sf::CSV, core_flag_csv(), "SCRIPT_VERIFY_CHECKSEQUENCEVERIFY != Core");
        assert_eq!(sf::WITNESS, core_flag_witness(), "SCRIPT_VERIFY_WITNESS != Core");
        assert_eq!(sf::TAPROOT, core_flag_taproot(), "SCRIPT_VERIFY_TAPROOT != Core");
    }
}

// Consensus VerifyScript flags active at a given mainnet height (soft-fork activation heights).
// This is how BIP66/65/112(CSV)/147/segwit/taproot get enforced — through VerifyScript. The
// height→flags schedule + the two script_flag_exception blocks live in `script_flags` (shared with the
// host differential test `host script-flags-test`).

// BIP30 grandfathered duplicate-coinbase blocks (internal/dsha256(header) order). Each reuses an
// earlier still-unspent coinbase's outpoint; pre-BIP30-enforcement Core OVERWRITES the old coin (it
// becomes unspendable). At exactly these two blocks the guest deletes the superseded coinbase leaf so
// it can't linger spendable (F3). 91842 duplicates 91812's coinbase; 91880 duplicates 91722's.
// (BIP34, enforced from 227931, makes coinbases unique thereafter, so no later duplicate can occur.)
/// Core mainnet `consensus.BIP34Height`. Runtime-pinned to Core's compiled value by
/// `assert_core_constants`, like BIP66/BIP65/CSV/Segwit — audit #3's phase-2 sweep found this was the
/// one buried height still hand-typed, in two places, with nothing checking it.
const BIP34_HEIGHT: u32 = 227_931;

const BIP30_OVERWRITE_A: [u8; 32] = [0xec, 0xca, 0xe0, 0x00, 0xe3, 0xc8, 0xe4, 0xe0, 0x93, 0x93, 0x63, 0x60, 0x43, 0x1f, 0x3b, 0x76, 0x03, 0xc5, 0x63, 0xc1, 0xff, 0x61, 0x81, 0x39, 0x0a, 0x4d, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00]; // block 91842
const BIP30_OVERWRITE_B: [u8; 32] = [0x21, 0xd7, 0x7c, 0xcb, 0x4c, 0x08, 0x38, 0x6a, 0x04, 0xac, 0x01, 0x96, 0xae, 0x10, 0xf6, 0xa1, 0xd2, 0xc2, 0xa3, 0x77, 0x55, 0x8c, 0xa1, 0x90, 0xf1, 0x43, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00]; // block 91880

// --- libc glue for bare-metal C/C++ in the zkVM guest: malloc family + abort, backed by the
// guest's Rust global allocator (size stored in a 16-byte header before each block). ---
use std::alloc::{alloc as ralloc, dealloc as rdealloc, Layout};
const HDR: usize = 16;
#[no_mangle]
pub unsafe extern "C" fn malloc(size: usize) -> *mut u8 {
    if size == 0 { return core::ptr::null_mut(); }
    let total = size + HDR;
    let p = ralloc(Layout::from_size_align(total, HDR).unwrap());
    if p.is_null() { return p; }
    *(p as *mut usize) = total;
    p.add(HDR)
}
#[no_mangle]
pub unsafe extern "C" fn free(ptr: *mut u8) {
    if ptr.is_null() { return; }
    let base = ptr.sub(HDR);
    let total = *(base as *mut usize);
    rdealloc(base, Layout::from_size_align(total, HDR).unwrap());
}
#[no_mangle]
pub unsafe extern "C" fn calloc(n: usize, sz: usize) -> *mut u8 {
    let total = n.wrapping_mul(sz);
    let p = malloc(total);
    if !p.is_null() { core::ptr::write_bytes(p, 0, total); }
    p
}
#[no_mangle]
pub unsafe extern "C" fn realloc(ptr: *mut u8, size: usize) -> *mut u8 {
    if ptr.is_null() { return malloc(size); }
    let base = ptr.sub(HDR);
    let old = *(base as *mut usize) - HDR;
    let np = malloc(size);
    if !np.is_null() { core::ptr::copy_nonoverlapping(ptr, np, core::cmp::min(old, size)); free(ptr); }
    np
}
#[no_mangle]
pub extern "C" fn abort() -> ! { panic!("C abort()") }

// Satisfy USE_EXTERNAL_DEFAULT_CALLBACKS without pulling in stdio/abort.
#[no_mangle]
pub extern "C" fn secp256k1_default_illegal_callback_fn(_msg: *const u8, _data: *mut core::ffi::c_void) {}
#[no_mangle]
pub extern "C" fn secp256k1_default_error_callback_fn(_msg: *const u8, _data: *mut core::ffi::c_void) {}

// ---- Block-proof wire format (matches the host structs) ----
#[derive(Deserialize)]
struct WireProof {
    leaf: [u8; 32],
    position: u64,
    siblings: Vec<[u8; 32]>,
}
#[derive(Deserialize)]
struct BlockInput {
    tx_idx: u32,            // index into BlockWitness.txs / tx_prevouts (the tx this input belongs to).
                           // raw_tx + prevouts are de-duplicated: one shared blob per tx, not per input
                           // (a multi-input tx would otherwise repeat its full bytes N times).
    input_idx: u32,
    // NOTE: consensus script flags are NOT taken from the host — they are derived in-guest by
    // block_script_flags(height, block_hash). The field was removed (was dead/never read) so a future
    // change cannot accidentally start honouring a host-chosen flag set. Do not re-add a host flags input.
    global_pos: u64,        // the spent coin's current position in the accumulator
    coin_height: u32,       // height the spent coin was created at (leaf-committed)
    coin_is_coinbase: u32,  // whether the spent coin is a coinbase output (leaf-committed)
    coin_mtp: u32,          // median-time-past at the coin's creation (leaf-committed; BIP68 time)
    tx_first: u32,          // 1 for the first input of its tx (gates per-tx checks: CheckTx/fee/weight/sigops)
    proof_i: WireProof,     // inclusion of the spent coin
    proof_last: WireProof,  // inclusion of the current rightmost coin (for swap-and-shrink)
}
#[derive(Deserialize)]
struct WireStump {
    roots: Vec<Option<[u8; 32]>>,
    num_leaves: u64,
}
// F3 / BIP30 overwrite: at the two grandfathered duplicate-coinbase blocks, the proof(s) to delete the
// superseded coinbase leaf(s). The leaf itself is RECOMPUTED by the guest from this block's coinbase at
// `old_height`/`old_mtp` (the duplicate coinbase is byte-identical), so a prover cannot delete an
// arbitrary coin — only a genuine earlier duplicate of this coinbase's outpoint.
#[derive(Deserialize)]
struct Bip30Del { global_pos: u64, proof_i: WireProof, proof_last: WireProof }
#[derive(Deserialize)]
struct Bip30Overwrite { old_height: u32, old_mtp: u32, dels: Vec<Bip30Del> }
// #54 — one coinbase-output spend. `coinbase_txid` is present but is NOT trusted: the guest derives
// the same value from the transaction and asserts they agree, so the field is a cross-check that
// cannot rot rather than an input a prover controls. `current_count` and `proof` are the only things
// genuinely supplied, and both are pinned by the root: a wrong count folds to a different root.
#[derive(Deserialize)]
struct SmtSpendW { coinbase_txid: [u8; 32], current_count: u32, proof: SmtProof }
#[derive(Deserialize)]
struct SmtWitnessW {
    // Cross-checks, not inputs — the guest derives both and refuses the block if they disagree. See
    // the note on SmtSpendW: a value the guest derives must never be a value it is told.
    coinbase_txid: [u8; 32],
    coinbase_outputs: u32,
    absence_proof: SmtProof,
    spends: Vec<SmtSpendW>,
    // #54 / audit#3 F-1 — `Some(prior_count)` ONLY at the two blocks BIP30 grandfathers. Gated below
    // on the block hash the guest DERIVES, exactly as the utreexo F3 overwrite is: required at those
    // two hashes, forbidden everywhere else. A prover cannot opt into it. LAST, because risc0's serde
    // is positional and appending is the only change that does not renumber every field on both sides.
    smt_overwrite: Option<u32>,
}
#[derive(Deserialize)]
struct BlockWitness {
    header: Vec<u8>,            // 80-byte block header
    height: u32,               // block height (for the subsidy schedule)
    coinbase_tx: Vec<u8>,      // the coinbase tx (its outputs = subsidy + fees)
    txids: Vec<[u8; 32]>,      // all txids in order (internal), for the merkle root
    wtxids: Vec<[u8; 32]>,     // all wtxids (coinbase = zeros), for the BIP141 witness commitment
    root_prev: WireStump,
    txs: Vec<PackedBytes>,     // the block's non-coinbase txs, ONE shared blob each (raw bytes)
    tx_prevouts: Vec<PackedBytes>, // parallel to `txs`: each tx's concatenated input prevouts blob
    inputs: Vec<BlockInput>,   // non-coinbase input verifications (each refers to its tx by tx_idx)
    new_outputs: Vec<[u8; 32]>, // leaves of the coins the block creates
    root_next: WireStump,
    bip30: Option<Bip30Overwrite>, // Some ONLY at the two grandfathered BIP30 blocks (F3)
    // #54: coinbase-SMT root entering this block, and the sequenced proofs that advance it. REQUIRED,
    // not Option — an optional consensus input is not an input, and a bundle that predates the fields
    // should fail to parse loudly rather than prove against a default.
    in_smt_root: [u8; 32],
    smt: SmtWitnessW,
}
#[derive(Serialize)]
struct BlockOutput {
    script_results: Vec<i32>,   // per-input VerifyScript result (1 = valid)
    tx_checks: Vec<i32>,        // per-tx CheckTransaction + amount rules (1 = valid)
    coin_leaves: Vec<[u8; 32]>, // guest-computed leaves (host cross-checks format)
    total_fee: i64,             // Σ fees across the block's txs
    pow_ok: bool,               // header hash ≤ target ≤ powLimit
    merkle_ok: bool,            // ComputeMerkleRoot(txids) == header.hashMerkleRoot
    coinbase_val: i64,          // Σ coinbase outputs
    subsidy: i64,               // GetBlockSubsidy(height)
    subsidy_ok: bool,           // coinbase_val ≤ subsidy + total_fee (no over-issuance)
    all_ok: bool,               // scripts valid AND consensus checks pass AND coins in the set
    root_matches: bool,         // resulting UTXO root == committed root_next
}

fn to_proof(w: &WireProof) -> utreexo::Proof {
    utreexo::Proof { leaf: w.leaf, position: w.position, siblings: w.siblings.clone() }
}

fn normalize(mut v: Vec<Option<[u8; 32]>>) -> Vec<Option<[u8; 32]>> {
    while v.last() == Some(&None) {
        v.pop();
    }
    v
}

// Full result of validating one block — the block-level flags plus the derived facts the chain
// step needs (this block's hash, its nBits, and the resulting UTXO root).
struct BlockResult {
    script_results: Vec<i32>,
    tx_checks: Vec<i32>,
    coin_leaves: Vec<[u8; 32]>,
    total_fee: i64,
    pow_ok: bool,
    merkle_ok: bool,
    coinbase_val: i64,
    subsidy: i64,
    subsidy_ok: bool,
    all_ok: bool,
    root_matches: bool,
    weight_ok: bool,
    sigops_ok: bool,
    witness_ok: bool,
    bip34_ok: bool,
    bip30_ok: bool,
    out_smt_root: [u8; 32],   // #54: the coinbase-SMT root AFTER this block's transition
    tip_hash: [u8; 32],
    nbits: u32,
    block_time: u32,
    root_next_roots: Vec<Option<[u8; 32]>>,
    root_next_leaves: u64,
}

fn dsha256(data: &[u8]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let h1 = Sha256::digest(data);
    Sha256::digest(h1).into()
}

// #2: canonical digest binding one input's ENTIRE script-verification context — the exact spending tx
// bytes, the input index, the prevouts the script ran against, the spent coin's metadata, AND the
// consensus flags. A chunk proof (mode 4) commits this per input; the aggregation (mode 5) recomputes
// it from the block's own input and requires equality. Without it a chunk could prove "some valid spend
// of this coin under attacker-chosen (weaker) flags" and the aggregation would accept a DIFFERENT
// spending witness / lower-flag verification for the block's input. Length-prefixed to be unambiguous.
fn input_bind(raw_tx: &[u8], input_idx: u32, prevouts: &[u8],
              coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32, flags: u32) -> [u8; 32] {
    let mut m = Vec::with_capacity(raw_tx.len() + prevouts.len() + 24);
    m.extend_from_slice(&(raw_tx.len() as u32).to_le_bytes());
    m.extend_from_slice(raw_tx);
    m.extend_from_slice(&input_idx.to_le_bytes());
    m.extend_from_slice(&(prevouts.len() as u32).to_le_bytes());
    m.extend_from_slice(prevouts);
    m.extend_from_slice(&coin_height.to_le_bytes());
    m.extend_from_slice(&coin_is_coinbase.to_le_bytes());
    m.extend_from_slice(&coin_mtp.to_le_bytes());
    m.extend_from_slice(&flags.to_le_bytes());
    dsha256(&m)
}

// Validate one whole block against `w.root_prev`: every input's script (real VerifyScript), the coin
// it spends present in the UTXO accumulator (bound by canonical leaf), spent coins removed + created
// inserted == root_next, plus real CheckTransaction, no-inflation amounts, PoW, merkle root, subsidy.
// `mtp` = the median-time-past to use for BIP113/BIP68 time rules (the previous block's MTP; for a
// standalone block, its own timestamp as a pre-activation fallback).
// `chunk` = aggregation mode: (per-input leaves already script-verified by chunk proofs, all_valid).
// When Some, scripts are NOT re-verified here — the leaf is recomputed (coin_leaf_only) and matched.
fn validate_block(w: &BlockWitness, mtp: u32, chunk: Option<(&Vec<[u8; 32]>, bool)>) -> BlockResult {
    let mut stump = utreexo::Stump::new(w.root_prev.roots.clone(), w.root_prev.num_leaves);
    let mut script_results = Vec::with_capacity(w.inputs.len());
    let mut tx_checks = Vec::with_capacity(w.inputs.len());
    let mut coin_leaves = Vec::with_capacity(w.inputs.len());
    let mut total_fee: i64 = 0;
    let mut all_ok = true;

    // The header is the sole PoW/merkle/time/version source. check_pow hashes exactly 80 bytes while
    // dsha256(&w.header) hashes the whole Vec, so a padded header would make the committed tip_hash
    // diverge from the canonical block hash, and a <80 header is an out-of-bounds read in check_pow.
    assert!(w.header.len() == 80, "block header must be exactly 80 bytes, got {}", w.header.len());

    let block_time = u32::from_le_bytes(w.header[68..72].try_into().unwrap());
    let block_hash = dsha256(&w.header); // this block's hash (internal order): flag exceptions + tip
    // Consensus script flags (Core GetBlockScriptFlags: always-on base + buried deployments + exceptions).
    let flags = block_script_flags(w.height, &block_hash);
    // BIP113: from CSV activation (419328), locktime uses median-time-past instead of block time.
    let lock_time = if w.height >= 419_328 { mtp } else { block_time };

    // nVersion soft-fork rejection (Core ContextualCheckBlockHeader): once a version's soft fork is
    // buried, a block below that version is invalid regardless of its scripts. Heights: BIP34 (v>=2 @
    // 227931), BIP66 (v>=3 @363725), BIP65/CLTV (v>=4 @388381). The height-derived script flags already
    // enforce the RULES; this rejects the stale header itself as Core does, closing an accept-invalid gap.
    let version = i32::from_le_bytes(w.header[0..4].try_into().unwrap());
    if (version < 2 && w.height >= BIP34_HEIGHT)
        || (version < 3 && w.height >= 363_725)
        || (version < 4 && w.height >= 388_381) {
        all_ok = false;
    }

    // Recompute the block's created output leaves from the REAL tx bytes (coinbase + every tx), skipping
    // unspendable outputs (H3) — instead of trusting host-supplied w.new_outputs (soundness). Each tx's
    // computed txid is bound to the merkle-committed w.txids, so the raw bytes ARE the block's txs. The
    // output set also lets us detect in-block-created coins (H1): an input whose leaf is in this set
    // spends a coin created earlier in this block (ephemeral — never entered the accumulator).
    let mut output_leaves: Vec<[u8; 32]> = Vec::new();
    // #3: map each in-block-created coin leaf -> the index of the tx that created it (0 = coinbase,
    // 1 = first non-coinbase tx, ...). An input spending one of these ephemeral coins must be in a
    // strictly LATER tx and may spend it at most once. The accumulator (which normally enforces
    // single-spend) is bypassed for in-block coins, so both rules must be enforced explicitly below.
    let mut created_at: BTreeMap<[u8; 32], u32> = BTreeMap::new();
    let gather = |raw: &[u8], is_cb: u32, sink: &mut Vec<[u8; 32]>| -> [u8; 32] {
        let mut buf = vec![0u8; (raw.len() / 8 + 1) * 32];
        let mut txid = [0u8; 32];
        // Created-output creation-MTP = `mtp` (the block's median-time-past, MTP(h-1)) — the real BIP68
        // value Core commits, not the raw block timestamp. Each mode passes the right mtp (chain_step/
        // aggregate/prove_range: median(prev.recent_times); block_proof standalone: block_time).
        let n = unsafe { tx_out_leaves(raw.as_ptr(), raw.len() as u32, w.height, is_cb, mtp, buf.as_mut_ptr(), txid.as_mut_ptr()) };
        for i in 0..n as usize {
            let mut l = [0u8; 32];
            l.copy_from_slice(&buf[i * 32..i * 32 + 32]);
            sink.push(l);
        }
        txid
    };
    let cb_start = output_leaves.len();
    let cb_txid = gather(&w.coinbase_tx, 1, &mut output_leaves);
    // #54 — the coinbase's SPENDABLE output count, captured HERE and not later: `output_leaves` goes
    // on to accumulate every other tx's leaves, so the same subtraction further down the function
    // silently yields the whole block's output count. It did, and the fixture blocks caught it.
    let cb_outputs = (output_leaves.len() - cb_start) as u32;
    for l in &output_leaves[cb_start..] { created_at.entry(*l).or_insert(0u32); }
    if w.txids.is_empty() || cb_txid != w.txids[0] { all_ok = false; }
    // The de-duplicated per-tx blobs: one raw_tx + one prevouts blob per non-coinbase tx. Bind their
    // counts to the merkle-committed tx set so a prover can neither add nor drop a tx.
    if w.txs.len() + 1 != w.txids.len() || w.tx_prevouts.len() != w.txs.len() { all_ok = false; }
    let mut tx_pos = 1usize; // 1-based block position (coinbase is position 0 / txids[0])
    for inp in &w.inputs {
        if inp.tx_first == 1 {
            // The tx a first-input refers to must be the tx at this block position (txs in block order).
            if inp.tx_idx as usize != tx_pos - 1 { all_ok = false; }
            let raw_tx = if (inp.tx_idx as usize) < w.txs.len() { &w.txs[inp.tx_idx as usize].0 } else { all_ok = false; break; };
            let start = output_leaves.len();
            let t = gather(raw_tx, 0, &mut output_leaves);
            for l in &output_leaves[start..] { created_at.entry(*l).or_insert(tx_pos as u32); }
            if tx_pos >= w.txids.len() || t != w.txids[tx_pos] { all_ok = false; }
            tx_pos += 1;
        }
    }
    if tx_pos != w.txids.len() { all_ok = false; } // tx count must match the merkle-committed set
    let mut spent_in_block: BTreeSet<[u8; 32]> = BTreeSet::new();
    let mut cur_tx: u32 = 0; // index of the tx currently being processed (increments on each tx_first)

    // #5: tie the flat host-supplied input list to each transaction's real inputs. Each tx must have
    // EXACTLY vin_count consecutive BlockInputs (input_idx 0..n-1 in order, tx_first only on the first),
    // all carrying the identical raw_tx and prevouts blob. Each BlockInput authenticates its own
    // prevouts[input_idx] against the accumulator, so requiring one shared blob per tx makes EVERY entry
    // that check_tx (the fee sum) and the sigop counter read an authenticated coin. Without this a prover
    // pads the first input's fee blob with a phantom high-value coin (fee inflation -> mint via the
    // coinbase) or omits a BlockInput entirely (its script is never checked and its coin never deleted
    // -> theft / double-spend).
    {
        let mut i = 0usize;
        let mut group_ok = true;
        while i < w.inputs.len() {
            let head = &w.inputs[i];
            if head.tx_first != 1 || (head.tx_idx as usize) >= w.txs.len() { group_ok = false; break; }
            let ht = &w.txs[head.tx_idx as usize].0;
            let n = unsafe { tx_vin_count(ht.as_ptr(), ht.len() as u32) } as usize;
            if n == 0 || i + n > w.inputs.len() { group_ok = false; break; }
            for j in 0..n {
                let g = &w.inputs[i + j];
                // same tx_idx ⟹ identical shared raw_tx + prevouts blob (they index the same object)
                if g.tx_idx != head.tx_idx
                    || g.input_idx as usize != j || (g.tx_first == 1) != (j == 0) {
                    group_ok = false; break;
                }
            }
            if !group_ok { break; }
            i += n;
        }
        if !group_ok { all_ok = false; }
    }

    for (idx, inp) in w.inputs.iter().enumerate() {
        if inp.tx_first == 1 { cur_tx += 1; } // this input begins a new tx (1 = first non-coinbase tx)
        // Resolve this input's shared (de-duplicated) tx + prevouts blob. w.inputs is non-empty here ⇒
        // w.txs is non-empty; clamp a malformed out-of-range tx_idx to reject cleanly instead of panicking.
        let ti = (inp.tx_idx as usize).min(w.txs.len() - 1);
        if ti != inp.tx_idx as usize { all_ok = false; }
        let raw_tx = &w.txs[ti].0;
        let prevouts = &w.tx_prevouts[ti].0;
        let mut leaf = [0u8; 32];
        let r = match chunk {
            None => unsafe {
                // Full verify (expensive VerifyScript), fills `leaf`.
                verify_input(
                    raw_tx.as_ptr(), raw_tx.len() as u32, inp.input_idx,
                    prevouts.as_ptr(), prevouts.len() as u32, flags,
                    inp.coin_height, inp.coin_is_coinbase, inp.coin_mtp,
                    leaf.as_mut_ptr(),
                )
            },
            Some((chunk_binds, all_valid)) => {
                // Aggregation: recompute the leaf (cheap, for the accumulator delete below) and take
                // script validity from the chunks — but ONLY after proving the chunk verified THIS
                // input. #2: recompute the same binding digest the chunk committed (tx bytes, input idx,
                // prevouts, coin metadata, and the block's own flags) and require it matches. This binds
                // both the spending witness and the flags, so a chunk cannot substitute a different
                // valid spend of the coin or validate it under attacker-chosen weaker flags.
                unsafe {
                    coin_leaf_only(
                        raw_tx.as_ptr(), raw_tx.len() as u32, inp.input_idx,
                        prevouts.as_ptr(), prevouts.len() as u32,
                        inp.coin_height, inp.coin_is_coinbase, inp.coin_mtp, leaf.as_mut_ptr(),
                    )
                };
                let d = input_bind(raw_tx, inp.input_idx, prevouts,
                    inp.coin_height, inp.coin_is_coinbase, inp.coin_mtp, flags);
                if idx < chunk_binds.len() && chunk_binds[idx] == d && all_valid { 1 } else { -1 }
            }
        };
        script_results.push(r);
        coin_leaves.push(leaf);

        // Per-TX checks — run once per tx (on its first input): structural + no-inflation + finality.
        if inp.tx_first == 1 {
            let mut fee: i64 = 0;
            let c = unsafe {
                check_tx(
                    raw_tx.as_ptr(), raw_tx.len() as u32,
                    prevouts.as_ptr(), prevouts.len() as u32,
                    &mut fee as *mut i64,
                )
            };
            tx_checks.push(c);
            if c != 1 { all_ok = false; } else { total_fee += fee; }
            let final_ok = unsafe {
                is_final_tx(raw_tx.as_ptr(), raw_tx.len() as u32, w.height as i64, lock_time as i64)
            } == 1;
            if !final_ok { all_ok = false; }
        }

        // Per-INPUT: coinbase maturity + BIP68 relative locktime (height + time).
        let locks = unsafe {
            check_input_locks(
                raw_tx.as_ptr(), raw_tx.len() as u32, inp.input_idx,
                inp.coin_height, inp.coin_is_coinbase, inp.coin_mtp, w.height, mtp,
            )
        };
        if locks != 1 {
            all_ok = false;
        }

        if r != 1 {
            all_ok = false;
        }
        if let Some(&creator) = created_at.get(&leaf) {
            // IN-BLOCK spend (H1): this coin was created by an earlier tx in THIS block, so it never
            // entered the accumulator — cancel it (ephemeral). Its script still had to pass (above).
            // #3: the coin must be created by a STRICTLY earlier tx (no spend-before-create, no
            // self-spend) and spent AT MOST ONCE (BTreeSet::insert returns false on a repeat) — else a
            // prover consumes one in-block output twice and mints its value.
            if creator >= cur_tx { all_ok = false; }
            if !spent_in_block.insert(leaf) { all_ok = false; }
        } else {
            // EXTERNAL spend: the coin exists in the accumulator — verify inclusion + delete it.
            if inp.proof_i.leaf != leaf {
                all_ok = false;
            }
            let pi = utreexo::Proof { leaf, position: inp.proof_i.position, siblings: inp.proof_i.siblings.clone() };
            let pl = to_proof(&inp.proof_last);
            if !stump.delete(inp.global_pos, &pi, &pl) {
                all_ok = false;
            }
        }
    }

    // F3 / BIP30 grandfathered overwrite (blocks 91842/91880 ONLY): the coinbase reuses an earlier
    // still-unspent coinbase's outpoint; pre-enforcement Core OVERWRITES it. Recompute the superseded
    // coinbase output leaf(s) — this coinbase's spendable outputs at the OLD height/mtp (the duplicate
    // coinbase is byte-identical, so same txid/value/spk; only height+mtp differ) — and delete them, so
    // the superseded coins can't linger spendable. The delete is bound to this coinbase's outpoint at a
    // real earlier height (a wrong old_height => recomputed leaf misses the accumulator => delete fails),
    // so only a genuine duplicate can be removed. Mandatory at these two hashes; the witness must carry it.
    let is_bip30_block = block_hash == BIP30_OVERWRITE_A || block_hash == BIP30_OVERWRITE_B;
    match (&w.bip30, is_bip30_block) {
        (Some(ov), true) => {
            let mut buf = vec![0u8; (w.coinbase_tx.len() / 8 + 1) * 32];
            let mut _t = [0u8; 32];
            let n = unsafe { tx_out_leaves(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, ov.old_height, 1, ov.old_mtp, buf.as_mut_ptr(), _t.as_mut_ptr()) } as usize;
            if n != ov.dels.len() { all_ok = false; }
            for (i, d) in ov.dels.iter().enumerate() {
                if i >= n { all_ok = false; break; }
                let mut leaf = [0u8; 32];
                leaf.copy_from_slice(&buf[i * 32..i * 32 + 32]);
                let pi = utreexo::Proof { leaf, position: d.proof_i.position, siblings: d.proof_i.siblings.clone() };
                let pl = to_proof(&d.proof_last);
                if !stump.delete(d.global_pos, &pi, &pl) { all_ok = false; }
            }
        }
        (None, true) => all_ok = false,     // overwrite REQUIRED at these two blocks — a prover cannot skip it
        (Some(_), false) => all_ok = false, // overwrite only permitted at the two grandfathered blocks
        (None, false) => {}
    }

    // Add the SURVIVING created outputs — recomputed from the txs (unspendable skipped, in-block-spent
    // cancelled), in canonical order (coinbase then each tx, vout order). NOT host-supplied new_outputs.
    for leaf in &output_leaves {
        if !spent_in_block.contains(leaf) {
            stump.add(*leaf);
        }
    }

    let root_matches =
        stump.normalized() == normalize(w.root_next.roots.clone()) && stump.num_leaves == w.root_next.num_leaves;

    // ---- Block-level checks: PoW, merkle root, coinbase subsidy (no over-issuance). ----
    let pow_ok = unsafe { check_pow(w.header.as_ptr()) } == 1;

    let mut mroot = [0u8; 32];
    let mut mutated = 0u8;
    let flat: Vec<u8> = w.txids.iter().flatten().copied().collect();
    unsafe { merkle_root(flat.as_ptr(), w.txids.len() as u32, mroot.as_mut_ptr(), &mut mutated) };
    // root matches header AND the tree is not malleated (CVE-2012-2459 duplicate-txid mutation).
    let merkle_ok = mroot[..] == w.header[36..68] && mutated == 0; // header 36..68 = hashMerkleRoot

    // BIP141 witness commitment (SEC-1): recompute the wtxids + has_witness from the REAL tx bytes —
    // NOT the host-supplied w.wtxids — so a prover cannot claim "no witness" to skip the commitment.
    // Coinbase wtxid is committed as all-zeros in the witness merkle (BIP141), but its OWN witness data
    // (the reserved-value nonce) DOES count toward has_witness — Core's unexpected-witness loop runs over
    // EVERY tx including the coinbase (G2: previously the coinbase was excluded from has_witness).
    let mut cb_wt = [0u8; 32];
    let cb_hw = unsafe { tx_wtxid_info(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, cb_wt.as_mut_ptr()) };
    let mut rec_wtxids: Vec<[u8; 32]> = vec![[0u8; 32]]; // coinbase leaf = zeros in the witness merkle
    let mut has_witness = cb_hw == 1;
    for inp in &w.inputs {
        if inp.tx_first == 1 {
            let raw_tx = &w.txs[(inp.tx_idx as usize).min(w.txs.len() - 1)].0;
            let mut wt = [0u8; 32];
            let hw = unsafe { tx_wtxid_info(raw_tx.as_ptr(), raw_tx.len() as u32, wt.as_mut_ptr()) };
            rec_wtxids.push(wt);
            has_witness |= hw == 1;
        }
    }
    let flat_wtx: Vec<u8> = rec_wtxids.iter().flatten().copied().collect();
    // G3: segwit activates at mainnet SegwitHeight 481824 (Core GetBlockScriptFlags / DeploymentActiveAfter).
    // BELOW activation Core never looks for a commitment and REJECTS any block carrying witness data
    // (unexpected-witness) — so witness_ok = "no witness present". FROM activation the BIP141 commitment is
    // enforced by check_witness_commitment, which also rejects a witness-carrying block that lacks the
    // commitment output (its own has_witness gate). Running the commitment check at ALL heights (the previous
    // behaviour) rejected the canonical pre-activation blocks that already carried an early commitment output
    // yet have a witness-free coinbase — a reject-valid liveness bug in the 433k–481823 range.
    let witness_ok = if w.height >= 481_824 {
        let rc = unsafe {
            check_witness_commitment(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32,
                flat_wtx.as_ptr(), rec_wtxids.len() as u32, has_witness as u32)
        };
        rc == 1
    } else {
        !has_witness // pre-segwit: no witness data permitted (Core unexpected-witness)
    };
    // BIP34: coinbase encodes the block height (from 227931).
    let bip34_ok = unsafe { check_bip34(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, w.height) } == 1;
    // BIP30 (no tx may create an outpoint duplicating an existing UNSPENT coin). A utreexo Stump cannot
    // prove NON-membership, so this is carried by a SECOND accumulator: a coinbase-only sparse Merkle
    // tree mapping coinbase txid -> unspent output count, whose root is journalled and folded like the
    // utreexo one. Absence and a zero count are the SAME state in that tree, so "prove it is absent" is
    // exactly "prove BIP30 is satisfied" — and a fully-spent duplicate stays legal, which it must
    // (Core accepted one in 2010).
    //
    // THIS REPLACES THE OLD STRUCTURAL ARGUMENT, which leaned on BIP34 making coinbase txids unique per
    // height and therefore expired at ~1,983,702 — the height where a BIP34 height-push could reproduce
    // a pre-BIP34 coinbase scriptSig. That ceiling is gone: the check no longer depends on any property
    // of scriptSig encoding.
    //
    // WHAT THE GUEST DERIVES vs WHAT IT IS TOLD. Everything that identifies the transition is derived
    // here: the coinbase txid and its spendable-output count from the coinbase transaction, and the
    // spent coinbase's txid from the same Core-deserialised transaction the scripts ran against. Only
    // the PROOFS come from the witness, and a wrong proof folds to a different root and fails. If a
    // prover could name which coinbases a block spends, it could decrement one the block never touched
    // down to zero and manufacture a free slot for a later duplicate — the exact attack this exists to
    // stop.
    //
    // In-block duplicate txids are still rejected: two txs in one block sharing an outpoint is a
    // separate failure from the cross-block one, and cheap to check over the merkle-committed set.
    let ids_distinct = {
        let mut ids = w.txids.clone();
        ids.sort_unstable();
        ids.windows(2).all(|w| w[0] != w[1])
    };
    // `cb_outputs` was captured immediately after the coinbase gather — see the note there.
    // The coinbase txid of every input spending a coinbase output, in the block's own tx-then-input
    // order — the same order the bridge emits proofs in. `w.inputs` is already pinned to that order by
    // the vin-grouping check above (`input_idx == j`, one entry per real input, no padding).
    let mut cb_spends: Vec<[u8; 32]> = Vec::new();
    let mut spends_ok = true;
    for inp in w.inputs.iter() {
        if inp.coin_is_coinbase != 1 { continue; }   // leaf-committed, so it cannot be lied about
        let raw = match w.txs.get(inp.tx_idx as usize) { Some(t) => &t.0, None => { spends_ok = false; break; } };
        let mut t = [0u8; 32];
        let rc = unsafe { tx_input_prevout_txid(raw.as_ptr(), raw.len() as u32, inp.input_idx, t.as_mut_ptr()) };
        if rc != 1 { spends_ok = false; break; }
        cb_spends.push(t);
    }
    // The witness's spend list must line up with the derived one. This is a cross-check, not a source
    // of truth — it exists so a bridge that reorders its emission fails HERE with a clear cause rather
    // than as an unexplained proof rejection three lines later.
    if spends_ok {
        spends_ok = w.smt.coinbase_txid == cb_txid
            && w.smt.coinbase_outputs == cb_outputs
            && w.smt.spends.len() == cb_spends.len()
            && w.smt.spends.iter().zip(cb_spends.iter()).all(|(s, d)| &s.coinbase_txid == d);
        if !spends_ok {
            // Named, because the alternative is a bare BadProof from three steps later that says
            // nothing about which of the two sides is wrong.
            env::log(&format!(
                "BIP30 witness disagrees with the derived transition: cb_txid_match={} outputs derived={} witness={} spends derived={} witness={}",
                w.smt.coinbase_txid == cb_txid, cb_outputs, w.smt.coinbase_outputs,
                cb_spends.len(), w.smt.spends.len()));
        }
    }
    // AUDIT #3 F-1. At 91842 and 91880 the duplicated coinbase was still UNSPENT — that is why BIP30
    // exists and why Core grandfathers exactly these two heights. Entering them the tree holds the
    // txid with a nonzero count, so NO absence proof can exist and the ordinary check is reject-valid
    // on real history: a from-genesis prover would stall at 91841, ~10% of the chain.
    //
    // So the same two block hashes that mandate the utreexo overwrite above also switch the SMT to an
    // overwrite. Gated identically and in both directions — required there, forbidden elsewhere — so
    // it is an exception history forces, not an escape hatch a prover can reach for. The transition
    // still binds the prover to the real prior count.
    let overwrite_ok = w.smt.smt_overwrite.is_some() == is_bip30_block;
    if !overwrite_ok {
        env::log(&format!(
            "BIP30 SMT overwrite claim does not match the block: is_grandfathered={} claimed={}",
            is_bip30_block, w.smt.smt_overwrite.is_some()));
    }
    let (bip30_ok, out_smt_root) = if !ids_distinct || !spends_ok || !overwrite_ok {
        (false, w.in_smt_root)
    } else {
        let u = bip30::BlockUpdate {
            coinbase_txid: cb_txid,
            coinbase_outputs: cb_outputs,
            absence_proof: w.smt.absence_proof.clone(),
            overwrite: w.smt.smt_overwrite,
            spends: w.smt.spends.iter()
                .map(|s| bip30::Spend { coinbase_txid: s.coinbase_txid,
                                        current_count: s.current_count, proof: s.proof.clone() })
                .collect(),
        };
        match bip30::apply_block(&w.in_smt_root, &u) {
            Ok(r) => (true, r),
            // Fails closed: a duplicate of an unspent coinbase, a bad proof, or a spend from an empty
            // count all land here, and `block_valid` gates the commit, so no receipt is produced.
            Err(e) => { env::log(&format!("BIP30 transition rejected the block: {e:?}")); (false, w.in_smt_root) }
        }
    };

    // #4: run the coinbase through real Core CheckTransaction (bad-cb-length, per-output MoneyRange,
    // duplicate-input, value-sum range) and assert it is structurally a coinbase. Previously the
    // coinbase only reached subsidy/BIP34/witness-commitment checks and never CheckTransaction, so a
    // malformed coinbase (or one whose output sum overflows i64 inside coinbase_value) could slip
    // through. Empty prevouts blob = a serialized empty CTxOut vector (CompactSize 0 == one 0x00 byte).
    let cb_empty_prevouts = [0u8; 1];
    let mut cb_fee: i64 = 0;
    let cb_struct_ok = unsafe {
        check_tx(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32,
            cb_empty_prevouts.as_ptr(), cb_empty_prevouts.len() as u32, &mut cb_fee as *mut i64)
    } == 1;
    let cb_is_coinbase = unsafe { is_coinbase_tx(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32) } == 1;
    if !cb_struct_ok || !cb_is_coinbase { all_ok = false; }

    let coinbase_val = unsafe { coinbase_value(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32) };
    let subsidy = unsafe { block_subsidy(w.height) };
    // G5: block-level MoneyRange on the accumulated fees + no-overflow on subsidy+fees (Core's
    // bad-txns-accumulated-fee-outofrange / the CheckTransaction MoneyRange applied block-wide). Each
    // per-tx fee is already ≥0 and MoneyRange-bounded in check_tx, but assert the block total explicitly
    // rather than relying on anchor-integrity induction, and compute the coinbase bound in i128 so a
    // maliciously large fee sum cannot overflow the i64 `subsidy + total_fee`.
    const MAX_MONEY: i64 = 21_000_000 * 100_000_000;
    let money_ok = (0..=MAX_MONEY).contains(&total_fee)
        && (0..=MAX_MONEY).contains(&subsidy)
        && coinbase_val >= 0
        && (subsidy as i128 + total_fee as i128) <= MAX_MONEY as i128;
    let subsidy_ok = money_ok && coinbase_val <= subsidy + total_fee;

    // Block weight (from tx serialization) + FULL sigop cost (legacy + P2SH + witness, real Core).
    let mut total_weight: i64 = 0;
    let mut total_sigops: i64 = 0;
    let weight_of = |raw: &[u8]| -> i64 {
        let (mut wt, mut _so): (i64, i64) = (0, 0);
        unsafe { tx_wu_sigops(raw.as_ptr(), raw.len() as u32, &mut wt, &mut _so) };
        wt
    };
    // coinbase: no prevouts.
    total_weight += weight_of(&w.coinbase_tx);
    total_sigops += unsafe { tx_full_sigops(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, core::ptr::null(), 0, flags) };
    for inp in &w.inputs {
        if inp.tx_first != 1 {
            continue; // weight + sigops are per-tx; count once (on the tx's first input)
        }
        let ti = (inp.tx_idx as usize).min(w.txs.len() - 1);
        let (raw_tx, prevouts) = (&w.txs[ti].0, &w.tx_prevouts[ti].0);
        total_weight += weight_of(raw_tx);
        total_sigops += unsafe {
            tx_full_sigops(raw_tx.as_ptr(), raw_tx.len() as u32, prevouts.as_ptr(), prevouts.len() as u32, flags)
        };
    }
    // Core's GetBlockWeight also weighs the 80-byte header and the tx-count varint (non-witness data, so
    // ×WITNESS_SCALE_FACTOR) — `4*(80 + CompactSize(ntx))` — on top of the per-tx weights. Without it a
    // block could sit up to ~324 WU over the limit while Core rejects it (F2, round-5 audit).
    let ntx = w.txids.len();
    let cs: i64 = if ntx < 0xfd { 1 } else if ntx <= 0xffff { 3 } else if ntx <= 0xffff_ffff { 5 } else { 9 };
    total_weight += 4 * (80 + cs);
    let weight_ok = total_weight <= MAX_BLOCK_WEIGHT;
    let sigops_ok = total_sigops <= MAX_BLOCK_SIGOPS_COST;

    // The coinbase tx must also be final (absolute locktime, MTP-aware post-BIP113).
    let cb_final = unsafe {
        is_final_tx(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, w.height as i64, lock_time as i64)
    } == 1;
    if !cb_final {
        all_ok = false;
    }

    BlockResult {
        script_results, tx_checks, coin_leaves, total_fee, pow_ok, merkle_ok,
        coinbase_val, subsidy, subsidy_ok, all_ok, root_matches, weight_ok, sigops_ok, witness_ok, bip34_ok, bip30_ok, out_smt_root,
        tip_hash: block_hash,
        nbits: u32::from_le_bytes(w.header[72..76].try_into().unwrap()),
        block_time: u32::from_le_bytes(w.header[68..72].try_into().unwrap()),
        root_next_roots: stump.normalized(),
        root_next_leaves: stump.num_leaves,
    }
}

// Mode 1: commit the full per-block report (used for standalone block validation + debugging).
fn block_proof() {
    let w: BlockWitness = env::read();
    assert!(w.header.len() == 80, "block header must be 80 bytes"); // guard before slicing header[68..72]
    let block_time = u32::from_le_bytes(w.header[68..72].try_into().unwrap());
    let r = validate_block(&w, block_time, None); // standalone: MTP fallback = block time
    env::commit(&BlockOutput {
        script_results: r.script_results,
        tx_checks: r.tx_checks,
        coin_leaves: r.coin_leaves,
        total_fee: r.total_fee,
        pow_ok: r.pow_ok,
        merkle_ok: r.merkle_ok,
        coinbase_val: r.coinbase_val,
        subsidy: r.subsidy,
        subsidy_ok: r.subsidy_ok,
        all_ok: r.all_ok,
        root_matches: r.root_matches,
    });
}

// The recursive chain proof's state = the committed journal. A ChainState proof attests: the chain
// from the anchor up to `tip_hash` is fully valid, the UTXO set is exactly `utxo_*`, and cumulative
// PoW is `cum_work`. Folding one block advances it.
#[derive(Serialize, Deserialize, Clone)]
struct ChainState {
    kind: u32,          // H8: == KIND_CHAIN (domain tag; asserted by every consumer)
    tip_hash: [u8; 32],
    utxo_roots: Vec<Option<[u8; 32]>>,
    utxo_leaves: u64,
    cum_work: [u8; 32], // cumulative chainwork (256-bit accumulator)
    height: u32,
    prev_nbits: u32,    // difficulty of the tip block (for the retarget rule)
    prev_time: u32,     // timestamp of the tip block
    epoch_start: u32,   // timestamp of the first block in the current retarget epoch
    recent_times: Vec<u32>, // timestamps of the last ≤11 blocks (for median-time-past)
    // S5: dsha256 of the base-anchor journal this chain bottomed out at (set once in the is_base==1 step,
    // then carried forward unchanged). Makes a ChainState receipt self-authenticating: the verifier pins
    // it to the genesis-anchor digest, exactly as the range track pins RangeState's in-boundary to genesis.
    // Without it a mode-2/5 receipt built on a FABRICATED anchor (arbitrary height/UTXO/work/easy nbits)
    // is journal-indistinguishable from a genesis-anchored one.
    anchor_id: [u8; 32],
    self_id: [u32; 8],  // S1: the guest image id this proof recursed against (verifier asserts ==METHOD_ID)
}

// A RANGE proof's committed state: blocks [lo..=hi] are all valid, GIVEN the "in" boundary (the state
// just before block lo), producing the "out" boundary (after hi). Range proofs are self-contained —
// each single-block proof takes its in-boundary as input (from the bridge pass), so they prove in
// PARALLEL; a fold verifies two adjacent range receipts and checks the in/out boundaries meet. The
// top-level verifier pins the leftmost in-boundary to the genesis anchor, binding the whole tree.
#[derive(Serialize, Deserialize)]
struct RangeState {
    kind: u32,          // H8: == KIND_RANGE (domain tag)
    lo: u32, hi: u32,
    // "in" boundary — the chain state just before block lo (must equal the previous range's "out").
    in_tip_hash: [u8; 32],
    in_roots: Vec<Option<[u8; 32]>>, in_leaves: u64,
    in_nbits: u32, in_time: u32, in_epoch_start: u32, in_recent: Vec<u32>,
    // Coinbase-SMT root at the in boundary (#54) — the BIP30 non-membership state a utreexo Stump
    // cannot express. Carried in the journal for the same reason the UTXO roots are: the next range
    // must inherit it unchanged, and a fold that did not check it could join two ranges whose BIP30
    // state disagrees.
    in_smt_root: [u8; 32],
    // "out" boundary — the chain state just after block hi.
    out_tip_hash: [u8; 32],
    out_roots: Vec<Option<[u8; 32]>>, out_leaves: u64,
    out_nbits: u32, out_time: u32, out_epoch_start: u32, out_recent: Vec<u32>,
    out_smt_root: [u8; 32],
    range_work: [u8; 32], // total chainwork of blocks lo..=hi (256-bit LE)
    self_id: [u32; 8],
}

// 256-bit little-endian addition: a += b (chainwork accumulation across two ranges).
fn add256(a: &mut [u8; 32], b: &[u8; 32]) {
    let mut carry = 0u16;
    for i in 0..32 {
        let s = a[i] as u16 + b[i] as u16 + carry;
        a[i] = s as u8;
        carry = s >> 8;
    }
}

// Decode a ChainState/RangeState from committed journal bytes (LE u32 words).
fn decode_words<T: for<'de> Deserialize<'de>>(journal: &[u8]) -> T {
    let words: Vec<u32> = journal.chunks_exact(4)
        .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
    risc0_zkvm::serde::from_slice(&words).expect("decode journal")
}

// Median-time-past: median of the last (≤11) block timestamps.
fn median_time_past(times: &[u32]) -> u32 {
    let mut v = times.to_vec();
    v.sort_unstable();
    if v.is_empty() { 0 } else { v[v.len() / 2] }
}

// Mode 2: the IVC transition F(prev_state, block) → next_state. Validates the block, enforces chain
// linkage (prevhash + UTXO-root carry), accumulates work, advances the tip. A proof only exists if
// everything holds (panic ⇒ no proof).
//
// RECURSION HOOK: when `is_base == 0`, the previous state must itself be a valid ChainState proof of
// THIS guest. That binding is `env::verify(self_image_id, prev_journal)` — RISC0 composition, the
// host discharging it with the previous step's receipt (`add_assumption`). Cryptographic recursion
// proving is resource-heavy (deferred to the big box); the transition logic below is what's validated
// here in execute over real consecutive blocks. (`is_base == 1` trusts the anchor checkpoint.)
fn chain_step() {
    let prev_journal: Vec<u8> = env::read(); // the previous chain proof's committed journal bytes
    let w: BlockWitness = env::read();
    let is_base: u32 = env::read();
    let self_id: [u32; 8] = env::read(); // this guest's own image id (host passes METHOD_ID)
    if is_base == 0 {
        // Composition: a receipt with (self_id, prev_journal) must exist — the previous chain proof.
        env::verify(self_id, &prev_journal).expect("previous chain proof invalid");
    }
    // Decode prev ChainState from the authoritative journal bytes (LE u32 words).
    let words: Vec<u32> = prev_journal
        .chunks_exact(4)
        .map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect();
    let prev: ChainState = risc0_zkvm::serde::from_slice(&words).expect("decode prev chain state");
    assert!(prev.kind == KIND_CHAIN, "chain step: prev journal is not a ChainState (domain tag)"); // H8
    // S1: chain the image-id constraint down — the prev proof must have recursed against the SAME id.
    // With the verifier asserting the FINAL self_id == METHOD_ID, this forces every level to METHOD_ID.
    if is_base == 0 {
        assert!(prev.self_id == self_id, "recursion image-id mismatch");
    }

    // #1: the block's height (which selects the script FLAGS and the coinbase SUBSIDY schedule) is
    // host-supplied in `w.height`. It must equal the real chain height prev.height+1, or a prover sets
    // w.height=1 to turn every soft-fork flag off (segwit/taproot outputs become anyone-can-spend) and
    // inflate the subsidy to 50 BTC, while the journal still commits the true height. Bind them.
    assert!(w.height == prev.height + 1, "chain step: block height {} != chain height {}", w.height, prev.height + 1);

    // S5: record the anchor this chain bottomed out at. In the base step the anchor IS `prev_journal`
    // (trusted, un-verified) — commit its digest so the verifier can pin it to genesis. In a recursive
    // step carry the verified prev's anchor_id forward unchanged (prev is a real receipt of this guest).
    let anchor_id = if is_base == 1 { dsha256(&prev_journal) } else { prev.anchor_id };

    // BIP113/BIP68 use the PREVIOUS block's median-time-past (median of the last ≤11 timestamps).
    let prev_mtp = median_time_past(&prev.recent_times);
    let r = validate_block(&w, prev_mtp, None);
    let block_valid = r.all_ok && r.root_matches && r.pow_ok && r.merkle_ok && r.subsidy_ok
        && r.weight_ok && r.sigops_ok && r.witness_ok && r.bip34_ok && r.bip30_ok;
    if !block_valid {
        env::log(&format!("FLAGS all_ok={} root_matches={} pow_ok={} merkle_ok={} subsidy_ok={} weight_ok={} sigops_ok={} witness_ok={} bip34_ok={} bip30_ok={}",
            r.all_ok, r.root_matches, r.pow_ok, r.merkle_ok, r.subsidy_ok, r.weight_ok, r.sigops_ok, r.witness_ok, r.bip34_ok, r.bip30_ok));
    }
    let prevhash_ok = w.header[4..36] == prev.tip_hash[..];
    let carry_ok = normalize(w.root_prev.roots.clone()) == normalize(prev.utxo_roots.clone())
        && w.root_prev.num_leaves == prev.utxo_leaves;

    // Difficulty retarget: between epochs nBits is fixed; on an epoch boundary it must equal the
    // value the real Core formula computes from the epoch's timespan.
    let height = prev.height + 1;
    let expected_nbits = if height % RETARGET_INTERVAL != 0 {
        prev.prev_nbits
    } else {
        unsafe { calc_next_bits(prev.prev_nbits, prev.epoch_start as i64, prev.prev_time as i64) }
    };
    let retarget_ok = r.nbits == expected_nbits;

    // "time-too-old" (Core ContextualCheckBlockHeader): a block's timestamp must exceed the
    // median-time-past of the previous 11 blocks. (The 2-hour future limit is node-local — it depends
    // on wall-clock adjusted time — so it is NOT a provable consensus rule and is intentionally omitted.)
    let time_ok = r.block_time > prev_mtp;

    assert!(
        block_valid && prevhash_ok && carry_ok && retarget_ok && time_ok,
        "chain step: block_valid={} prevhash_ok={} carry_ok={} retarget_ok={} time_ok={}",
        block_valid, prevhash_ok, carry_ok, retarget_ok, time_ok
    );

    let mut cum = prev.cum_work;
    unsafe { add_work(cum.as_mut_ptr(), r.nbits) };
    // The epoch's first-block time resets at each retarget boundary.
    let epoch_start = if height % RETARGET_INTERVAL == 0 { r.block_time } else { prev.epoch_start };
    // Advance the median-time-past window (keep the last 11 timestamps).
    let mut recent_times = prev.recent_times.clone();
    recent_times.push(r.block_time);
    if recent_times.len() > 11 {
        let excess = recent_times.len() - 11;
        recent_times.drain(0..excess);
    }

    env::commit(&ChainState {
        kind: KIND_CHAIN,
        tip_hash: r.tip_hash,
        utxo_roots: r.root_next_roots,
        utxo_leaves: r.root_next_leaves,
        cum_work: cum,
        height,
        prev_nbits: r.nbits,
        prev_time: r.block_time,
        epoch_start,
        recent_times,
        anchor_id,
        self_id,
    });
}

// Mode 6: prove ONE block as a self-contained range [N..N]. The in-boundary (state before block N) is
// host-supplied input (from the cheap bridge pass); block N is validated against it exactly as in
// chain_step, and the out-boundary is computed. NO env::verify — independent, so blocks prove in
// parallel. Soundness comes from the fold tree checking each boundary meets, back to the genesis anchor.
fn prove_range() {
    let in_tip_hash: [u8; 32] = env::read();
    let in_roots: Vec<Option<[u8; 32]>> = env::read();
    let in_leaves: u64 = env::read();
    let in_nbits: u32 = env::read();
    let in_time: u32 = env::read();
    let in_epoch_start: u32 = env::read();
    let in_recent: Vec<u32> = env::read();
    let w: BlockWitness = env::read();
    let self_id: [u32; 8] = env::read();

    let prev_mtp = median_time_past(&in_recent);
    let r = validate_block(&w, prev_mtp, None);
    let block_valid = r.all_ok && r.root_matches && r.pow_ok && r.merkle_ok && r.subsidy_ok
        && r.weight_ok && r.sigops_ok && r.witness_ok && r.bip34_ok && r.bip30_ok;
    let prevhash_ok = w.header[4..36] == in_tip_hash[..];
    let carry_ok = normalize(w.root_prev.roots.clone()) == normalize(in_roots.clone())
        && w.root_prev.num_leaves == in_leaves;
    let height = w.height;
    let expected_nbits = if height % RETARGET_INTERVAL != 0 { in_nbits }
        else { unsafe { calc_next_bits(in_nbits, in_epoch_start as i64, in_time as i64) } };
    let retarget_ok = r.nbits == expected_nbits;
    let time_ok = r.block_time > prev_mtp; // time-too-old (Core ContextualCheckBlockHeader)
    assert!(block_valid && prevhash_ok && carry_ok && retarget_ok && time_ok,
        "prove_range block {}: bv={} ph={} carry={} rt={} time={}", height, block_valid, prevhash_ok, carry_ok, retarget_ok, time_ok);

    let mut range_work = [0u8; 32];
    unsafe { add_work(range_work.as_mut_ptr(), r.nbits) };
    let out_epoch_start = if height % RETARGET_INTERVAL == 0 { r.block_time } else { in_epoch_start };
    let mut out_recent = in_recent.clone();
    out_recent.push(r.block_time);
    if out_recent.len() > 11 { let e = out_recent.len() - 11; out_recent.drain(0..e); }

    // #54 — the coinbase-SMT root actually ADVANCES now: `validate_block` ran the BIP30 transition
    // against `in_smt_root` and `block_valid` (asserted above) already required it to succeed, so
    // reaching here means the block satisfied BIP30 by proof rather than by argument.
    let in_smt_root = w.in_smt_root;
    let out_smt_root = r.out_smt_root;
    env::commit(&RangeState {
        kind: KIND_RANGE,
        lo: height, hi: height,
        in_tip_hash, in_roots, in_leaves, in_nbits, in_time, in_epoch_start, in_recent,
        in_smt_root,
        out_tip_hash: r.tip_hash, out_roots: r.root_next_roots, out_leaves: r.root_next_leaves,
        out_nbits: r.nbits, out_time: r.block_time, out_epoch_start, out_recent,
        out_smt_root,
        range_work, self_id,
    });
}

// Mode 7: fold two ADJACENT range proofs (left [.. .hi], right [hi+1 ..]) into one. Verifies both
// receipts (composition) and checks the boundaries meet: tip-hash linkage, UTXO-root carry, and
// difficulty/MTP-window continuity — so difficulty and the coin set can't be forged across the seam.
// Parallel + log-depth over a range: the tree-fold that replaces the sequential chain for backfill.
fn fold_range() {
    let self_id: [u32; 8] = env::read();
    let l_journal: Vec<u8> = env::read();
    let r_journal: Vec<u8> = env::read();
    env::verify(self_id, &l_journal).expect("left range proof invalid");
    env::verify(self_id, &r_journal).expect("right range proof invalid");
    let l: RangeState = decode_words(&l_journal);
    let rr: RangeState = decode_words(&r_journal);
    assert!(l.kind == KIND_RANGE && rr.kind == KIND_RANGE, "fold: journal is not a RangeState (domain tag)"); // H8

    assert!(l.self_id == self_id && rr.self_id == self_id, "fold: image-id mismatch");
    assert!(l.hi + 1 == rr.lo, "fold: ranges [{}..{}] and [{}..{}] not adjacent", l.lo, l.hi, rr.lo, rr.hi);
    assert!(l.out_tip_hash == rr.in_tip_hash, "fold: tip-hash linkage broken at seam");
    assert!(normalize(l.out_roots.clone()) == normalize(rr.in_roots.clone()) && l.out_leaves == rr.in_leaves,
        "fold: UTXO-root carry broken at seam");
    assert!(l.out_nbits == rr.in_nbits && l.out_time == rr.in_time
        && l.out_epoch_start == rr.in_epoch_start && l.out_recent == rr.in_recent,
        "fold: difficulty/MTP context discontinuous at seam");
    // #54: the BIP30 state must carry across the seam exactly as the UTXO set does. Without this a
    // fold could join a left range that spent a coinbase to zero with a right range that still
    // believes it unspent — or the reverse — and the joined proof would attest a BIP30 history that
    // neither half proved. No normalisation: the SMT root is a single hash with no padding ambiguity,
    // unlike the utreexo root vector above.
    assert!(l.out_smt_root == rr.in_smt_root, "fold: coinbase-SMT (BIP30) state broken at seam");

    let mut range_work = l.range_work;
    add256(&mut range_work, &rr.range_work);
    env::commit(&RangeState {
        kind: KIND_RANGE,
        lo: l.lo, hi: rr.hi,
        in_tip_hash: l.in_tip_hash, in_roots: l.in_roots, in_leaves: l.in_leaves,
        in_nbits: l.in_nbits, in_time: l.in_time, in_epoch_start: l.in_epoch_start, in_recent: l.in_recent,
        in_smt_root: l.in_smt_root,
        out_tip_hash: rr.out_tip_hash, out_roots: rr.out_roots, out_leaves: rr.out_leaves,
        out_nbits: rr.out_nbits, out_time: rr.out_time, out_epoch_start: rr.out_epoch_start, out_recent: rr.out_recent,
        out_smt_root: rr.out_smt_root,
        range_work, self_id,
    });
}

// Mode 3: validate a mix of real spends with the CORRECT per-height consensus flags + full sigop
// cost (exercises segwit/P2SH/taproot + witness sigops on real data; PoW/merkle proven on block 170).
#[derive(Deserialize)]
struct SpendCheck {
    raw_tx: Vec<u8>,
    prevouts: Vec<u8>,
    block_height: u32,
}
#[derive(Serialize)]
struct SpendResult {
    script: i32,
    sigops: i64,
    tx_check: i32,
    flags: u32,
}
/// DEBUG EXERCISER — NOT a consensus path, and it does not prove a block.
///
/// It commits a bare `Vec<SpendResult>`: no `RangeState`, no `ChainState`, no accumulator root, no
/// height binding. A receipt from this mode therefore attests nothing about any chain and cannot be
/// folded, submitted or verified as a range — which is why the hardcoded metadata below is safe.
///
/// It is documented rather than deleted because the hardcoding is a TRIPWIRE: `coin_height` is fixed
/// at 700_000, `coin_is_coinbase` and `coin_mtp` at 0, and the flag-exception block hash at all-zero.
/// Anyone reaching for this believing it exercises real coin metadata would get script flags for a
/// height that has nothing to do with their input, coinbase maturity never enforced, and every
/// BIP68 time-based lock evaluated against MTP 0. Same shape as the round-9 harnesses that printed
/// results without asserting them (see SECURITY.md): it looks like it is checking more than it is.
///
/// Raised by external review 2026-08-01 (opencode, L-3); see #59.
fn multi_check() {
    let n: u32 = env::read();
    let mut out: Vec<SpendResult> = Vec::with_capacity(n as usize);
    for _ in 0..n {
        let s: SpendCheck = env::read();
        let flags = block_script_flags(s.block_height, &[0u8; 32]); // isolated spend check: no real block (non-exception)
        let mut leaf = [0u8; 32];
        let script = unsafe {
            verify_input(
                s.raw_tx.as_ptr(), s.raw_tx.len() as u32, 0,
                s.prevouts.as_ptr(), s.prevouts.len() as u32, flags,
                // coin_height / coin_is_coinbase / coin_mtp — PLACEHOLDERS, see the doc comment.
                700_000, 0, 0, leaf.as_mut_ptr(),
            )
        };
        let sigops = unsafe {
            tx_full_sigops(s.raw_tx.as_ptr(), s.raw_tx.len() as u32, s.prevouts.as_ptr(), s.prevouts.len() as u32, flags)
        };
        let mut fee = 0i64;
        let tx_check = unsafe {
            check_tx(s.raw_tx.as_ptr(), s.raw_tx.len() as u32, s.prevouts.as_ptr(), s.prevouts.len() as u32, &mut fee)
        };
        out.push(SpendResult { script, sigops, tx_check, flags });
    }
    env::commit(&out);
}

// ---- Segmentation: chunk (map) + aggregate (reduce) ----
#[derive(Serialize, Deserialize)]
struct ChunkOut { kind: u32, all_valid: bool, binds: Vec<[u8; 32]> }

// Mode 4: prove a BATCH of inputs' scripts (the expensive VerifyScript). Parallelisable across a
// block; commits the coin leaves it verified so the aggregation can bind them to the block's inputs.
fn chunk_prove() {
    let height: u32 = env::read();
    let block_hash: [u8; 32] = env::read(); // real block hash — needed for flag exceptions; a wrong
    let flags = block_script_flags(height, &block_hash); // hash yields wrong flags -> aggregate bind mismatch
    let n: u32 = env::read();
    let mut binds: Vec<[u8; 32]> = Vec::with_capacity(n as usize);
    let mut all_valid = true;
    // #135: the byte payloads arrive as raw bytes via read_slice, not as serde `Vec<u8>`. Serde walks
    // the word stream a byte at a time — measured at ~147 cycles/byte, half this guest's entire cost on
    // a transaction-heavy chunk. Each payload is padded to a word by the host so the u32 reads stay
    // aligned; we truncate back to the declared length. Nothing about WHAT is proven changes: the same
    // bytes reach verify_input and input_bind commits the same digest.
    fn read_bytes(len: u32) -> Vec<u8> {
        let mut v = vec![0u8; (len as usize).div_ceil(4) * 4];
        env::read_slice(&mut v);
        v.truncate(len as usize);
        v
    }
    for _ in 0..n {
        let tx_len: u32 = env::read();
        let prevouts_len: u32 = env::read();
        let input_idx: u32 = env::read();
        let coin_height: u32 = env::read();
        let coin_is_coinbase: u32 = env::read();
        let coin_mtp: u32 = env::read();
        let raw_tx = read_bytes(tx_len);
        let prevouts = read_bytes(prevouts_len);
        let mut leaf = [0u8; 32];
        let r = unsafe {
            verify_input(
                raw_tx.as_ptr(), raw_tx.len() as u32, input_idx,
                prevouts.as_ptr(), prevouts.len() as u32, flags,
                coin_height, coin_is_coinbase, coin_mtp, leaf.as_mut_ptr(),
            )
        };
        if r != 1 { all_valid = false; }
        // Bind exactly what was verified (tx bytes, input idx, prevouts, coin metadata, flags) so the
        // aggregation can prove the block's input is the one this chunk validated — see input_bind (#2).
        binds.push(input_bind(&raw_tx, input_idx, &prevouts, coin_height, coin_is_coinbase, coin_mtp, flags));
    }
    env::commit(&ChunkOut { kind: KIND_CHUNK, all_valid, binds });
}

// Mode 5: aggregate K chunk proofs into a block/chain proof. env::verify each chunk (composition),
// concatenate their leaves, then do the CHEAP sequential parts (accumulator transition + block
// checks) via validate_block with scripts sourced from the chunks. Same output as chain_step.
fn aggregate() {
    let self_id: [u32; 8] = env::read();
    let k: u32 = env::read();
    let mut all_binds: Vec<[u8; 32]> = Vec::new();
    let mut chunks_ok = true;
    for _ in 0..k {
        let cj: Vec<u8> = env::read();
        env::verify(self_id, &cj).expect("chunk proof invalid");
        let words: Vec<u32> = cj.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        let out: ChunkOut = risc0_zkvm::serde::from_slice(&words).expect("decode chunk");
        assert!(out.kind == KIND_CHUNK, "aggregate: assumption is not a ChunkOut (domain tag)"); // H8
        if !out.all_valid { chunks_ok = false; }
        all_binds.extend(out.binds);
    }
    let prev_journal: Vec<u8> = env::read();
    let w: BlockWitness = env::read();
    let is_base: u32 = env::read();
    if is_base == 0 { env::verify(self_id, &prev_journal).expect("previous chain proof invalid"); }
    let words: Vec<u32> = prev_journal.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
    let prev: ChainState = risc0_zkvm::serde::from_slice(&words).expect("decode prev");
    assert!(prev.kind == KIND_CHAIN, "aggregate: prev journal is not a ChainState (domain tag)"); // H8
    // S1: chunk verification above AND the prev-chain verification both used `self_id`; committing it
    // + asserting prev.self_id==self_id (below) + the verifier checking final==METHOD_ID forces every
    // recursion (chunks and chain) to the real guest.
    if is_base == 0 {
        assert!(prev.self_id == self_id, "recursion image-id mismatch");
    }
    // #1: bind the block's height to the real chain height (same reason as chain_step — otherwise the
    // segmented path validates flags/subsidy at an attacker-chosen height). Also closes half of #2: the
    // per-input binding digest folds in `flags = block_script_flags(w.height)`, now pinned to the height.
    assert!(w.height == prev.height + 1, "aggregate: block height {} != chain height {}", w.height, prev.height + 1);
    // S5: same anchor binding as chain_step — base step commits the trusted anchor's digest, recursive
    // step carries the verified prev's anchor_id forward.
    let anchor_id = if is_base == 1 { dsha256(&prev_journal) } else { prev.anchor_id };
    let prev_mtp = median_time_past(&prev.recent_times);

    let r = validate_block(&w, prev_mtp, Some((&all_binds, chunks_ok)));
    let block_valid = r.all_ok && r.root_matches && r.pow_ok && r.merkle_ok && r.subsidy_ok && r.weight_ok && r.sigops_ok && r.witness_ok && r.bip34_ok && r.bip30_ok;
    let prevhash_ok = w.header[4..36] == prev.tip_hash[..];
    let carry_ok = normalize(w.root_prev.roots.clone()) == normalize(prev.utxo_roots.clone()) && w.root_prev.num_leaves == prev.utxo_leaves;
    let height = prev.height + 1;
    let expected_nbits = if height % RETARGET_INTERVAL != 0 { prev.prev_nbits } else { unsafe { calc_next_bits(prev.prev_nbits, prev.epoch_start as i64, prev.prev_time as i64) } };
    let retarget_ok = r.nbits == expected_nbits;
    let time_ok = r.block_time > prev_mtp; // time-too-old (Core ContextualCheckBlockHeader)
    assert!(block_valid && prevhash_ok && carry_ok && retarget_ok && time_ok,
        "aggregate: bv={} ph={} carry={} rt={} time={}", block_valid, prevhash_ok, carry_ok, retarget_ok, time_ok);
    let mut cum = prev.cum_work;
    unsafe { add_work(cum.as_mut_ptr(), r.nbits) };
    let epoch_start = if height % RETARGET_INTERVAL == 0 { r.block_time } else { prev.epoch_start };
    let mut recent_times = prev.recent_times.clone();
    recent_times.push(r.block_time);
    if recent_times.len() > 11 { let e = recent_times.len() - 11; recent_times.drain(0..e); }
    env::commit(&ChainState {
        kind: KIND_CHAIN,
        tip_hash: r.tip_hash, utxo_roots: r.root_next_roots, utxo_leaves: r.root_next_leaves,
        cum_work: cum, height, prev_nbits: r.nbits, prev_time: r.block_time, epoch_start, recent_times, anchor_id, self_id,
    });
}

fn main() {
    // Run C++ static constructors ONCE (Core's global tagged-hash midstates) — fixed cost per run.
    unsafe { __libc_init_array() };
    // Pin every Rust-side consensus literal to Core's own compiled value before doing any work.
    assert_core_constants();
    let mode: u32 = env::read();
    match mode {
        1 => block_proof(),
        2 => chain_step(),
        3 => multi_check(),
        4 => chunk_prove(),
        5 => aggregate(),
        6 => prove_range(),
        7 => fold_range(),
        8 => test_locks(),
        9 => test_merkle(),
        _ => panic!("unknown guest mode {mode}"),
    }
}

// Mode 8: isolated exerciser for the real Core-derived maturity/BIP68 relative-lock check
// (`check_input_locks`). Used by the host `test-locks` command to drive the time-based branch with
// real MTP numbers (no block; the check only reads tx.version + vin[idx].nSequence + the two MTPs).
fn test_locks() {
    let tx: Vec<u8> = env::read();
    let input_idx: u32 = env::read();
    let coin_height: u32 = env::read();
    let coin_is_coinbase: u32 = env::read();
    let coin_mtp: u32 = env::read();
    let spend_height: u32 = env::read();
    let spend_mtp: u32 = env::read();
    let rc = unsafe {
        check_input_locks(tx.as_ptr(), tx.len() as u32, input_idx,
            coin_height, coin_is_coinbase, coin_mtp, spend_height, spend_mtp)
    };
    env::commit(&rc);
}

// Mode 9: isolated exerciser for the merkle-root computation incl. the CVE-2012-2459 mutation flag
// (COV-2). Reads a flat list of n*32-byte txids; commits (root, mutated) exactly as the real Core
// ComputeMerkleRoot reports them — so a duplicate-txid malleation is caught (mutated==1).
fn test_merkle() {
    let flat: Vec<u8> = env::read();
    let n = (flat.len() / 32) as u32;
    let mut root = [0u8; 32];
    let mut mutated = 0u8;
    unsafe { merkle_root(flat.as_ptr(), n, root.as_mut_ptr(), &mut mutated) };
    env::commit(&(root, mutated));
}

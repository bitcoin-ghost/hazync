// Guest: verify ONE transaction input using Bitcoin Core's REAL VerifyScript + interpreter + sighash
// + libsecp256k1 — all compiled into the guest via build.rs.
use risc0_zkvm::guest::env;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

mod utreexo;

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

// Consensus VerifyScript flags active at a given mainnet height (soft-fork activation heights).
// This is how BIP66/65/112(CSV)/147/segwit/taproot get enforced — through VerifyScript.
// Core mainnet script_flag_exceptions (chainparams.cpp), in internal (dsha256(header)) byte order.
// One historical block violated BIP16 (runs with NO script flags) and one violated Taproot (runs
// without TAPROOT). Matching Core here is REQUIRED — otherwise the from-genesis prover stalls on these
// canonical blocks (guest rejects a block Core accepts).
const BIP16_EXCEPTION: [u8; 32] = [0x22, 0x9c, 0x4f, 0xac, 0x88, 0xba, 0xb1, 0x94, 0xeb, 0x08, 0xf1, 0xa5, 0x28, 0xcc, 0x30, 0x8d, 0xed, 0x23, 0x97, 0xf4, 0xf4, 0xeb, 0x6e, 0x75, 0xdc, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00];
const TAPROOT_EXCEPTION: [u8; 32] = [0xad, 0x95, 0xe3, 0xa1, 0x5e, 0xe5, 0xff, 0xd5, 0x85, 0xc5, 0xe8, 0x1d, 0x44, 0xb5, 0x6a, 0x98, 0x1e, 0x84, 0x2d, 0x5b, 0xc3, 0x14, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00];

// BIP30 grandfathered duplicate-coinbase blocks (internal/dsha256(header) order). Each reuses an
// earlier still-unspent coinbase's outpoint; pre-BIP30-enforcement Core OVERWRITES the old coin (it
// becomes unspendable). At exactly these two blocks the guest deletes the superseded coinbase leaf so
// it can't linger spendable (F3). 91842 duplicates 91812's coinbase; 91880 duplicates 91722's.
// (BIP34, enforced from 227931, makes coinbases unique thereafter, so no later duplicate can occur.)
const BIP30_OVERWRITE_A: [u8; 32] = [0xec, 0xca, 0xe0, 0x00, 0xe3, 0xc8, 0xe4, 0xe0, 0x93, 0x93, 0x63, 0x60, 0x43, 0x1f, 0x3b, 0x76, 0x03, 0xc5, 0x63, 0xc1, 0xff, 0x61, 0x81, 0x39, 0x0a, 0x4d, 0x0a, 0x00, 0x00, 0x00, 0x00, 0x00]; // block 91842
const BIP30_OVERWRITE_B: [u8; 32] = [0x21, 0xd7, 0x7c, 0xcb, 0x4c, 0x08, 0x38, 0x6a, 0x04, 0xac, 0x01, 0x96, 0xae, 0x10, 0xf6, 0xa1, 0xd2, 0xc2, 0xa3, 0x77, 0x55, 0x8c, 0xa1, 0x90, 0xf1, 0x43, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00]; // block 91880

// Consensus script flags for a block — replicates Core's GetBlockScriptFlags (validation.cpp) EXACTLY:
// the base P2SH|WITNESS|TAPROOT is ALWAYS on (retroactive to genesis) except for the two exception
// blocks above (which override it), then DERSIG/CLTV/CSV/NULLDUMMY are OR'd in at their buried-deployment
// heights. `block_hash` is the guest-computed dsha256(header) (internal order), so the exception override
// cannot be forged — a wrong hash fails PoW (monolithic) or the H2 bind digest (segmented). Height-gating
// the base flags (the previous behaviour) was both too lenient below the gate (accept-invalid, H-S4) and
// wrong on the exception blocks (reject-valid, H-S1).
fn block_script_flags(height: u32, block_hash: &[u8; 32]) -> u32 {
    const P2SH: u32 = 1 << 0; const DERSIG: u32 = 1 << 2; const NULLDUMMY: u32 = 1 << 4;
    const CLTV: u32 = 1 << 9; const CSV: u32 = 1 << 10; const WITNESS: u32 = 1 << 11; const TAPROOT: u32 = 1 << 17;
    let mut f = P2SH | WITNESS | TAPROOT;
    if block_hash == &BIP16_EXCEPTION { f = 0; }
    else if block_hash == &TAPROOT_EXCEPTION { f = P2SH | WITNESS; }
    if height >= 363_725 { f |= DERSIG; }               // BIP66Height (DERSIG)
    if height >= 388_381 { f |= CLTV; }                 // BIP65Height (CHECKLOCKTIMEVERIFY)
    if height >= 419_328 { f |= CSV; }                  // CSVHeight (CHECKSEQUENCEVERIFY)
    if height >= 481_824 { f |= NULLDUMMY; }            // SegwitHeight (BIP147 NULLDUMMY)
    f
}

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

// RISC0-accelerated secp256k1 ECDSA verify (k256, using the EC precompile). NOT on the sound-build
// consensus path: reachable only when patch 0003 is applied (it is not — provision applies 0001+0002
// only) or from the compiled-out bench. Kept linked for the acceleration experiment (ACCELERATION.md).
// msg = 32-byte prehash, sig = 64-byte compact (low-S), pk = SEC1. Returns 1 valid, 0 invalid, neg on parse error.
#[no_mangle]
pub extern "C" fn k256_ecdsa_verify(msg: *const u8, sig: *const u8, pk: *const u8, pk_len: usize) -> i32 {
    use k256::ecdsa::{signature::hazmat::PrehashVerifier, Signature, VerifyingKey};
    let msg = unsafe { core::slice::from_raw_parts(msg, 32) };
    let sig = unsafe { core::slice::from_raw_parts(sig, 64) };
    let pk = unsafe { core::slice::from_raw_parts(pk, pk_len) };
    let vk = match VerifyingKey::from_sec1_bytes(pk) { Ok(v) => v, Err(_) => return -1 };
    let s = match Signature::from_slice(sig) { Ok(s) => s, Err(_) => return -2 };
    match vk.verify_prehash(msg, &s) {
        Ok(_) => 1,
        Err(_) => 0,
    }
}

// ---- Block-proof wire format (matches the host structs) ----
#[derive(Deserialize)]
struct WireProof {
    leaf: [u8; 32],
    position: u64,
    siblings: Vec<[u8; 32]>,
}
#[derive(Deserialize)]
struct BlockInput {
    raw_tx: Vec<u8>,
    input_idx: u32,
    prevouts: Vec<u8>,
    flags: u32,
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
#[derive(Deserialize)]
struct BlockWitness {
    header: Vec<u8>,            // 80-byte block header
    height: u32,               // block height (for the subsidy schedule)
    coinbase_tx: Vec<u8>,      // the coinbase tx (its outputs = subsidy + fees)
    txids: Vec<[u8; 32]>,      // all txids in order (internal), for the merkle root
    wtxids: Vec<[u8; 32]>,     // all wtxids (coinbase = zeros), for the BIP141 witness commitment
    root_prev: WireStump,
    inputs: Vec<BlockInput>,   // non-coinbase input verifications
    new_outputs: Vec<[u8; 32]>, // leaves of the coins the block creates
    root_next: WireStump,
    bip30: Option<Bip30Overwrite>, // Some ONLY at the two grandfathered BIP30 blocks (F3)
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
    if (version < 2 && w.height >= 227_931)
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
    for l in &output_leaves[cb_start..] { created_at.entry(*l).or_insert(0u32); }
    if w.txids.is_empty() || cb_txid != w.txids[0] { all_ok = false; }
    let mut tx_idx = 1usize;
    for inp in &w.inputs {
        if inp.tx_first == 1 {
            let start = output_leaves.len();
            let t = gather(&inp.raw_tx, 0, &mut output_leaves);
            for l in &output_leaves[start..] { created_at.entry(*l).or_insert(tx_idx as u32); }
            if tx_idx >= w.txids.len() || t != w.txids[tx_idx] { all_ok = false; }
            tx_idx += 1;
        }
    }
    if tx_idx != w.txids.len() { all_ok = false; } // tx count must match the merkle-committed set
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
            let n = unsafe { tx_vin_count(head.raw_tx.as_ptr(), head.raw_tx.len() as u32) } as usize;
            if head.tx_first != 1 || n == 0 || i + n > w.inputs.len() { group_ok = false; break; }
            for j in 0..n {
                let g = &w.inputs[i + j];
                if g.raw_tx != head.raw_tx || g.prevouts != head.prevouts
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
        let mut leaf = [0u8; 32];
        let r = match chunk {
            None => unsafe {
                // Full verify (expensive VerifyScript), fills `leaf`.
                verify_input(
                    inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, inp.input_idx,
                    inp.prevouts.as_ptr(), inp.prevouts.len() as u32, flags,
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
                        inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, inp.input_idx,
                        inp.prevouts.as_ptr(), inp.prevouts.len() as u32,
                        inp.coin_height, inp.coin_is_coinbase, inp.coin_mtp, leaf.as_mut_ptr(),
                    )
                };
                let d = input_bind(&inp.raw_tx, inp.input_idx, &inp.prevouts,
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
                    inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32,
                    inp.prevouts.as_ptr(), inp.prevouts.len() as u32,
                    &mut fee as *mut i64,
                )
            };
            tx_checks.push(c);
            if c != 1 { all_ok = false; } else { total_fee += fee; }
            let final_ok = unsafe {
                is_final_tx(inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, w.height as i64, lock_time as i64)
            } == 1;
            if !final_ok { all_ok = false; }
        }

        // Per-INPUT: coinbase maturity + BIP68 relative locktime (height + time).
        let locks = unsafe {
            check_input_locks(
                inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, inp.input_idx,
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
    // Coinbase wtxid is committed as all-zeros (BIP141); has_witness = any non-coinbase carries witness.
    let mut rec_wtxids: Vec<[u8; 32]> = vec![[0u8; 32]];
    let mut has_witness = false;
    for inp in &w.inputs {
        if inp.tx_first == 1 {
            let mut wt = [0u8; 32];
            let hw = unsafe { tx_wtxid_info(inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, wt.as_mut_ptr()) };
            rec_wtxids.push(wt);
            has_witness |= hw == 1;
        }
    }
    let flat_wtx: Vec<u8> = rec_wtxids.iter().flatten().copied().collect();
    let witness_ok = unsafe {
        check_witness_commitment(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32,
            flat_wtx.as_ptr(), rec_wtxids.len() as u32, has_witness as u32)
    } == 1;
    // BIP34: coinbase encodes the block height (from 227931).
    let bip34_ok = unsafe { check_bip34(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, w.height) } == 1;
    // BIP30: no duplicate txids within the block. (Cross-block duplicate-txid — spending an already-
    // existing unspent output — is structurally prevented post-227931 by BIP34, which we enforce; the
    // two historical pre-BIP34 exceptions are known.) Cheap O(n log n) distinctness over the txids.
    let bip30_ok = {
        let mut ids = w.txids.clone();
        ids.sort_unstable();
        ids.windows(2).all(|w| w[0] != w[1])
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
    let subsidy_ok = coinbase_val <= subsidy + total_fee;

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
        total_weight += weight_of(&inp.raw_tx);
        total_sigops += unsafe {
            tx_full_sigops(inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, inp.prevouts.as_ptr(), inp.prevouts.len() as u32, flags)
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
        coinbase_val, subsidy, subsidy_ok, all_ok, root_matches, weight_ok, sigops_ok, witness_ok, bip34_ok, bip30_ok,
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
    // "out" boundary — the chain state just after block hi.
    out_tip_hash: [u8; 32],
    out_roots: Vec<Option<[u8; 32]>>, out_leaves: u64,
    out_nbits: u32, out_time: u32, out_epoch_start: u32, out_recent: Vec<u32>,
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

    env::commit(&RangeState {
        kind: KIND_RANGE,
        lo: height, hi: height,
        in_tip_hash, in_roots, in_leaves, in_nbits, in_time, in_epoch_start, in_recent,
        out_tip_hash: r.tip_hash, out_roots: r.root_next_roots, out_leaves: r.root_next_leaves,
        out_nbits: r.nbits, out_time: r.block_time, out_epoch_start, out_recent,
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

    let mut range_work = l.range_work;
    add256(&mut range_work, &rr.range_work);
    env::commit(&RangeState {
        kind: KIND_RANGE,
        lo: l.lo, hi: rr.hi,
        in_tip_hash: l.in_tip_hash, in_roots: l.in_roots, in_leaves: l.in_leaves,
        in_nbits: l.in_nbits, in_time: l.in_time, in_epoch_start: l.in_epoch_start, in_recent: l.in_recent,
        out_tip_hash: rr.out_tip_hash, out_roots: rr.out_roots, out_leaves: rr.out_leaves,
        out_nbits: rr.out_nbits, out_time: rr.out_time, out_epoch_start: rr.out_epoch_start, out_recent: rr.out_recent,
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

// Legacy per-input batch (n inputs, no accumulator) — kept for single-spend cycle measurement.
fn legacy() {
    let n: u32 = env::read();
    let mut results: Vec<i32> = Vec::with_capacity(n as usize);
    for _ in 0..n {
        let raw_tx: Vec<u8> = env::read();
        let input_idx: u32 = env::read();
        let prevouts: Vec<u8> = env::read();
        let flags: u32 = env::read();
        let mut leaf = [0u8; 32];
        let r: i32 = unsafe {
            verify_input(
                raw_tx.as_ptr(), raw_tx.len() as u32, input_idx,
                prevouts.as_ptr(), prevouts.len() as u32, flags,
                0, 0, 0,
                leaf.as_mut_ptr(),
            )
        };
        results.push(r);
    }
    env::commit(&results);
}

// ---- Segmentation: chunk (map) + aggregate (reduce) ----
#[derive(Deserialize)]
struct ChunkInput { raw_tx: Vec<u8>, input_idx: u32, prevouts: Vec<u8>, coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32 }
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
    for _ in 0..n {
        let c: ChunkInput = env::read();
        let mut leaf = [0u8; 32];
        let r = unsafe {
            verify_input(
                c.raw_tx.as_ptr(), c.raw_tx.len() as u32, c.input_idx,
                c.prevouts.as_ptr(), c.prevouts.len() as u32, flags,
                c.coin_height, c.coin_is_coinbase, c.coin_mtp, leaf.as_mut_ptr(),
            )
        };
        if r != 1 { all_valid = false; }
        // Bind exactly what was verified (tx bytes, input idx, prevouts, coin metadata, flags) so the
        // aggregation can prove the block's input is the one this chunk validated — see input_bind (#2).
        binds.push(input_bind(&c.raw_tx, c.input_idx, &c.prevouts, c.coin_height, c.coin_is_coinbase, c.coin_mtp, flags));
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
        cum_work: cum, height, prev_nbits: r.nbits, prev_time: r.block_time, epoch_start, recent_times, self_id,
    });
}

fn main() {
    // Run C++ static constructors ONCE (Core's global tagged-hash midstates) — fixed cost per run.
    unsafe { __libc_init_array() };
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
        _ => legacy(),
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

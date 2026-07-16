// Guest: verify ONE transaction input using Bitcoin Core's REAL VerifyScript + interpreter + sighash
// + libsecp256k1 — all compiled into the guest via build.rs.
use risc0_zkvm::guest::env;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

mod utreexo;

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
    fn merkle_root(txids: *const u8, n: u32, out_root: *mut u8);
    // BIP141 witness commitment check (coinbase commits to the witness merkle root over `wtxids`).
    fn check_witness_commitment(cb: *const u8, cb_len: u32, wtxids: *const u8, n: u32, has_witness: u32) -> i32;
    // BIP34: coinbase scriptSig must encode the block height (from height 227836).
    fn check_bip34(cb: *const u8, cb_len: u32, height: u32) -> i32;
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
fn block_script_flags(height: u32) -> u32 {
    let mut f = 0u32;
    if height >= 173_805 { f |= 1 << 0; }              // P2SH (BIP16)
    if height >= 363_725 { f |= 1 << 2; }              // DERSIG (BIP66)
    if height >= 388_381 { f |= 1 << 9; }              // CHECKLOCKTIMEVERIFY (BIP65)
    if height >= 419_328 { f |= 1 << 10; }             // CHECKSEQUENCEVERIFY (BIP112)
    if height >= 481_824 { f |= (1 << 11) | (1 << 4); } // WITNESS + NULLDUMMY (segwit, BIP141/147)
    if height >= 709_632 { f |= 1 << 17; }             // TAPROOT (BIP341/342)
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

// RISC0-accelerated secp256k1 ECDSA verify (k256, using the EC precompile). Called from Core's
// pubkey.cpp (or the benchmark). msg = 32-byte prehash, sig = 64-byte compact (low-S), pk = SEC1.
// Returns 1 valid, 0 invalid, negative on parse error.
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

    let block_time = u32::from_le_bytes(w.header[68..72].try_into().unwrap());
    // Consensus script flags active at this block's height (BIP66/65/CSV/segwit/taproot).
    let flags = block_script_flags(w.height);
    // BIP113: from CSV activation (419328), locktime uses median-time-past instead of block time.
    let lock_time = if w.height >= 419_328 { mtp } else { block_time };

    // Recompute the block's created output leaves from the REAL tx bytes (coinbase + every tx), skipping
    // unspendable outputs (H3) — instead of trusting host-supplied w.new_outputs (soundness). Each tx's
    // computed txid is bound to the merkle-committed w.txids, so the raw bytes ARE the block's txs. The
    // output set also lets us detect in-block-created coins (H1): an input whose leaf is in this set
    // spends a coin created earlier in this block (ephemeral — never entered the accumulator).
    let mut output_leaves: Vec<[u8; 32]> = Vec::new();
    let gather = |raw: &[u8], is_cb: u32, sink: &mut Vec<[u8; 32]>| -> [u8; 32] {
        let mut buf = vec![0u8; (raw.len() / 8 + 1) * 32];
        let mut txid = [0u8; 32];
        let n = unsafe { tx_out_leaves(raw.as_ptr(), raw.len() as u32, w.height, is_cb, block_time, buf.as_mut_ptr(), txid.as_mut_ptr()) };
        for i in 0..n as usize {
            let mut l = [0u8; 32];
            l.copy_from_slice(&buf[i * 32..i * 32 + 32]);
            sink.push(l);
        }
        txid
    };
    let cb_txid = gather(&w.coinbase_tx, 1, &mut output_leaves);
    if w.txids.is_empty() || cb_txid != w.txids[0] { all_ok = false; }
    let mut tx_idx = 1usize;
    for inp in &w.inputs {
        if inp.tx_first == 1 {
            let t = gather(&inp.raw_tx, 0, &mut output_leaves);
            if tx_idx >= w.txids.len() || t != w.txids[tx_idx] { all_ok = false; }
            tx_idx += 1;
        }
    }
    if tx_idx != w.txids.len() { all_ok = false; } // tx count must match the merkle-committed set
    let output_set: BTreeSet<[u8; 32]> = output_leaves.iter().copied().collect();
    let mut spent_in_block: BTreeSet<[u8; 32]> = BTreeSet::new();

    for (idx, inp) in w.inputs.iter().enumerate() {
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
            Some((chunk_leaves, all_valid)) => {
                // Aggregation: recompute the leaf (cheap) and take script validity from the chunks.
                unsafe {
                    coin_leaf_only(
                        inp.raw_tx.as_ptr(), inp.raw_tx.len() as u32, inp.input_idx,
                        inp.prevouts.as_ptr(), inp.prevouts.len() as u32,
                        inp.coin_height, inp.coin_is_coinbase, inp.coin_mtp, leaf.as_mut_ptr(),
                    )
                };
                if idx < chunk_leaves.len() && chunk_leaves[idx] == leaf && all_valid { 1 } else { -1 }
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
        if output_set.contains(&leaf) {
            // IN-BLOCK spend (H1): this coin was created by an earlier tx in THIS block, so it never
            // entered the accumulator — cancel it (ephemeral). Its script still had to pass (above).
            spent_in_block.insert(leaf);
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
    let flat: Vec<u8> = w.txids.iter().flatten().copied().collect();
    unsafe { merkle_root(flat.as_ptr(), w.txids.len() as u32, mroot.as_mut_ptr()) };
    let merkle_ok = mroot[..] == w.header[36..68]; // header bytes 36..68 = hashMerkleRoot

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
    // BIP34: coinbase encodes the block height (from 227836).
    let bip34_ok = unsafe { check_bip34(w.coinbase_tx.as_ptr(), w.coinbase_tx.len() as u32, w.height) } == 1;
    // BIP30: no duplicate txids within the block. (Cross-block duplicate-txid — spending an already-
    // existing unspent output — is structurally prevented post-227836 by BIP34, which we enforce; the
    // two historical pre-BIP34 exceptions are known.) Cheap O(n log n) distinctness over the txids.
    let bip30_ok = {
        let mut ids = w.txids.clone();
        ids.sort_unstable();
        ids.windows(2).all(|w| w[0] != w[1])
    };

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
        tip_hash: dsha256(&w.header),
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
    // S1: chain the image-id constraint down — the prev proof must have recursed against the SAME id.
    // With the verifier asserting the FINAL self_id == METHOD_ID, this forces every level to METHOD_ID.
    if is_base == 0 {
        assert!(prev.self_id == self_id, "recursion image-id mismatch");
    }

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

    assert!(
        block_valid && prevhash_ok && carry_ok && retarget_ok,
        "chain step: block_valid={} prevhash_ok={} carry_ok={} retarget_ok={}",
        block_valid, prevhash_ok, carry_ok, retarget_ok
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
    assert!(block_valid && prevhash_ok && carry_ok && retarget_ok,
        "prove_range block {}: bv={} ph={} carry={} rt={}", height, block_valid, prevhash_ok, carry_ok, retarget_ok);

    let mut range_work = [0u8; 32];
    unsafe { add_work(range_work.as_mut_ptr(), r.nbits) };
    let out_epoch_start = if height % RETARGET_INTERVAL == 0 { r.block_time } else { in_epoch_start };
    let mut out_recent = in_recent.clone();
    out_recent.push(r.block_time);
    if out_recent.len() > 11 { let e = out_recent.len() - 11; out_recent.drain(0..e); }

    env::commit(&RangeState {
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
        let flags = block_script_flags(s.block_height);
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
struct ChunkOut { all_valid: bool, leaves: Vec<[u8; 32]> }

// Mode 4: prove a BATCH of inputs' scripts (the expensive VerifyScript). Parallelisable across a
// block; commits the coin leaves it verified so the aggregation can bind them to the block's inputs.
fn chunk_prove() {
    let height: u32 = env::read();
    let flags = block_script_flags(height);
    let n: u32 = env::read();
    let mut leaves: Vec<[u8; 32]> = Vec::with_capacity(n as usize);
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
        leaves.push(leaf);
    }
    env::commit(&ChunkOut { all_valid, leaves });
}

// Mode 5: aggregate K chunk proofs into a block/chain proof. env::verify each chunk (composition),
// concatenate their leaves, then do the CHEAP sequential parts (accumulator transition + block
// checks) via validate_block with scripts sourced from the chunks. Same output as chain_step.
fn aggregate() {
    let self_id: [u32; 8] = env::read();
    let k: u32 = env::read();
    let mut all_leaves: Vec<[u8; 32]> = Vec::new();
    let mut chunks_ok = true;
    for _ in 0..k {
        let cj: Vec<u8> = env::read();
        env::verify(self_id, &cj).expect("chunk proof invalid");
        let words: Vec<u32> = cj.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
        let out: ChunkOut = risc0_zkvm::serde::from_slice(&words).expect("decode chunk");
        if !out.all_valid { chunks_ok = false; }
        all_leaves.extend(out.leaves);
    }
    let prev_journal: Vec<u8> = env::read();
    let w: BlockWitness = env::read();
    let is_base: u32 = env::read();
    if is_base == 0 { env::verify(self_id, &prev_journal).expect("previous chain proof invalid"); }
    let words: Vec<u32> = prev_journal.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
    let prev: ChainState = risc0_zkvm::serde::from_slice(&words).expect("decode prev");
    // S1: chunk verification above AND the prev-chain verification both used `self_id`; committing it
    // + asserting prev.self_id==self_id (below) + the verifier checking final==METHOD_ID forces every
    // recursion (chunks and chain) to the real guest.
    if is_base == 0 {
        assert!(prev.self_id == self_id, "recursion image-id mismatch");
    }
    let prev_mtp = median_time_past(&prev.recent_times);

    let r = validate_block(&w, prev_mtp, Some((&all_leaves, chunks_ok)));
    let block_valid = r.all_ok && r.root_matches && r.pow_ok && r.merkle_ok && r.subsidy_ok && r.weight_ok && r.sigops_ok && r.witness_ok && r.bip34_ok && r.bip30_ok;
    let prevhash_ok = w.header[4..36] == prev.tip_hash[..];
    let carry_ok = normalize(w.root_prev.roots.clone()) == normalize(prev.utxo_roots.clone()) && w.root_prev.num_leaves == prev.utxo_leaves;
    let height = prev.height + 1;
    let expected_nbits = if height % RETARGET_INTERVAL != 0 { prev.prev_nbits } else { unsafe { calc_next_bits(prev.prev_nbits, prev.epoch_start as i64, prev.prev_time as i64) } };
    let retarget_ok = r.nbits == expected_nbits;
    assert!(block_valid && prevhash_ok && carry_ok && retarget_ok,
        "aggregate: bv={} ph={} carry={} rt={}", block_valid, prevhash_ok, carry_ok, retarget_ok);
    let mut cum = prev.cum_work;
    unsafe { add_work(cum.as_mut_ptr(), r.nbits) };
    let epoch_start = if height % RETARGET_INTERVAL == 0 { r.block_time } else { prev.epoch_start };
    let mut recent_times = prev.recent_times.clone();
    recent_times.push(r.block_time);
    if recent_times.len() > 11 { let e = recent_times.len() - 11; recent_times.drain(0..e); }
    env::commit(&ChainState {
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

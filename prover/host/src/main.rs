use bitcoin::consensus::{deserialize, serialize};
use bitcoin::hashes::Hash as _;
use bitcoin::{absolute, transaction, Amount, OutPoint, ScriptBuf, Sequence, Transaction, TxIn, TxOut, Txid, Witness};
use hazync_utreexo::{coin_leaf, hash_leaf, Forest, Hash};
use hazync_coinbase_smt::{Proof as SmtProof, Smt};
use methods::{METHOD_ELF, METHOD_ID};
use risc0_zkvm::{default_executor, default_prover, ExecutorEnv, ProverOpts};
use serde::{Deserialize, Serialize};

// The guest's actual script-flag schedule, compiled here too so `script-flags-test` exercises the real
// code (no drifting copy). Same file the guest builds as `mod script_flags`.
#[path = "../../methods/guest/src/script_flags.rs"]
mod script_flags;

// A byte blob (de)serialised via risc0 serde's PACKED byte path (serialize_bytes → 4 bytes/word) instead
// of the default one-word-per-byte. Must match the guest's PackedBytes. Wire encoding only. Used for the
// shared, de-duplicated per-tx raw_tx / prevouts blobs.
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
            // JSON (serde_json) has no native bytes type: `serialize_bytes` emits a sequence of u8, so the
            // bundle round-trip (bridge writes bundle_<n>.json, prove-range-bridge reads it) lands here.
            // risc0's binary serde still hits visit_bytes/visit_byte_buf above; both paths yield Vec<u8>.
            fn visit_seq<A: serde::de::SeqAccess<'de>>(self, mut seq: A) -> Result<Vec<u8>, A::Error> {
                let mut out = Vec::with_capacity(seq.size_hint().unwrap_or(0));
                while let Some(b) = seq.next_element::<u8>()? { out.push(b); }
                Ok(out)
            }
        }
        Ok(PackedBytes(d.deserialize_byte_buf(V)?))
    }
}

// H8 domain tags — first committed field of each recursion-consumed journal (must match the guest).
const KIND_CHAIN: u32 = 0xC4A1_0002;
const KIND_RANGE: u32 = 0xC4A1_0006;
const KIND_CHUNK: u32 = 0xC4A1_0004;

// ---- Wire format: MUST match the guest structs field-for-field, in order. ----
#[derive(Serialize, Deserialize, Clone)]
struct WireProof { leaf: [u8; 32], position: u64, siblings: Vec<[u8; 32]> }
#[derive(Serialize, Deserialize)]
struct BlockInput {
    // flags removed: script flags are guest-derived (block_script_flags), never host-supplied.
    // raw_tx + prevouts are de-duplicated into BlockWitness.txs / tx_prevouts; this input refers to its
    // tx by index (a multi-input tx would otherwise repeat its full bytes once per input).
    tx_idx: u32, input_idx: u32,
    global_pos: u64, coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32, tx_first: u32,
    proof_i: WireProof, proof_last: WireProof,
}
#[derive(Serialize, Deserialize, Clone)]
struct WireStump { roots: Vec<Option<[u8; 32]>>, num_leaves: u64 }
#[derive(Serialize, Deserialize, Clone)]
struct Bip30Del { global_pos: u64, proof_i: WireProof, proof_last: WireProof }
#[derive(Serialize, Deserialize, Clone)]
struct Bip30Overwrite { old_height: u32, old_mtp: u32, dels: Vec<Bip30Del> } // F3: superseded coinbase deletes
#[derive(Serialize, Deserialize)]
struct BlockWitness {
    header: Vec<u8>, height: u32, coinbase_tx: Vec<u8>, txids: Vec<[u8; 32]>, wtxids: Vec<[u8; 32]>,
    root_prev: WireStump, txs: Vec<PackedBytes>, tx_prevouts: Vec<PackedBytes>,
    inputs: Vec<BlockInput>, new_outputs: Vec<[u8; 32]>, root_next: WireStump,
    bip30: Option<Bip30Overwrite>,
    // #54 — the coinbase-SMT root this block starts from, and the sequenced proofs that advance it.
    // Serialised LAST so the field order matches the guest's, which must stay in step for every wire
    // struct here.
    #[serde(default)]
    in_smt_root: [u8; 32],
    smt: SmtBlockWitness,
}
#[derive(Serialize, Deserialize, Clone)]
struct ChainState {
    kind: u32, // H8: == KIND_CHAIN
    tip_hash: [u8; 32], utxo_roots: Vec<Option<[u8; 32]>>, utxo_leaves: u64,
    cum_work: [u8; 32], height: u32,
    prev_nbits: u32, prev_time: u32, epoch_start: u32, recent_times: Vec<u32>,
    anchor_id: [u8; 32], // S5: dsha256 of the base anchor; verifier pins == dsha256(genesis_anchor)
    self_id: [u32; 8],  // S1: image id recursed against; verifier asserts == METHOD_ID
}
#[derive(Serialize, Deserialize)]
struct ChunkInput { raw_tx: Vec<u8>, input_idx: u32, prevouts: Vec<u8>, coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32 }
#[derive(Serialize, Deserialize)]
struct ChunkOut { kind: u32, all_valid: bool, binds: Vec<[u8; 32]> }
#[derive(Serialize, Deserialize)]
struct SpendCheck { raw_tx: Vec<u8>, prevouts: Vec<u8>, block_height: u32 }
#[derive(Serialize, Deserialize)]
struct SpendResult { script: i32, sigops: i64, tx_check: i32, flags: u32 }

// ---- Real mainnet blocks 170 (coinbase + first Bitcoin tx) → 171 → 172 (coinbase-only). ----
const CB170: &str = "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff0704ffff001d0102ffffffff0100f2052a01000000434104d46c4968bde02899d2aa0963367c7a6ce34eec332b32e42e5f3407e052d64ac625da6f0718e7b302140434bd725706957c092db53805b821a85b23a7ac61725bac00000000";
const SPEND170: &str = "0100000001c997a5e56e104102fa209c6a852dd90660a20b2d9c352423edce25857fcd3704000000004847304402204e45e16932b8af514961a1d3a1a25fdf3f4f7732e9d624c6c61548ab5fb8cd410220181522ec8eca07de4860a4acdd12909d831cc56cbbac4622082221a8768d1d0901ffffffff0200ca9a3b00000000434104ae1a62fe09c5f51b13905f07f06b99a2f7159b2225f374cd378d71302fa28414e7aab37397f554a7df5f142c21c1b7303b8a0626f1baded5c72a704f7e6cd84cac00286bee0000000043410411db93e1dcdb8a016b49840f8c53bc1eb68a382e97b1482ecad7b148a6909a5cb2e0eaddfb84ccf9744464f82e160bfa9b8b64f9d4c03f999b8643f656b412a3ac00000000";
const SPEND170_PREV_SPK: &str = "410411db93e1dcdb8a016b49840f8c53bc1eb68a382e97b1482ecad7b148a6909a5cb2e0eaddfb84ccf9744464f82e160bfa9b8b64f9d4c03f999b8643f656b412a3ac";
const SPEND170_PREV_VALUE: u64 = 5_000_000_000;
const CB171: &str = "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff0704ffff001d010effffffff0100f2052a01000000434104566824c312073315df60e5aa6490b6cdd80cd90f6a8f02e022ca3c2d52968c253006c9c602e03aed7be52d6ac55f5b557c72529bcc3899ace7eb4227153eb44bac00000000";
const CB172: &str = "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff0704ffff001d0106ffffffff0100f2052a010000004341044c718603ac207940cfce606b414b42b7cb10abbc714fe44f42f1c10a9990fb0f7202838cfb4fb8512f884ee3e2f47d55992d916880a2c6b46e254d86cd5952b3ac00000000";

// Real block 91842 (coinbase-only) — a BIP30 grandfathered block: its coinbase duplicates block 91812's
// still-unspent coinbase (merkle == that coinbase's txid). Used by check-bip30 (F3).
const CB91842: &str = "01000000010000000000000000000000000000000000000000000000000000000000000000ffffffff060456720e1b00ffffffff0100f2052a010000004341046896ecfc449cb8560594eb7f413f199deb9b4e5d947a142e7dc7d2de0b811b8e204833ea2a2fd9d4c7b153a8ca7661d0a0b7fc981df1f42f55d64b26b3da1e9cac00000000";
const PREV91842: &str = "00000000000a1e92acbcbdf594cac25d1095544d5fbf5113bfec85a9eb4b1120";
const MERKLE91842: &str = "d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599";

const HASH169: &str = "000000002a22cfee1f2c846adbd12b3e183d4f97683f85dad08a79780a84bd55"; // block 170's prev
const HASH170: &str = "00000000d1145790a8694403d4063f323d499e655c83426834d4ce2f8dd4a2ee";
const HASH171: &str = "00000000c9ec538cab7f38ef9c67a95742f56ab07b0a37c5be6b02808dbfb4e0";
const HASH172: &str = "00000000e3efabf60693ecc2519c5f761801ccac25c2ac89e32d11dd92686854";
const MERKLE170: &str = "7dac2c5666815c17a3b36427de37bb9d2e2c5ccec3f8633eb91a4205cb4c10ff";

fn hx(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}
fn hex(b: &[u8]) -> String { b.iter().map(|x| format!("{x:02x}")).collect() }

// Proving segment size (2^n cycles). risc0 4.0.5 has a preflight bug: for ~10% of blocks a segment packs
// right to its 2^po2 boundary and the assertion `cycles <= 1 << segment.po2` overflows -> the prove panics
// (on CPU AND cuda — shared witgen). It's a liveness bug, never a wrong proof. Smaller segments repartition
// the work and clear the boundary, so the caller retries a failed prove with a decremented HAZYNC_SEG_PO2.
// EVERY prove path calls this so the retry is honoured everywhere. Host-side executor config only — the
// guest is untouched by this knob, so it does not affect METHOD_ID.
// Default depends on the backend, because the trade-off does. Bigger segments = fewer of them = less
// recursion/fold overhead: on GPU that measured ~6% faster than 20 on block 130000 (23.0s vs 24.4s), flat
// to 22, so 21 is the sweet spot (same speed as 22, less VRAM). But a po2-21 segment also needs ~2x the
// working memory, and on CPU that is a pure cost — the speed win was never measured there, while an
// 11 GB box proving block 170 (a 2.3M-cycle block!) hit 8.7 GB RSS and went to swap. Swapping is not a
// prove *failure*, so the retry ladder below never fires; it just crawls. So: 21 with the cuda feature,
// the risc0 default of 20 otherwise. HAZYNC_SEG_PO2 overrides either way.
fn seg_po2() -> u32 {
    std::env::var("HAZYNC_SEG_PO2").ok().and_then(|s| s.parse().ok())
        .unwrap_or(if cfg!(feature = "cuda") { 21 } else { 20 })
}

// This host's guest image id (METHOD_ID) as the canonical RISC0 hex digest.
fn method_id_hex() -> String { risc0_zkvm::Digest::from(METHOD_ID).to_string() }

// Verify a receipt's STARK against THIS host's guest image id. On failure, explain the usual cause
// instead of a raw panic: the host was built from a different guest/toolchain than produced the
// proof, so the image ids (METHOD_ID) differ. That is a BUILD mismatch, not an invalid proof.
fn digest_hex(id: [u32; 8]) -> String { risc0_zkvm::Digest::from(id).to_string() }

fn verify_receipt(r: &risc0_zkvm::Receipt) { verify_receipt_ex(r, None) }

// Verify a receipt's STARK against THIS host's guest image id, distinguishing the two failure modes when
// the caller can supply the proof's own committed guest id (`claimed_id`, the journal's self_id):
//   - MISMATCH  — the proof was made by a DIFFERENT guest (claimed_id != METHOD_ID); a build mismatch,
//                 the common onboarding trip, NOT evidence the proof is bad.
//   - INVALID   — the proof claims THIS guest but the STARK does not verify; forged/tampered/corrupt.
// Without a claimed_id (generic callers) we can't tell, so we assume the common build-mismatch cause.
fn verify_receipt_ex(r: &risc0_zkvm::Receipt, claimed_id: Option<[u32; 8]>) {
    if let Err(e) = r.verify(METHOD_ID) {
        let mismatch = claimed_id.map_or(true, |id| id != METHOD_ID);
        if mismatch {
            eprintln!("STARK verification FAILED: guest image-id (METHOD_ID) MISMATCH.");
            eprintln!("The proof was produced by a DIFFERENT guest build than this host (a RISC0 image id");
            eprintln!("is a hash of the exact guest build), so the ids differ. This is a BUILD mismatch,");
            eprintln!("not necessarily a bad proof.");
            if let Some(id) = claimed_id { eprintln!("  proof's guest id:      {}", digest_hex(id)); }
            eprintln!("  this host's METHOD_ID: {}", method_id_hex());
            eprintln!("Build a host that matches the proof's guest (reproduce/Dockerfile), then retry.");
            eprintln!("See PROVING.md -> \"the guest image id (METHOD_ID) & reproducibility\".");
        } else {
            eprintln!("STARK verification FAILED: PROOF INVALID (not a genuine proof for this guest).");
            eprintln!("The receipt claims THIS guest (METHOD_ID {}) but the STARK did not", method_id_hex());
            eprintln!("verify — the proof is forged, tampered, or corrupt. This is NOT a build mismatch.");
        }
        eprintln!("Underlying verifier error: {e}");
        std::process::exit(1);
    }
}
fn rev(mut v: Vec<u8>) -> Vec<u8> { v.reverse(); v }
fn arr(v: Vec<u8>) -> [u8; 32] { v.try_into().unwrap() }

fn wire_proof(p: &hazync_utreexo::Proof) -> WireProof {
    WireProof { leaf: p.leaf, position: p.position, siblings: p.siblings.clone() }
}
fn wire_stump(f: &Forest) -> WireStump { WireStump { roots: f.roots(), num_leaves: f.leaves.len() as u64 } }
// Strip trailing empty root slots (mirrors the guest `normalize`) so two representations of the same
// accumulator (e.g. the empty genesis forest) compare equal regardless of padding.
fn normalize_host(mut v: Vec<Option<[u8; 32]>>) -> Vec<Option<[u8; 32]>> {
    while v.last() == Some(&None) { v.pop(); }
    v
}
// Block hash (internal/LE order) = double-SHA256 of the 80-byte header, matching the guest's dsha256.
fn header_hash(header: &[u8]) -> [u8; 32] { bitcoin::hashes::sha256d::Hash::hash(header).to_byte_array() }

// Digest of a range's FULL boundary — everything `fold_range` binds at a seam (tip, UTXO roots+leaves,
// difficulty, and the MTP window). Chaining ranges on `out_bhash(k) == in_bhash(k+1)` reproduces the
// guest fold's seam check that tip-hash equality alone does NOT (a mid-chain range could otherwise
// fabricate its in-boundary UTXO set / in_time / MTP window). Roots are normalized so padding can't vary.
fn boundary_digest(height: u32, tip: &[u8; 32], roots: &[Option<[u8; 32]>], leaves: u64, nbits: u32, time: u32, epoch: u32, recent: &[u32]) -> [u8; 32] {
    let mut m: Vec<u8> = Vec::new();
    // H9: bind the boundary's HEIGHT (out-boundary = hi, in-boundary = lo-1). RangeState.lo/hi are
    // committed in-circuit (prove_range sets lo=hi=w.height, the same value that selects the script
    // flags and coinbase subsidy; fold_range asserts hi+1==lo adjacency). Folding height into the seam
    // digest makes the coordinator's out_bhash(k)==in_bhash(k+1) chaining STRUCTURALLY require
    // hi(k)==lo(k+1)-1 — so a block mined onto the real tip but labelled a false (low) height, claiming a
    // larger subsidy / weaker flags, cannot chain even though its UTXO/difficulty/MTP boundary is valid.
    m.extend_from_slice(&height.to_le_bytes());
    m.extend_from_slice(tip);
    let nr = normalize_host(roots.to_vec());
    m.extend_from_slice(&(nr.len() as u32).to_le_bytes());
    for r in &nr {
        match r { Some(h) => { m.push(1); m.extend_from_slice(h); } None => m.push(0) }
    }
    m.extend_from_slice(&leaves.to_le_bytes());
    m.extend_from_slice(&nbits.to_le_bytes());
    m.extend_from_slice(&time.to_le_bytes());
    m.extend_from_slice(&epoch.to_le_bytes());
    m.extend_from_slice(&(recent.len() as u32).to_le_bytes());
    for t in recent { m.extend_from_slice(&t.to_le_bytes()); }
    header_hash(&m)
}
// Core CScript::IsUnspendable(): OP_RETURN (0x6a) or script > MAX_SCRIPT_SIZE (10000). Unspendable
// outputs never enter the UTXO set, so the accumulator (host + guest) must skip them (H3).
fn out_spendable(spk: &[u8]) -> bool { !((!spk.is_empty() && spk[0] == 0x6a) || spk.len() > 10_000) }

fn out_leaf_of(tx: &Transaction, txid: &[u8; 32], vout: usize, height: u32, is_coinbase: bool, mtp: u32) -> Hash {
    let o = &tx.output[vout];
    coin_leaf(txid, vout as u32, o.value.to_sat(), o.script_pubkey.as_bytes(), height, is_coinbase, mtp)
}
fn build_header(prev_disp: &str, merkle_internal: &[u8; 32], time: u32, bits: u32, nonce: u32) -> Vec<u8> {
    build_header_v(1, prev_disp, merkle_internal, time, bits, nonce) // version-1 helper (early blocks)
}
fn build_header_v(version: i32, prev_disp: &str, merkle_internal: &[u8; 32], time: u32, bits: u32, nonce: u32) -> Vec<u8> {
    let mut h = Vec::with_capacity(80);
    h.extend_from_slice(&version.to_le_bytes());     // real block version (versionbits post-BIP9)
    h.extend_from_slice(&rev(hx(prev_disp)));        // prev (internal)
    h.extend_from_slice(merkle_internal);            // merkle (internal)
    h.extend_from_slice(&time.to_le_bytes());
    h.extend_from_slice(&bits.to_le_bytes());
    h.extend_from_slice(&nonce.to_le_bytes());
    h
}

// One non-coinbase spend to fold into a block.
struct Spend { raw: Vec<u8>, prev_value: u64, prev_spk: Vec<u8>, coin_height: u32, coin_is_coinbase: bool, coin_mtp: u32 }

/// Build a block witness against (and advancing) the running accumulator `forest`.
fn build_block(
    forest: &mut Forest, header: Vec<u8>, height: u32, coinbase_hex: &str, spends: &[Spend], create_mtp: u32,
) -> BlockWitness {
    let coinbase: Transaction = deserialize(&hx(coinbase_hex)).unwrap();
    let cb_txid = coinbase.compute_txid().to_byte_array();
    let root_prev = wire_stump(forest);

    let mut txids = vec![cb_txid];
    let mut inputs = Vec::new();
    let mut txs = Vec::new();
    let mut tx_prevouts = Vec::new();
    for (tx_i, sp) in spends.iter().enumerate() {
        let tx: Transaction = deserialize(&sp.raw).unwrap();
        txids.push(tx.compute_txid().to_byte_array());
        let op = tx.input[0].previous_output;
        let spk = ScriptBuf::from_bytes(sp.prev_spk.clone());
        let coin = coin_leaf(&op.txid.to_byte_array(), op.vout, sp.prev_value, spk.as_bytes(), sp.coin_height, sp.coin_is_coinbase, sp.coin_mtp);
        let prevouts = serialize(&vec![TxOut { value: Amount::from_sat(sp.prev_value), script_pubkey: spk }]);
        txs.push(PackedBytes(sp.raw.clone()));
        tx_prevouts.push(PackedBytes(prevouts));
        let pos = forest.find(&coin).expect("spent coin in accumulator");
        let last = forest.leaves.len() - 1;
        inputs.push(BlockInput {
            tx_idx: tx_i as u32, input_idx: 0,
            global_pos: pos as u64, coin_height: sp.coin_height, coin_is_coinbase: sp.coin_is_coinbase as u32, coin_mtp: sp.coin_mtp, tx_first: 1,
            proof_i: wire_proof(&forest.prove(pos)),
            proof_last: wire_proof(&forest.prove(last)),
        });
        forest.delete(pos);
    }

    // Insert created coins: coinbase outputs then each spend's outputs. Creation-MTP = `create_mtp`
    // (= MTP(height-1), the same value the guest's median(prev.recent_times) commits).
    let mut new_outputs = Vec::new();
    for v in 0..coinbase.output.len() {
        if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
        let l = out_leaf_of(&coinbase, &cb_txid, v, height, true, create_mtp);
        forest.add(l);
        new_outputs.push(l);
    }
    for sp in spends {
        let tx: Transaction = deserialize(&sp.raw).unwrap();
        let txid = tx.compute_txid().to_byte_array();
        for v in 0..tx.output.len() {
            if !out_spendable(tx.output[v].script_pubkey.as_bytes()) { continue; }
            let l = out_leaf_of(&tx, &txid, v, height, false, create_mtp);
            forest.add(l);
            new_outputs.push(l);
        }
    }
    let root_next = wire_stump(forest);
    let wtxids = txids.clone(); // pre-segwit blocks: no witness -> has_witness=false, check passes
    let (in_smt_root, smt) = smt_witness_standalone(cb_txid, cb_spendable_outputs(&coinbase),
        &cb_spends_from(&inputs, &txs));
    BlockWitness { header, height, coinbase_tx: hx(coinbase_hex), txids, wtxids, root_prev, txs, tx_prevouts, inputs, new_outputs, root_next, bip30: None, in_smt_root, smt }
}

// Serialize a ChainState to the exact bytes env::commit(&state) would produce (LE u32 words).
fn state_journal_bytes(s: &ChainState) -> Vec<u8> {
    risc0_zkvm::serde::to_vec(s).unwrap().iter().flat_map(|w| w.to_le_bytes()).collect()
}

fn chain_step(prev: &ChainState, w: &BlockWitness, _is_base: u32) -> (ChainState, u64) {
    let mut b = ExecutorEnv::builder();
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(prev)).unwrap();
    b.write(w).unwrap();
    b.write(&1u32).unwrap(); // execute: is_base=1 skips env::verify (logic validation only)
    b.write(&METHOD_ID).unwrap();
    let s = default_executor().execute(b.build().unwrap(), METHOD_ELF).unwrap();
    (s.journal.decode().unwrap(), s.cycles())
}

fn work_u128(w: &[u8; 32]) -> u128 {
    let mut low = [0u8; 16];
    low.copy_from_slice(&w[0..16]); // arith_uint256 internal = little-endian
    u128::from_le_bytes(low)
}

// Seed the running UTXO accumulator (block-9 coinbase + filler) and the anchor checkpoint at 169.
fn seed_and_anchor() -> (Forest, ChainState) {
    let mut forest = Forest::new();
    for i in 0..4u64 {
        forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat()));
    }
    let spk9 = ScriptBuf::from_bytes(hx(SPEND170_PREV_SPK));
    let spend170_tx: Transaction = deserialize(&hx(SPEND170)).unwrap();
    let op9 = spend170_tx.input[0].previous_output;
    forest.add(coin_leaf(&op9.txid.to_byte_array(), op9.vout, SPEND170_PREV_VALUE, spk9.as_bytes(), 9, true, 1_231_473_279));
    for i in 0..2u64 {
        forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat()));
    }
    let anchor = ChainState {
        kind: KIND_CHAIN,
        tip_hash: arr(rev(hx(HASH169))), utxo_roots: forest.roots(), utxo_leaves: forest.leaves.len() as u64,
        cum_work: [0u8; 32], height: 169,
        prev_nbits: 0x1d00ffff, prev_time: 1_231_730_523, epoch_start: 1_231_006_505,
        recent_times: (0..11).map(|i| 1_231_729_000u32 + i * 140).collect(),
        anchor_id: [0u8; 32], self_id: METHOD_ID,
    };
    (forest, anchor)
}

fn header_170() -> Vec<u8> {
    build_header(HASH169, &arr(rev(hx(MERKLE170))), 1_231_731_025, 0x1d00ffff, 1_889_418_792)
}
fn spend_170() -> Spend {
    Spend { raw: hx(SPEND170), prev_value: SPEND170_PREV_VALUE, prev_spk: hx(SPEND170_PREV_SPK), coin_height: 9, coin_is_coinbase: true, coin_mtp: 1_231_473_279 }
}

// PROVE (not execute) the block-170 fold: a real STARK receipt attesting the block is valid and
// extends the anchor. Run on the VPS (this is memory-heavy). is_base=1 → no recursion assumption.
fn prove_block() {
    use std::time::Instant;
    let (mut forest, anchor) = seed_and_anchor();
    let w = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&anchor.recent_times));

    println!("=== PROVING block 170 chain_step (real STARK receipt) ===");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap(); // is_base = 1 (anchor-trusted; no env::verify assumption)
    b.write(&METHOD_ID).unwrap();
    let env = b.build().unwrap();

    let t = Instant::now();
    let info = default_prover().prove(env, METHOD_ELF).expect("prove");
    let secs = t.elapsed().as_secs_f64();
    let receipt = info.receipt;
    receipt.verify(METHOD_ID).expect("receipt verify");
    let out: ChainState = receipt.journal.decode().unwrap();
    assert!(out.self_id == METHOD_ID, "S1: proof recursed against wrong image id");
    let seal = bincode::serialize(&receipt).map(|v| v.len()).unwrap_or(0);
    println!("PROVED in {:.1}s — receipt VERIFIED against METHOD_ID.", secs);
    println!("  chain tip: height {}  tip_hash {}  cum_work {}", out.height, hex(&out.tip_hash), work_u128(&out.cum_work));
    println!("  UTXO root leaves: {}", out.utxo_leaves);
    println!("  receipt ~{} bytes (STARK). SNARK-wrap → ~200-300 B for trivial verification anywhere.", seal);
}

// Prove one chain step, discharging the env::verify recursion assumption with the previous receipt.
fn prove_step(prev_journal: Vec<u8>, prev_receipt: Option<risc0_zkvm::Receipt>, w: &BlockWitness, is_base: u32) -> risc0_zkvm::Receipt {
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    if let Some(r) = prev_receipt {
        b.add_assumption(r); // discharge env::verify(self_id, prev_journal)
    }
    b.write(&2u32).unwrap();
    b.write(&prev_journal).unwrap();
    b.write(w).unwrap();
    b.write(&is_base).unwrap();
    b.write(&METHOD_ID).unwrap();
    let receipt = default_prover().prove(b.build().unwrap(), METHOD_ELF).expect("prove step").receipt;
    receipt.verify(METHOD_ID).expect("step receipt verify");
    receipt
}

// PROVE the recursive chain 170 → 171 → 172: fold each block, binding to the previous proof via
// env::verify. The final receipt is a chain-tip proof of the whole range. Run on the VPS.
fn prove_chain() {
    use std::time::Instant;
    let (mut forest, anchor) = seed_and_anchor();
    let mut recent = anchor.recent_times.clone();
    let w170 = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&recent));
    advance_recent(&mut recent, 1_231_731_025); // block 170 time
    let cb171: Transaction = deserialize(&hx(CB171)).unwrap();
    let hdr171 = build_header(HASH170, &cb171.compute_txid().to_byte_array(), 1_231_731_401, 0x1d00ffff, 653_436_935);
    let w171 = build_block(&mut forest, hdr171, 171, CB171, &[], median_u32(&recent));
    advance_recent(&mut recent, 1_231_731_401); // block 171 time
    let cb172: Transaction = deserialize(&hx(CB172)).unwrap();
    let hdr172 = build_header(HASH171, &cb172.compute_txid().to_byte_array(), 1_231_731_853, 0x1d00ffff, 1_565_279_797);
    let w172 = build_block(&mut forest, hdr172, 172, CB172, &[], median_u32(&recent));

    println!("=== PROVING recursive chain 170 → 171 → 172 (env::verify composition) ===");
    let t = Instant::now();
    let r170 = prove_step(state_journal_bytes(&anchor), None, &w170, 1);
    println!("  block 170 proved ({:.0}s cum)", t.elapsed().as_secs_f64());
    let r171 = prove_step(r170.journal.bytes.clone(), Some(r170.clone()), &w171, 0);
    println!("  block 171 folded ({:.0}s cum)", t.elapsed().as_secs_f64());
    let r172 = prove_step(r171.journal.bytes.clone(), Some(r171.clone()), &w172, 0);
    let secs = t.elapsed().as_secs_f64();
    let tip: ChainState = r172.journal.decode().unwrap();
    assert!(tip.self_id == METHOD_ID, "S1: proof recursed against wrong image id");
    let seal = bincode::serialize(&r172).map(|v| v.len()).unwrap_or(0);
    println!("\n>>> CHAIN-TIP PROOF (170→172) in {:.1}s — receipt VERIFIED.", secs);
    println!("  tip height {}  tip_hash {}  cum_work {}", tip.height, hex(&tip.tip_hash), work_u128(&tip.cum_work));
    println!("  receipt ~{} bytes. This one proof attests the whole 170→172 range is valid.", seal);
}

// SNARK-wrap: prove block 170 and compress STARK -> Groth16 (~200-300 B, verifiable anywhere).
fn prove_snark() {
    use std::time::Instant;
    let (mut forest, anchor) = seed_and_anchor();
    let w = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&anchor.recent_times));
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    println!("=== SNARK-wrapping block 170 (STARK → Groth16) ===");
    let t = Instant::now();
    let receipt = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::groth16())
        .expect("groth16 prove")
        .receipt;
    receipt.verify(METHOD_ID).expect("groth16 verify");
    let secs = t.elapsed().as_secs_f64();
    let seal = bincode::serialize(&receipt).map(|v| v.len()).unwrap_or(0);
    println!(">>> GROTH16 receipt in {:.1}s — VERIFIED. size ~{} bytes (verifiable on a phone / on-chain).", secs, seal);
}

// Prove an ARBITRARY real block from a JSON file (HAZYNC_BLOCK). Handles a coinbase + N single-input
// txs with real prevouts. coin_height/coinbase/mtp are set benign (maturity/BIP68 no-op) — scripts,
// amounts, PoW, retarget, merkle, subsidy and the UTXO transition are all REAL and fully checked.
// Build the anchor + full block witness from the HAZYNC_BLOCK JSON (coinbase + N multi-input txs).
fn build_full() -> (ChainState, BlockWitness) {
    let path = std::env::var("HAZYNC_BLOCK").unwrap_or_else(|_| "block_full.json".into());
    let j: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    let height = j["height"].as_u64().unwrap() as u32;
    let bits = j["bits"].as_u64().unwrap() as u32;
    let time = j["time"].as_u64().unwrap() as u32;
    let nonce = j["nonce"].as_u64().unwrap() as u32;
    let version = j["version"].as_i64().unwrap_or(1) as i32; // real versionbits value (fallback v1)
    let prev = j["prev"].as_str().unwrap();
    let merkle = j["merkle"].as_str().unwrap();
    let cb_hex = j["coinbase_hex"].as_str().unwrap();
    let header = build_header_v(version, prev, &arr(rev(hx(merkle))), time, bits, nonce);

    let ch: u32 = height.saturating_sub(10_000); // benign mature height (coins marked non-coinbase)
    let coinbase: Transaction = deserialize(&hx(cb_hex)).unwrap();
    let cb_txid = coinbase.compute_txid().to_byte_array();

    // Parse each non-coinbase tx: raw + full prevout set + per-coin (height, is_coinbase, creation-MTP)
    // — real from the fetcher/bridge (S2), or benign fallback for JSONs lacking it (pre-S2 vectors like
    // 130000/140000). coin_mtp arrives from the archive-node bridge (-hazyncwitness hook) and closes
    // BIP68 time-based relative locks; the fetcher omits it (fallback 0 = conservative, no false reject).
    struct Ptx { raw: Vec<u8>, tx: Transaction, prevouts: Vec<TxOut>, meta: Vec<(u32, bool, u32)>, txid: [u8; 32] }
    let mut ptxs: Vec<Ptx> = Vec::new();
    for tx in j["txs"].as_array().unwrap() {
        let raw = hx(tx["raw"].as_str().unwrap());
        let t: Transaction = deserialize(&raw).unwrap();
        let mut prevouts = Vec::new();
        let mut meta = Vec::new();
        for p in tx["prevouts"].as_array().unwrap() {
            prevouts.push(TxOut {
                value: Amount::from_sat(p["value"].as_u64().unwrap()),
                script_pubkey: ScriptBuf::from_bytes(hx(p["spk"].as_str().unwrap())),
            });
            let h = p["coin_height"].as_u64().map(|x| x as u32).unwrap_or(ch);
            let cb = p["coin_is_coinbase"].as_u64().map(|x| x != 0).unwrap_or(false);
            let mtp = p["coin_mtp"].as_u64().map(|x| x as u32).unwrap_or(0);
            meta.push((h, cb, mtp));
        }
        let txid = t.compute_txid().to_byte_array();
        ptxs.push(Ptx { raw, tx: t, prevouts, meta, txid });
    }
    let leaf_of = |op: &bitcoin::OutPoint, o: &TxOut, h: u32, cb: bool, mtp: u32| coin_leaf(&op.txid.to_byte_array(), op.vout, o.value.to_sat(), o.script_pubkey.as_bytes(), h, cb, mtp);

    // recent_times (prev-11) + this block's creation-MTP — needed EARLY: the guest detects in-block
    // spends (H1) by LEAF membership in this block's created-output set (main.rs `created_at`), and the
    // leaf commits the creation-MTP. Keying on txid instead would false-positive a spend of an earlier
    // coin whose funding tx shares a txid with a tx in this block (pre-BIP34 coinbase-txid collisions).
    let mut recent_times: Vec<u32> = {
        let rt: Vec<u32> = j["recent_times"].as_array()
            .map(|a| a.iter().filter_map(|v| v.as_u64().map(|x| x as u32)).collect())
            .unwrap_or_default();
        if rt.is_empty() { (0..11).map(|i| time.saturating_sub(2000) + i * 100).collect() } else { rt }
    };
    // COV-1 negative-test hook (test-only, inert unless HAZYNC_COV1_BADTIME): prev-11 MTP == this block's
    // time so `time_ok` fails. Applied to recent_times so cmtp stays consistent host↔guest. NEVER in prod.
    if std::env::var("HAZYNC_COV1_BADTIME").is_ok() { recent_times = vec![time; 11]; }
    let cmtp = median_u32(&recent_times);
    // This block's created output leaves (coinbase + every tx, unspendable skipped) — the guest's in-block
    // set, keyed by LEAF (height-bearing, so a colliding old coinbase's leaf differs and stays external).
    let mut created: std::collections::HashSet<[u8; 32]> = std::collections::HashSet::new();
    for v in 0..coinbase.output.len() {
        if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
        created.insert(out_leaf_of(&coinbase, &cb_txid, v, height, true, cmtp));
    }
    for p in &ptxs {
        for v in 0..p.tx.output.len() {
            if !out_spendable(p.tx.output[v].script_pubkey.as_bytes()) { continue; }
            created.insert(out_leaf_of(&p.tx, &p.txid, v, height, false, cmtp));
        }
    }
    let mut spent_in_block: std::collections::HashSet<[u8; 32]> = std::collections::HashSet::new();
    // Seed the accumulator: filler + every EXTERNAL input's spent coin (in-block coins never entered it) + filler.
    let mut forest = Forest::new();
    for i in 0..4u64 { forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat())); }
    for p in &ptxs {
        for (i, o) in p.prevouts.iter().enumerate() {
            let coin = leaf_of(&p.tx.input[i].previous_output, o, p.meta[i].0, p.meta[i].1, p.meta[i].2);
            if created.contains(&coin) { continue; } // in-block: ephemeral, never in the accumulator
            forest.add(coin);
        }
    }
    for i in 0..2u64 { forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat())); }

    let mut anchor = ChainState {
        kind: KIND_CHAIN,
        tip_hash: arr(rev(hx(prev))), utxo_roots: forest.roots(), utxo_leaves: forest.leaves.len() as u64,
        cum_work: [0u8; 32], height: height - 1,
        prev_nbits: bits, prev_time: time.saturating_sub(600), epoch_start: time.saturating_sub(600 * 1000),
        // Real prev-11 block timestamps (median = MTP(height-1), the spend block's BIP68-time/BIP113
        // window) when the fetcher/bridge supplies them; else the benign placeholder for pre-S2 vectors
        // (130000/140000) that carry no `recent_times` — computed above (with the COV-1 hook applied).
        recent_times: recent_times.clone(),
        anchor_id: [0u8; 32], self_id: METHOD_ID,
    };

    // Build the witness: per tx a shared full-prevouts blob; per input a BlockInput (tx_first on input 0).
    let root_prev = wire_stump(&forest);
    let mut txids = vec![cb_txid];
    let mut wtxids: Vec<[u8; 32]> = vec![[0u8; 32]]; // coinbase wtxid = zeros (BIP141)
    let mut inputs: Vec<BlockInput> = Vec::new();
    let mut txs: Vec<PackedBytes> = Vec::new();          // de-duplicated: one raw_tx blob per tx
    let mut tx_prevouts: Vec<PackedBytes> = Vec::new();  // parallel: one prevouts blob per tx
    // SEC-2 negative-test hook (test-only, inert unless HAZYNC_SEC2_BADPOS is set): corrupt the FIRST
    // spend's claimed global position while leaving its inclusion proof honest — the exact inconsistency
    // an honest witness-builder cannot express (both fields normally derive from the same `pos`). The
    // guest's hardened `delete` must reject it (`all_ok=false`, and the accumulator diverges so
    // `root_matches=false`). See SECURITY.md / ROADMAP (SEC-2). NEVER set in production.
    let sec2_bad = std::env::var("HAZYNC_SEC2_BADPOS").is_ok();
    for (tx_i, p) in ptxs.iter().enumerate() {
        txids.push(p.txid);
        wtxids.push(p.tx.compute_wtxid().to_byte_array());
        let prevouts_blob = serialize(&p.prevouts);
        txs.push(PackedBytes(p.raw.clone()));            // this tx's shared raw_tx blob (index == tx_i)
        tx_prevouts.push(PackedBytes(prevouts_blob));    // this tx's shared prevouts blob
        for i in 0..p.tx.input.len() {
            let (ch_i, cb_i, mtp_i) = p.meta[i];
            let coin = leaf_of(&p.tx.input[i].previous_output, &p.prevouts[i], ch_i, cb_i, mtp_i);
            if created.contains(&coin) {
                // IN-BLOCK spend (H1): leaf matches a coin created earlier in this block (the guest's exact
                // rule) — it never entered the accumulator: dummy proof, no delete. Script still verifies.
                spent_in_block.insert(coin);
                inputs.push(BlockInput {
                    tx_idx: tx_i as u32, input_idx: i as u32,
                    global_pos: 0, coin_height: ch_i, coin_is_coinbase: cb_i as u32, coin_mtp: mtp_i, tx_first: (i == 0) as u32,
                    proof_i: WireProof { leaf: coin, position: 0, siblings: vec![] },
                    proof_last: WireProof { leaf: coin, position: 0, siblings: vec![] },
                });
                continue;
            }
            let pos = forest.find(&coin).expect("input coin in accumulator");
            let last = forest.leaves.len() - 1;
            let mut global_pos = pos as u64;
            if sec2_bad && inputs.is_empty() {
                // a different but in-range index -> membership proof stays valid, position is a lie
                global_pos = if (pos as u64) < last as u64 { pos as u64 + 1 } else { (pos as u64).saturating_sub(1) };
                eprintln!("[SEC2-TEST] corrupting first spend global_pos {} -> {} (proof_i left honest)", pos, global_pos);
            }
            inputs.push(BlockInput {
                tx_idx: tx_i as u32, input_idx: i as u32,
                global_pos, coin_height: ch_i, coin_is_coinbase: cb_i as u32, coin_mtp: mtp_i, tx_first: (i == 0) as u32,
                proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)),
            });
            forest.delete(pos);
        }
    }
    // Add the SURVIVING created outputs (cmtp computed above) — unspendable skipped AND in-block-spent
    // cancelled (leaf ∈ spent_in_block), matching the guest's surviving set exactly.
    let mut new_outputs: Vec<[u8; 32]> = Vec::new();
    for v in 0..coinbase.output.len() { if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; } let l = out_leaf_of(&coinbase, &cb_txid, v, height, true, cmtp); if spent_in_block.contains(&l) { continue; } forest.add(l); new_outputs.push(l); }
    for p in &ptxs { for v in 0..p.tx.output.len() { if !out_spendable(p.tx.output[v].script_pubkey.as_bytes()) { continue; } let l = out_leaf_of(&p.tx, &p.txid, v, height, false, cmtp); if spent_in_block.contains(&l) { continue; } forest.add(l); new_outputs.push(l); } }
    let root_next = wire_stump(&forest);
    let (in_smt_root, smt) = smt_witness_standalone(cb_txid, cb_spendable_outputs(&coinbase),
        &cb_spends_from(&inputs, &txs));
    let mut w = BlockWitness { header, height, coinbase_tx: hx(cb_hex), txids, wtxids, root_prev, txs, tx_prevouts, inputs, new_outputs, root_next, bip30: None, in_smt_root, smt };
    // --- reject-path negative-test hooks (test-only, inert unless the env var is set; NEVER in production) ---
    // Each corrupts exactly one consensus input so check-full drives the matching guest flag false, closing
    // the retarget / block-weight / sigop-cost coverage gap so those reject-paths are continuously CI-enforced.
    if std::env::var("HAZYNC_TEST_BADNBITS").is_ok() {
        // Non-boundary block: retarget_ok requires nBits == prev_nbits. Corrupt prev_nbits → retarget_ok=false.
        anchor.prev_nbits = anchor.prev_nbits.wrapping_add(0x1000);
    }
    if std::env::var("HAZYNC_TEST_BADWEIGHT").is_ok() {
        // Append a ~1.1 MB OP_RETURN output to the coinbase → block weight > MAX_BLOCK_WEIGHT → weight_ok=false.
        let mut cb: Transaction = deserialize(&hx(cb_hex)).unwrap();
        cb.output.push(TxOut { value: Amount::from_sat(0), script_pubkey: ScriptBuf::from_bytes(vec![0x6au8; 1_100_000]) });
        w.coinbase_tx = serialize(&cb);
    }
    if std::env::var("HAZYNC_TEST_BADSIGOPS").is_ok() {
        // Append an output of 30001 OP_CHECKSIG → sigop cost 30001*4 = 120004 > 80000 → sigops_ok=false.
        let mut cb: Transaction = deserialize(&hx(cb_hex)).unwrap();
        cb.output.push(TxOut { value: Amount::from_sat(0), script_pubkey: ScriptBuf::from_bytes(vec![0xacu8; 30_001]) });
        w.coinbase_tx = serialize(&cb);
    }
    (anchor, w)
}

fn prove_full() {
    use std::time::Instant;
    let (anchor, w) = build_full();
    println!("=== PROVING REAL BLOCK {} ({} inputs) — full consensus, monolithic, on GPU ===", w.height, w.inputs.len());
    let t = Instant::now();
    let r = prove_step(state_journal_bytes(&anchor), None, &w, 1);
    let tip: ChainState = r.journal.decode().unwrap();
    assert!(tip.self_id == METHOD_ID, "S1: proof recursed against wrong image id");
    println!(">>> BLOCK {} PROVED in {:.1}s — receipt VERIFIED.", w.height, t.elapsed().as_secs_f64());
    println!("  tip_hash {}  cum_work {}  UTXO leaves {}", hex(&tip.tip_hash), work_u128(&tip.cum_work), tip.utxo_leaves);
}

// CHECK-FULL: execute-mode (no proving) validation of the HAZYNC_BLOCK — runs the exact same guest
// consensus path as prove_full (mode 2, is_base=1). Guest asserts block_valid, so a clean execute ==
// every rule passed (scripts, no-inflation, PoW, retarget, merkle, subsidy, weight, sigops, witness
// commitment, BIP34, BIP30, and now REAL maturity/BIP68 from the S2 metadata). Cheap pre-flight before
// Isolated exerciser for the real maturity/BIP68 relative-lock check (guest mode 8). Builds a minimal
// v2 tx with one input carrying the given nSequence, then runs `check_input_locks` with real MTP
// numbers supplied via env vars. Lets us drive the time-based branch (which no tested block exercises)
// with real mainnet MTP data. Return codes: 1 valid, -40 immature coinbase, -41 height-lock unmet,
// -42 time-lock unmet.
// COV-2 negative test: demonstrate the merkle mutation (CVE-2012-2459) check. An honest 3-tx list
// [A,B,C] and a malleated 4-tx list [A,B,C,C] (last tx duplicated) produce the SAME merkle root — the
// classic malleability — but the real Core ComputeMerkleRoot flags the second as `mutated`. Our
// `merkle_ok` requires `mutated == 0`, so the malleated block is rejected.
fn test_merkle_cmd() {
    let run = |txids: &[[u8; 32]]| -> ([u8; 32], u8) {
        let flat: Vec<u8> = txids.iter().flatten().copied().collect();
        let mut b = ExecutorEnv::builder();
        b.write(&9u32).unwrap();
        b.write(&flat).unwrap();
        let s = default_executor().execute(b.build().unwrap(), METHOD_ELF).expect("exec");
        s.journal.decode().unwrap()
    };
    let (a, bb, c) = ([0x11u8; 32], [0x22u8; 32], [0x33u8; 32]);
    let (root_n, mut_n) = run(&[a, bb, c]);      // honest 3-tx block
    let (root_m, mut_m) = run(&[a, bb, c, c]);   // malleated: last tx duplicated (CVE-2012-2459)
    println!("normal  [A,B,C]   : merkle {}  mutated={}  (merkle_ok: {})", hex(&root_n), mut_n, mut_n == 0);
    println!("mutated [A,B,C,C] : merkle {}  mutated={}  (merkle_ok: {})", hex(&root_m), mut_m, mut_m == 0);
    println!("SAME root (CVE collision): {}  -> the malleated block is REJECTED on merkle_ok (mutated=1)",
        root_n == root_m);
}

fn test_locks_cmd() {
    let ev = |k: &str, d: u32| std::env::var(k).ok().and_then(|s| s.parse().ok()).unwrap_or(d);
    let seq = ev("HAZYNC_LOCK_SEQ", 0);
    let coin_mtp = ev("HAZYNC_LOCK_COINMTP", 0);
    let spend_mtp = ev("HAZYNC_LOCK_SPENDMTP", 0);
    let coin_h = ev("HAZYNC_LOCK_COINH", 100);
    let spend_h = ev("HAZYNC_LOCK_SPENDH", 200);
    let cb = ev("HAZYNC_LOCK_CB", 0);
    // Real-tx mode: if HAZYNC_LOCK_RAWTX (hex) is set, feed the ACTUAL mainnet tx bytes to the real
    // check_input_locks (its version + vin[idx].nSequence are read from these). Else build a minimal
    // synthetic v2 tx carrying HAZYNC_LOCK_SEQ.
    let input_idx: u32 = std::env::var("HAZYNC_LOCK_IDX").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
    let raw: Vec<u8> = if let Ok(h) = std::env::var("HAZYNC_LOCK_RAWTX") {
        hx(h.trim())
    } else {
        let mut raw: Vec<u8> = Vec::new();
        raw.extend_from_slice(&2u32.to_le_bytes()); // version 2
        raw.push(1); // vin count
        raw.extend_from_slice(&[0u8; 32]); // prev txid
        raw.extend_from_slice(&0u32.to_le_bytes()); // prev vout
        raw.push(0); // scriptSig len
        raw.extend_from_slice(&seq.to_le_bytes()); // nSequence
        raw.push(1); // vout count
        raw.extend_from_slice(&0u64.to_le_bytes()); // value
        raw.push(0); // scriptPubKey len
        raw.extend_from_slice(&0u32.to_le_bytes()); // locktime
        raw
    };
    let mut b = ExecutorEnv::builder();
    b.write(&8u32).unwrap();
    b.write(&raw).unwrap();
    b.write(&input_idx).unwrap(); // input_idx
    b.write(&coin_h).unwrap();
    b.write(&cb).unwrap();
    b.write(&coin_mtp).unwrap();
    b.write(&spend_h).unwrap();
    b.write(&spend_mtp).unwrap();
    let s = default_executor().execute(b.build().unwrap(), METHOD_ELF).expect("exec");
    let rc: i32 = s.journal.decode().unwrap();
    let meaning = match rc { 1 => "VALID", -40 => "REJECT immature-coinbase", -41 => "REJECT height-lock-unmet", -42 => "REJECT time-lock-unmet", _ => "?" };
    // display the ACTUAL nSequence the check read (from the real tx bytes, not the synthetic var)
    let disp_seq = deserialize::<Transaction>(&raw).map(|t| t.input[input_idx as usize].sequence.0).unwrap_or(seq);
    println!("LOCKS rc={} ({})  [nSequence={:#010x} coin_mtp={} spend_mtp={} coin_h={} spend_h={} cb={}]",
        rc, meaning, disp_seq, coin_mtp, spend_mtp, coin_h, spend_h, cb);
}

// committing a multi-GPU prove; a false flag panics here in seconds-to-minutes on CPU, not hours on GPU.
fn check_full() {
    use std::time::Instant;
    let (anchor, w) = build_full();
    if std::env::var("HAZYNC_WITNESS_SIZES").is_ok() {
        let tot = risc0_zkvm::serde::to_vec(&w).unwrap().len() * 4;
        let n = w.inputs.len();
        let rawtx: usize = w.txs.iter().map(|t| t.0.len()).sum(); // de-duplicated: one blob per tx
        let prevouts: usize = w.tx_prevouts.iter().map(|t| t.0.len()).sum();
        let sibs: usize = w.inputs.iter().map(|i| (i.proof_i.siblings.len() + i.proof_last.siblings.len()) * 32).sum();
        println!("  txs(deduped)={} raw_tx bytes={} prevouts bytes={}", w.txs.len(), rawtx, prevouts);
        let idlists = (w.txids.len() + w.wtxids.len()) * 32;
        let outs = w.new_outputs.len() * 32;
        let pct = |x: usize| if tot > 0 { x as f64 / tot as f64 * 100.0 } else { 0.0 };
        println!("WITNESS block {} inputs={} total={}B", w.height, n, tot);
        println!("  proof_siblings = {}B ({:.1}%)   raw_tx = {}B ({:.1}%)   prevouts = {}B ({:.1}%)", sibs, pct(sibs), rawtx, pct(rawtx), prevouts, pct(prevouts));
        println!("  txids+wtxids = {}B ({:.1}%)   new_outputs = {}B ({:.1}%)", idlists, pct(idlists), outs, pct(outs));
        return;
    }
    println!("=== CHECK-FULL (execute, no proof) block {} — {} inputs ===", w.height, w.inputs.len());
    let mut b = ExecutorEnv::builder();
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    let t = Instant::now();
    // Do NOT collapse every executor error into "the guest rejected the block". An environment failure
    // and a consensus rejection need opposite responses, and reporting the first as the second sends you
    // hunting a consensus bug that does not exist: a missing r0vm binary reported itself as "guest
    // asserted a consensus flag false", which cost two rounds of diagnosis. IO errors name a file or a
    // process; a guest rejection never does.
    let s = match default_executor().execute(b.build().unwrap(), METHOD_ELF) {
        Ok(s) => s,
        Err(e) => {
            let msg = e.to_string();
            if msg.contains("No such file or directory")
                || msg.contains("os error")
                || msg.contains("Permission denied")
                || msg.contains("server version")
            {
                eprintln!("CHECK-FULL could not RUN. This is an ENVIRONMENT failure, not a consensus");
                eprintln!("rejection -- the block was never validated.");
                eprintln!("  underlying: {msg}");
                eprintln!();
                eprintln!("  Most likely the external r0vm server is missing: default_executor() spawns");
                eprintln!("  it unless the binary was built with an in-process prover. Run inside the");
                eprintln!("  build container, or use a binary with the prover linked in.");
                std::process::exit(2);
            }
            panic!("CHECK-FULL FAILED: the guest REJECTED the block -- a consensus flag came back false \
                    (see the guest message above).\n  underlying: {msg}");
        }
    };
    let tip: ChainState = s.journal.decode().unwrap();
    println!(">>> BLOCK {} VALID (execute {:.0}s, {} cycles) — all consensus flags true.",
        w.height, t.elapsed().as_secs_f64(), s.cycles());
    println!("  tip_hash {}  cum_work {}  UTXO leaves {}", hex(&tip.tip_hash), work_u128(&tip.cum_work), tip.utxo_leaves);
}

// ===================== IBD / tip proof-chain driver (Tests 1 & 2) ============================
// Fold the recursive validity chain over a directory of per-block witnesses (block_<h>.json, the exact
// shape the archive-node bridge and fetch_block.py emit), carrying the REAL UTXO accumulator
// from the genesis anchor. This is what closes S3: each spent coin's inclusion proof binds it to the
// real UTXO set built from genesis, not a fabricated per-block root.

// Mainnet genesis (the unconditional trusted anchor — its hash/params are consensus constants).
const GENESIS_HASH: &str = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f";
const GENESIS_TIME: u32 = 1_231_006_505;
const GENESIS_BITS: u32 = 0x1d00ffff;
const GENESIS_WORK: u128 = 4_295_032_833; // GetBlockProof(0x1d00ffff): cumulative work through block 0.

fn arr_u128(x: u128) -> [u8; 32] { let mut a = [0u8; 32]; a[..16].copy_from_slice(&x.to_le_bytes()); a }

// State just after genesis (height 0), before block 1. UTXO set empty: the genesis coinbase is
// unspendable and (per Core) never enters the UTXO set, so the accumulator starts empty.
fn genesis_anchor() -> ChainState {
    ChainState {
        kind: KIND_CHAIN,
        tip_hash: arr(rev(hx(GENESIS_HASH))), utxo_roots: Forest::new().roots(), utxo_leaves: 0,
        cum_work: arr_u128(GENESIS_WORK), height: 0,
        prev_nbits: GENESIS_BITS, prev_time: GENESIS_TIME, epoch_start: GENESIS_TIME,
        recent_times: vec![GENESIS_TIME], anchor_id: [0u8; 32], self_id: METHOD_ID,
    }
}

fn read_block_json(dir: &str, h: u32) -> serde_json::Value {
    let p = format!("{dir}/block_{h}.json");
    serde_json::from_str(&std::fs::read_to_string(&p).unwrap_or_else(|_| panic!("missing {p}"))).unwrap()
}

// Build a block witness from a bridge/fetcher JSON block, against and ADVANCING the running
// accumulator `forest`. Multi-input, multi-tx; root_prev = current forest, root_next = after this
// block's external spends + created outputs. (No in-block-spend handling: the guest deletes external
// inputs then adds outputs — a spent coin created in the same block would fail the position lookup
// below with a clear panic. Absent in the early chain this targets.)
// Advance the median-time-past window with this block and append `block_mtp[height] = MTP(height)`
// (median of the ≤11 most recent block timestamps, Core's `GetMedianTimePast`). Assumes blocks are fed
// in order from the genesis anchor (block_mtp is indexed by absolute height).
fn median_u32(v: &[u32]) -> u32 { let mut s = v.to_vec(); s.sort_unstable(); s[s.len() / 2] }

// Advance a median-time-past window by one block (mirrors the guest's chain_step recent_times update),
// so a demo driver can compute each block's create_mtp = median of the window through the prev block.
fn advance_recent(recent: &mut Vec<u32>, block_time: u32) {
    recent.push(block_time);
    if recent.len() > 11 { let n = recent.len() - 11; recent.drain(0..n); }
}

fn push_mtp(j: &serde_json::Value, win: &mut Vec<u32>, block_mtp: &mut Vec<u32>) {
    // block_mtp[h] = MTP(h-1): median of the window through the PREVIOUS block, matching Core's BIP68
    // creation time GetMedianTimePast(coinHeight-1) and the guest's median(prev.recent_times).
    block_mtp.push(median_u32(win));
    let bt = j["time"].as_u64().unwrap() as u32;
    win.push(bt);
    if win.len() > 11 { let n = win.len() - 11; win.drain(0..n); }
}

// `block_mtp[h]` = GetMedianTimePast() of the block at height h (the host derives it from the chain it
// has already processed — same value an archive node holds for free). Used as the coin's creation-MTP
// on BOTH sides: committed when an output is created here, and looked up by the coin's committed height
// when it is spent. This is the real BIP68-time value (Core's median-time-past), replacing the earlier
// raw-block-timestamp proxy, and it stays consistent so the accumulator leaf matches across the coin's
// life.
fn build_block_carried(forest: &mut Forest, j: &serde_json::Value, block_mtp: &[u32]) -> BlockWitness {
    let height = j["height"].as_u64().unwrap() as u32;
    let bits = j["bits"].as_u64().unwrap() as u32;
    let time = j["time"].as_u64().unwrap() as u32;
    let nonce = j["nonce"].as_u64().unwrap() as u32;
    let version = j["version"].as_i64().unwrap_or(1) as i32;
    let prev = j["prev"].as_str().unwrap();
    let merkle = j["merkle"].as_str().unwrap();
    let cb_hex = j["coinbase_hex"].as_str().unwrap();
    let header = build_header_v(version, prev, &arr(rev(hx(merkle))), time, bits, nonce);
    let coinbase: Transaction = deserialize(&hx(cb_hex)).unwrap();
    let cb_txid = coinbase.compute_txid().to_byte_array();

    let root_prev = wire_stump(forest);
    let mut txids: Vec<[u8; 32]> = vec![cb_txid];
    let mut wtxids: Vec<[u8; 32]> = vec![[0u8; 32]]; // coinbase wtxid convention (pre-segwit: unused)
    let mut inputs: Vec<BlockInput> = Vec::new();
    let mut txs: Vec<PackedBytes> = Vec::new();
    let mut tx_prevouts: Vec<PackedBytes> = Vec::new();

    struct P { raw: Vec<u8>, tx: Transaction, prevouts: Vec<TxOut>, meta: Vec<(u32, bool, u32)>, txid: [u8; 32] }
    let mut ptxs: Vec<P> = Vec::new();
    for tx in j["txs"].as_array().unwrap() {
        let raw = hx(tx["raw"].as_str().unwrap());
        let t: Transaction = deserialize(&raw).unwrap();
        let mut prevouts = Vec::new();
        let mut meta = Vec::new();
        for p in tx["prevouts"].as_array().unwrap() {
            prevouts.push(TxOut {
                value: Amount::from_sat(p["value"].as_u64().unwrap()),
                script_pubkey: ScriptBuf::from_bytes(hx(p["spk"].as_str().unwrap())),
            });
            let h = p["coin_height"].as_u64().map(|x| x as u32).unwrap_or(0);
            let cb = p["coin_is_coinbase"].as_u64().map(|x| x != 0).unwrap_or(false);
            // Real BIP68-time value: the median-time-past of the coin's CREATION block, derived by the
            // host (not the JSON's raw block timestamp). Matches what was committed when the coin was
            // created (below), so the leaf is found in the accumulator.
            let mtp = block_mtp.get(h as usize).copied().unwrap_or(0);
            meta.push((h, cb, mtp));
        }
        let txid = t.compute_txid().to_byte_array();
        ptxs.push(P { raw, tx: t, prevouts, meta, txid });
    }

    // This block's created output leaves (coinbase + every tx, unspendable skipped) — the guest detects
    // in-block spends (H1) by LEAF membership here (main.rs `created_at`), NOT by txid. A leaf carries the
    // coin's height, so a spend of an earlier coin whose funding tx shares a txid with a tx in this block
    // (pre-BIP34 coinbase-txid collision) has a DIFFERENT leaf and stays correctly external — keying on
    // txid would false-positive it, diverge from the guest, and stall the frontier at that block.
    let self_mtp = block_mtp.get(height as usize).copied().unwrap_or(time);
    let mut created: std::collections::HashSet<Hash> = std::collections::HashSet::new();
    for v in 0..coinbase.output.len() {
        if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
        created.insert(out_leaf_of(&coinbase, &cb_txid, v, height, true, self_mtp));
    }
    for p in &ptxs {
        for v in 0..p.tx.output.len() {
            if !out_spendable(p.tx.output[v].script_pubkey.as_bytes()) { continue; }
            created.insert(out_leaf_of(&p.tx, &p.txid, v, height, false, self_mtp));
        }
    }
    let mut spent_in_block: std::collections::HashSet<Hash> = std::collections::HashSet::new();

    for (tx_i, p) in ptxs.iter().enumerate() {
        txids.push(p.txid);
        wtxids.push(p.tx.compute_wtxid().to_byte_array());
        let prevouts_blob = serialize(&p.prevouts);
        txs.push(PackedBytes(p.raw.clone()));
        tx_prevouts.push(PackedBytes(prevouts_blob));
        for i in 0..p.tx.input.len() {
            let (ch, cb, mtp) = p.meta[i];
            let o = &p.prevouts[i];
            let op = p.tx.input[i].previous_output;
            let coin = coin_leaf(&op.txid.to_byte_array(), op.vout, o.value.to_sat(), o.script_pubkey.as_bytes(), ch, cb, mtp);
            if created.contains(&coin) {
                // IN-BLOCK (H1): leaf matches a coin created earlier in this block (the guest's exact
                // rule) — never entered the accumulator: dummy proof, no delete. Script still verifies.
                spent_in_block.insert(coin);
                inputs.push(BlockInput {
                    tx_idx: tx_i as u32, input_idx: i as u32,
                    global_pos: 0, coin_height: ch, coin_is_coinbase: cb as u32, coin_mtp: mtp, tx_first: (i == 0) as u32,
                    proof_i: WireProof { leaf: coin, position: 0, siblings: vec![] },
                    proof_last: WireProof { leaf: coin, position: 0, siblings: vec![] },
                });
            } else {
                // EXTERNAL: prove inclusion in the carried forest, delete.
                let pos = forest.find(&coin)
                    .expect("spent coin not in carried accumulator (bad metadata)");
                let last = forest.leaves.len() - 1;
                inputs.push(BlockInput {
                    tx_idx: tx_i as u32, input_idx: i as u32,
                    global_pos: pos as u64, coin_height: ch, coin_is_coinbase: cb as u32, coin_mtp: mtp, tx_first: (i == 0) as u32,
                    proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)),
                });
                forest.delete(pos);
            }
        }
    }

    // Created coins: coinbase outputs then each tx's outputs, in canonical order — skipping unspendable
    // outputs (H3) and coins spent within this same block (H1). Must match the guest's surviving set.
    // This block's creation-MTP (committed on every output leaf); a later block spending these coins
    // looks up the identical value by height, so the leaves match.
    // self_mtp computed above (with `created`). SURVIVING outputs only: unspendable skipped AND
    // in-block-spent cancelled (leaf ∈ spent_in_block), matching the guest's surviving set exactly.
    let mut new_outputs = Vec::new();
    let add_out = |tx: &Transaction, txid: &[u8; 32], is_cb: bool, forest: &mut Forest, no: &mut Vec<Hash>| {
        for v in 0..tx.output.len() {
            if !out_spendable(tx.output[v].script_pubkey.as_bytes()) { continue; }
            let l = out_leaf_of(tx, txid, v, height, is_cb, self_mtp);
            if spent_in_block.contains(&l) { continue; }
            forest.add(l);
            no.push(l);
        }
    };
    // F3: BIP30 grandfathered duplicate-coinbase overwrite. Blocks 91842 (dup of 91812) and 91880 (dup
    // of 91722) re-use an earlier still-unspent coinbase's outpoint; Core OVERWRITES the old coin. Delete
    // the superseded coinbase output leaf(s) — this same coinbase's spendable outputs committed at the OLD
    // height/MTP — from the carried forest BEFORE add_out re-adds them at the current height, and hand the
    // guest the deletion witness. Without this the bridge emits bip30:None and the guest (which MANDATES
    // the overwrite at these two hashes) rejects the block, making 91842/91880 unprovable via the bridge.
    // old_mtp = block_mtp[old_height] = MTP(old_height-1) = exactly what the superseded leaf committed at
    // creation, so the leaf is found. Height-gated here; the guest cross-checks the block HASH
    // (BIP30_OVERWRITE_A/B), so a spurious height hit on a non-matching block is still rejected there.
    let bip30 = if height == 91842 || height == 91880 {
        let old_height: u32 = if height == 91842 { 91812 } else { 91722 };
        let old_mtp = block_mtp.get(old_height as usize).copied().unwrap_or(0);
        let mut dels: Vec<Bip30Del> = Vec::new();
        for v in 0..coinbase.output.len() {
            if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
            let l = out_leaf_of(&coinbase, &cb_txid, v, old_height, true, old_mtp);
            let pos = forest.find(&l)
                .expect("BIP30 superseded coinbase leaf not in carried accumulator");
            let last = forest.leaves.len() - 1;
            dels.push(Bip30Del { global_pos: pos as u64, proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)) });
            forest.delete(pos);
        }
        Some(Bip30Overwrite { old_height, old_mtp, dels })
    } else {
        None
    };
    add_out(&coinbase, &cb_txid, true, forest, &mut new_outputs);
    for p in &ptxs {
        add_out(&p.tx, &p.txid, false, forest, &mut new_outputs);
    }
    let root_next = wire_stump(forest);
    let (in_smt_root, smt) = smt_witness_standalone(cb_txid, cb_spendable_outputs(&coinbase),
        &cb_spends_from(&inputs, &txs));
    BlockWitness { header, height, coinbase_tx: hx(cb_hex), txids, wtxids, root_prev, txs, tx_prevouts, inputs, new_outputs, root_next, bip30, in_smt_root, smt }
}

// Prove one chain step to a SUCCINCT receipt (FIX A): cheap composition for a long recursive chain.
fn prove_step_succinct(prev_journal: Vec<u8>, prev_receipt: Option<risc0_zkvm::Receipt>, w: &BlockWitness, is_base: u32) -> risc0_zkvm::Receipt {
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    if let Some(r) = prev_receipt { b.add_assumption(r); }
    b.write(&2u32).unwrap();
    b.write(&prev_journal).unwrap();
    b.write(w).unwrap();
    b.write(&is_base).unwrap();
    b.write(&METHOD_ID).unwrap();
    let receipt = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct()).expect("prove ibd step").receipt;
    receipt.verify(METHOD_ID).expect("ibd step verify");
    receipt
}

fn ibd_range() -> (String, u32, u32) {
    let dir = std::env::var("HAZYNC_WITNESS_DIR").expect("set HAZYNC_WITNESS_DIR");
    let from: u32 = std::env::var("HAZYNC_FROM").ok().and_then(|s| s.parse().ok()).unwrap_or(1);
    let to: u32 = std::env::var("HAZYNC_TO").expect("set HAZYNC_TO to the last block height").parse().unwrap();
    (dir, from, to)
}

// CHECK-IBD: execute-mode fold from the genesis anchor over [from..=to]. Validates every block's full
// consensus AND the real carried-accumulator transition + chain linkage + retarget — no proving.
fn check_ibd() {
    use std::time::Instant;
    let (dir, from, to) = ibd_range();
    println!("=== CHECK-IBD (execute) genesis-anchor → fold blocks {from}..={to} from {dir} ===");
    let mut forest = Forest::new();
    let mut state = genesis_anchor();
    let t = Instant::now();
    let mut total_cyc = 0u64;
    let mut block_mtp: Vec<u32> = vec![GENESIS_TIME]; // index = height; [0] = genesis
    let mut win: Vec<u32> = vec![GENESIS_TIME];       // rolling ≤11 block times for MTP
    for h in from..=to {
        let j = read_block_json(&dir, h);
        push_mtp(&j, &mut win, &mut block_mtp);
        let w = build_block_carried(&mut forest, &j, &block_mtp);
        let (ns, cyc) = chain_step(&state, &w, if h == from { 1 } else { 0 });
        assert_eq!(ns.height, h, "block {h}: height did not advance");
        state = ns;
        total_cyc += cyc;
        if h % 200 == 0 || h == to {
            println!("  folded {h}: tip {} leaves {} cum_work {} ({:.0}s)", hex(&state.tip_hash), state.utxo_leaves, work_u128(&state.cum_work), t.elapsed().as_secs_f64());
        }
    }
    println!(">>> CHECK-IBD {from}..{to} VALID ({:.0}s, {}M cyc). tip_hash {}  cum_work {}  UTXO leaves {}",
        t.elapsed().as_secs_f64(), total_cyc / 1_000_000, hex(&state.tip_hash), work_u128(&state.cum_work), state.utxo_leaves);
}

// PROVE-IBD: the real recursive STARK chain from genesis (Test 1), then an explicit incremental
// tip-extension phase (Test 2 — each block folds onto the existing tip proof, the marginal cost of
// proving a new block AT THE TIP). HAZYNC_TIP=<n> folds n extra blocks past HAZYNC_TO one at a time.
fn prove_ibd() {
    use std::time::Instant;
    let (dir, from, to) = ibd_range();
    let tip_extra: u32 = std::env::var("HAZYNC_TIP").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
    println!("=== PROVE-IBD: recursive validity chain, genesis → block {to} (Test 1){} ===",
        if tip_extra > 0 { format!(", then +{tip_extra} tip extensions (Test 2)") } else { String::new() });
    let mut forest = Forest::new();
    let mut state = genesis_anchor();
    let mut prev_receipt: Option<risc0_zkvm::Receipt> = None;
    let t = Instant::now();
    let mut block_mtp: Vec<u32> = vec![GENESIS_TIME];
    let mut win: Vec<u32> = vec![GENESIS_TIME];
    for h in from..=to {
        let j = read_block_json(&dir, h);
        push_mtp(&j, &mut win, &mut block_mtp);
        let w = build_block_carried(&mut forest, &j, &block_mtp);
        let is_base = (h == from) as u32;
        let prev_journal = match &prev_receipt { Some(r) => r.journal.bytes.clone(), None => state_journal_bytes(&state) };
        let st = Instant::now();
        let r = prove_step_succinct(prev_journal, prev_receipt.clone(), &w, is_base);
        state = r.journal.decode().unwrap();
        prev_receipt = Some(r);
        if h % 50 == 0 || h == to || h == from {
            println!("  [IBD] proved block {h}: tip {} ({:.1}s this block, {:.0}s cum)", hex(&state.tip_hash), st.elapsed().as_secs_f64(), t.elapsed().as_secs_f64());
        }
    }
    println!(">>> IBD CHAIN PROOF genesis→{to} in {:.0}s — receipt VERIFIED. tip_hash {}  cum_work {}  UTXO leaves {}",
        t.elapsed().as_secs_f64(), hex(&state.tip_hash), work_u128(&state.cum_work), state.utxo_leaves);

    // Test 2: incremental tip proving — each block extends the existing chain proof by one step.
    for h in (to + 1)..=(to + tip_extra) {
        let j = read_block_json(&dir, h);
        push_mtp(&j, &mut win, &mut block_mtp);
        let w = build_block_carried(&mut forest, &j, &block_mtp);
        let prev_journal = prev_receipt.as_ref().unwrap().journal.bytes.clone();
        let st = Instant::now();
        let r = prove_step_succinct(prev_journal, prev_receipt.clone(), &w, 0);
        state = r.journal.decode().unwrap();
        prev_receipt = Some(r);
        println!("  [TIP] block {h} validated + folded onto the chain proof in {:.1}s — tip now {} (height {})",
            st.elapsed().as_secs_f64(), hex(&state.tip_hash), state.height);
    }
    if tip_extra > 0 {
        println!(">>> TIP PROOF at height {} — one verified receipt attests genesis→{} valid. Marginal cost per tip block above.", state.height, state.height);
    }
}

// ===================== PARALLEL RANGE-FOLD (backfill) =========================================
// Prove each block INDEPENDENTLY as a range [N..N] (parallel across GPUs), then fold adjacent ranges
// pairwise in a tree (parallel, log-depth). Replaces the sequential chain for backfill. The in-boundary
// of each block comes from a cheap host "bridge pass" (fold the accumulator, no proving).

// Host mirror of the guest RangeState (identical field order — journal decodes into this).
#[derive(serde::Serialize, serde::Deserialize)]
struct RangeState {
    kind: u32, // H8: == KIND_RANGE
    lo: u32, hi: u32,
    in_tip_hash: [u8; 32], in_roots: Vec<Option<[u8; 32]>>, in_leaves: u64,
    in_nbits: u32, in_time: u32, in_epoch_start: u32, in_recent: Vec<u32>,
    in_smt_root: [u8; 32],
    out_tip_hash: [u8; 32], out_roots: Vec<Option<[u8; 32]>>, out_leaves: u64,
    out_nbits: u32, out_time: u32, out_epoch_start: u32, out_recent: Vec<u32>,
    out_smt_root: [u8; 32],
    range_work: [u8; 32], self_id: [u32; 8],
}

fn add256_host(a: &mut [u8; 32], b: &[u8; 32]) {
    let mut carry = 0u16;
    for i in 0..32 { let s = a[i] as u16 + b[i] as u16 + carry; a[i] = s as u8; carry = s >> 8; }
}

struct InCtx { roots: Vec<Option<[u8; 32]>>, leaves: u64, nbits: u32, time: u32, epoch_start: u32, recent: Vec<u32>, block_mtp: Vec<u32> }

// Cheap bridge pass: fold blocks 1..n (exclusive) advancing the accumulator + difficulty/MTP context,
// returning the forest + the chain context just BEFORE block n. No proving — pure host replay.
fn bridge_pass(dir: &str, n: u32) -> (Forest, InCtx) {
    let mut forest = Forest::new();
    let (mut nbits, mut time, mut epoch_start) = (GENESIS_BITS, GENESIS_TIME, GENESIS_TIME);
    let mut recent = vec![GENESIS_TIME];
    let mut block_mtp: Vec<u32> = vec![GENESIS_TIME];
    for h in 1..n {
        let j = read_block_json(dir, h);
        push_mtp(&j, &mut recent, &mut block_mtp); // advances the MTP window + block_mtp[h] BEFORE build
        let _ = build_block_carried(&mut forest, &j, &block_mtp); // advances the forest (spends + outputs)
        let bt = j["time"].as_u64().unwrap() as u32;
        nbits = j["bits"].as_u64().unwrap() as u32;
        time = bt;
        if h % 2016 == 0 { epoch_start = bt; }
    }
    let s = wire_stump(&forest);
    (forest, InCtx { roots: s.roots, leaves: s.num_leaves, nbits, time, epoch_start, recent, block_mtp })
}

// `prove-range <n>`: prove block n as a self-contained range [n..n] → range_<n>.bin (parallelisable).
fn prove_range_cmd(n: u32) {
    use std::time::Instant;
    let dir = std::env::var("HAZYNC_WITNESS_DIR").expect("set HAZYNC_WITNESS_DIR");
    let (mut forest, ctx) = bridge_pass(&dir, n);
    let jn = read_block_json(&dir, n);
    let mut block_mtp = ctx.block_mtp.clone();
    let mut win = ctx.recent.clone();
    push_mtp(&jn, &mut win, &mut block_mtp); // block_mtp[n]
    let w = build_block_carried(&mut forest, &jn, &block_mtp);
    let in_tip_hash = arr(rev(hx(jn["prev"].as_str().unwrap())));
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&6u32).unwrap();
    b.write(&in_tip_hash).unwrap();
    b.write(&ctx.roots).unwrap();
    b.write(&ctx.leaves).unwrap();
    b.write(&ctx.nbits).unwrap();
    b.write(&ctx.time).unwrap();
    b.write(&ctx.epoch_start).unwrap();
    b.write(&ctx.recent).unwrap();
    b.write(&w).unwrap();
    b.write(&METHOD_ID).unwrap();
    let t = Instant::now();
    let receipt = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct()).expect("prove range").receipt;
    receipt.verify(METHOD_ID).expect("range verify");
    let out = std::env::var("HAZYNC_OUT").unwrap_or_else(|_| format!("range_{n}.bin"));
    std::fs::write(&out, bincode::serialize(&receipt).unwrap()).unwrap();
    println!("proved range [{n}..{n}] in {:.1}s -> {out}", t.elapsed().as_secs_f64());
}

// `fold-range <left.bin> <right.bin> <out.bin>`: verify both adjacent range proofs, fold into one.
fn fold_range_cmd(left: &str, right: &str, out: &str) {
    use std::time::Instant;
    let lr: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(left).expect("left")).unwrap();
    let rr: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(right).expect("right")).unwrap();
    lr.verify(METHOD_ID).expect("left verify");
    rr.verify(METHOD_ID).expect("right verify");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.add_assumption(lr.clone());
    b.add_assumption(rr.clone());
    b.write(&7u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    b.write(&lr.journal.bytes).unwrap();
    b.write(&rr.journal.bytes).unwrap();
    let t = Instant::now();
    let receipt = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct()).expect("fold range").receipt;
    receipt.verify(METHOD_ID).expect("fold verify");
    let rs: RangeState = receipt.journal.decode().unwrap();
    std::fs::write(out, bincode::serialize(&receipt).unwrap()).unwrap();
    println!("folded -> range [{}..{}] in {:.1}s -> {out}", rs.lo, rs.hi, t.elapsed().as_secs_f64());
}

// `extend-spine <spine.bin> <next.bin> <out.bin>`: advance the genesis-anchored spine by absorbing one
// adjacent chunk. This is #30's whole point — the spine must EXTEND, never be re-folded from scratch.
// Re-folding [1..N] every time the board grows makes the cost both recur and grow with the board,
// which is what made a one-off fold look like a 21-hour job that gets worse every day you wait.
//
//     spine [1..N]  +  chunk [N+1..M]   ->   spine [1..M]
//
// Mechanically this is `fold-range`. What it adds is the checks around the fold, in both directions:
//
//   BEFORE — adjacency and the full seam are checked on the HOST, in milliseconds. The guest checks
//   the seam too (mode 7), so an unchecked mismatch is caught either way — but it is caught after a
//   multi-second GPU fold and reported as a guest panic. Absorption is the one serial step in the
//   system; making its failures cheap and legible is worth the duplication.
//
//   AFTER — the result must still be genesis-anchored AND must actually have advanced. A fold that
//   silently returned the left operand would verify perfectly and stall the spine forever, so the
//   post-conditions assert hi/out_tip moved to the chunk's.
fn extend_spine_cmd(spine: &str, next: &str, out: &str) {
    use std::time::Instant;
    let sr: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(spine).expect("spine")).unwrap();
    let nr: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(next).expect("next")).unwrap();
    verify_receipt(&sr);
    verify_receipt(&nr);
    let ss: RangeState = sr.journal.decode().expect("spine journal is not a RangeState");
    let ns: RangeState = nr.journal.decode().expect("next journal is not a RangeState");

    assert!(ss.kind == KIND_RANGE, "spine is not a RangeState (domain tag)");
    assert!(ns.kind == KIND_RANGE, "next is not a RangeState (domain tag)");
    assert!(ss.self_id == METHOD_ID, "spine self_id != METHOD_ID");
    assert!(ns.self_id == METHOD_ID, "next self_id != METHOD_ID");

    // The spine must be genesis-anchored. Absorbing into a non-anchored range would produce a proof
    // that looks like a spine and attests nothing about genesis.
    assert_eq!(ss.lo, 1, "spine must start at block 1 — {} is not a spine", ss.lo);
    assert_genesis_in_boundary(&ss);

    assert_eq!(ns.lo, ss.hi + 1,
        "chunk is not adjacent: spine ends at {} so the chunk must start at {}, but starts at {}",
        ss.hi, ss.hi + 1, ns.lo);
    assert!(ns.hi >= ns.lo, "chunk has an empty range [{}..{}]", ns.lo, ns.hi);

    // Full seam equality. out_tip alone is not enough for the same reason the genesis pin is not just
    // in_tip: nbits/time/epoch_start/recent feed the retarget and MTP, and roots/leaves are the UTXO
    // carry. A seam that matched on tip alone could still splice two incompatible chain contexts.
    assert_eq!(ns.in_tip_hash, ss.out_tip_hash, "seam: chunk in_tip != spine out_tip");
    // Roots must be compared NORMALIZED. The accumulator's root vector carries empty slots for absent
    // levels, so the same UTXO set has more than one representation — [A, B] and [A, B, None] differ
    // as Vecs and are identical as accumulators. A raw compare rejects a perfectly valid adjacent
    // chunk whenever the two sides happen to carry different trailing padding.
    //
    // Found by running it: absorbing real board block 4 into [1..3] failed the seam with left
    // [Some(A), Some(B), None] against right [Some(A), Some(B)] — same roots, different padding. The
    // guest normalizes before comparing, so this host check was STRICTER than the thing it exists to
    // pre-empt, which is the worst way for a fail-fast check to be wrong: it turns a cheap early
    // warning into a false rejection of valid work.
    assert_eq!(normalize_host(ns.in_roots.clone()), normalize_host(ss.out_roots.clone()),
        "seam: chunk in_roots != spine out_roots (normalized)");
    assert_eq!(ns.in_leaves, ss.out_leaves, "seam: chunk in_leaves != spine out_leaves");
    assert_eq!(ns.in_nbits, ss.out_nbits, "seam: chunk in_nbits != spine out_nbits");
    assert_eq!(ns.in_time, ss.out_time, "seam: chunk in_time != spine out_time");
    assert_eq!(ns.in_epoch_start, ss.out_epoch_start, "seam: chunk in_epoch_start != spine out_epoch_start");
    assert_eq!(ns.in_recent, ss.out_recent, "seam: chunk in_recent != spine out_recent");

    println!("=== EXTEND SPINE [1..{}] + [{}..{}] -> [1..{}] ===", ss.hi, ns.lo, ns.hi, ns.hi);
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.add_assumption(sr.clone());
    b.add_assumption(nr.clone());
    b.write(&7u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    b.write(&sr.journal.bytes).unwrap();
    b.write(&nr.journal.bytes).unwrap();
    let t = Instant::now();
    let receipt = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct())
        .expect("extend spine").receipt;
    receipt.verify(METHOD_ID).expect("extended spine failed to verify");
    let rs: RangeState = receipt.journal.decode().expect("folded journal is not a RangeState");

    // Post-conditions: still a spine, and it moved.
    assert!(rs.kind == KIND_RANGE, "folded receipt is not a RangeState");
    assert_eq!(rs.lo, 1, "extended spine no longer starts at block 1");
    assert_genesis_in_boundary(&rs);
    assert_eq!(rs.hi, ns.hi, "extended spine ends at {} but the chunk ended at {}", rs.hi, ns.hi);
    assert_eq!(rs.out_tip_hash, ns.out_tip_hash, "extended spine out_tip != chunk out_tip");
    assert!(rs.hi > ss.hi, "spine did not advance: still ends at {}", rs.hi);

    std::fs::write(out, bincode::serialize(&receipt).unwrap()).unwrap();
    let mut total = arr_u128(GENESIS_WORK);
    add256_host(&mut total, &rs.range_work);
    println!("  spine [1..{}] in {:.1}s -> {out}", rs.hi, t.elapsed().as_secs_f64());
    println!("  out_tip_hash {}  total_cum_work {}  UTXO leaves {}",
        hex(&rs.out_tip_hash), work_u128(&total), rs.out_leaves);
    println!("  absorbed {} block(s); the spine is genesis-anchored and shippable as it stands.",
        ns.hi - ns.lo + 1);
}

// Pin the FULL genesis in-boundary of a range proof. in_tip alone is not enough: in_epoch_start feeds
// the first retarget (block 2016) via calc_next_bits and propagates unchanged across fold seams, so an
// unpinned value forges that retarget's difficulty (up to 4x easier) and understates cum_work; in_roots
// must be the empty accumulator (in_leaves==0 alone permits phantom roots); in_recent/in_time feed MTP.
fn assert_genesis_in_boundary(rs: &RangeState) {
    assert_eq!(rs.lo, 1, "genesis-connected range must start at block 1");
    assert_eq!(rs.in_tip_hash, arr(rev(hx(GENESIS_HASH))), "in-boundary tip != genesis hash");
    assert_eq!(rs.in_leaves, 0, "in-boundary UTXO set not empty");
    assert_eq!(rs.in_nbits, GENESIS_BITS, "in-boundary nbits != genesis");
    assert_eq!(rs.in_epoch_start, GENESIS_TIME, "in-boundary epoch_start != genesis time");
    assert_eq!(normalize_host(rs.in_roots.clone()), normalize_host(Forest::new().roots()), "in-boundary UTXO roots != empty");
    assert_eq!(rs.in_recent, vec![GENESIS_TIME], "in-boundary recent-times != [genesis time]");
    assert_eq!(rs.in_time, GENESIS_TIME, "in-boundary prev-time != genesis time");
}

// `verify-range <bin>`: verify a range proof and PIN its leftmost boundary to the genesis anchor.
fn verify_range_cmd(bin: &str) {
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(bin).expect("bin")).unwrap();
    verify_receipt(&r);
    let rs: RangeState = r.journal.decode().unwrap();
    assert!(rs.self_id == METHOD_ID, "self_id != METHOD_ID");
    assert!(rs.kind == KIND_RANGE, "receipt is not a RangeState (domain tag)"); // H8
    assert_eq!(rs.lo, 1, "range must start at block 1 (genesis-anchored)");
    assert_genesis_in_boundary(&rs);
    let mut total = arr_u128(GENESIS_WORK);
    add256_host(&mut total, &rs.range_work);
    println!(">>> RANGE PROOF [1..{}] VERIFIED — genesis-anchored, one succinct receipt.", rs.hi);
    println!("  out_tip_hash {}  range_work {}  total_cum_work {}  UTXO leaves {}",
        hex(&rs.out_tip_hash), work_u128(&rs.range_work), work_u128(&total), rs.out_leaves);
}

// `snark-wrap <range.bin> <out.snark>`: Groth16-compress an EXISTING folded range receipt.
//
// Note this wraps a receipt that already exists rather than re-proving: risc0's `compress` takes the
// composite/succinct receipt and produces a Groth16 one committing the SAME journal. The journal is
// what every assertion below reads, so wrapping cannot weaken those checks — but it also cannot
// strengthen them, which is why `verify-snark` re-applies the full genesis pin rather than trusting
// that a small receipt must have come from a good one.
fn snark_wrap_cmd(bin: &str, out: &str) {
    use std::time::Instant;
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(bin).expect("bin")).unwrap();
    // Verify BEFORE wrapping. Compressing an invalid receipt would either fail deep inside the prover
    // or, worse, produce a small artifact nobody re-checks — so establish validity here first.
    verify_receipt(&r);
    let rs: RangeState = r.journal.decode().expect("receipt journal is not a RangeState");
    assert!(rs.kind == KIND_RANGE, "receipt is not a RangeState (domain tag)"); // H8
    assert!(rs.self_id == METHOD_ID, "self_id != METHOD_ID");
    let before = bincode::serialize(&r).map(|v| v.len()).unwrap_or(0);
    println!("=== SNARK-wrapping range [{}..{}] (STARK -> Groth16) ===", rs.lo, rs.hi);
    println!("  input receipt: {} bytes", before);
    let t = Instant::now();
    let snark = default_prover()
        .compress(&ProverOpts::groth16(), &r)
        .expect("groth16 compress");
    let secs = t.elapsed().as_secs_f64();
    // The wrapped receipt must verify against the same guest id, and must carry the same journal —
    // a wrap that silently changed either would be a forgery vector, not an optimisation.
    snark.verify(METHOD_ID).expect("wrapped receipt failed to verify");
    let rs2: RangeState = snark.journal.decode().expect("wrapped journal is not a RangeState");
    assert!(rs2.lo == rs.lo && rs2.hi == rs.hi && rs2.out_tip_hash == rs.out_tip_hash
            && rs2.range_work == rs.range_work && rs2.self_id == rs.self_id,
            "wrapped journal differs from the original — refusing to write it");
    let bytes = bincode::serialize(&snark).unwrap();
    std::fs::write(out, &bytes).unwrap();
    println!("  wrapped in {:.1}s -> {} ({} bytes, {:.0}x smaller)",
        secs, out, bytes.len(), before as f64 / bytes.len().max(1) as f64);
    println!("  verify with: host verify-snark {out}");
}

// `verify-snark <out.snark>`: verify a Groth16-wrapped range proof and PIN it to genesis.
//
// This deliberately repeats EVERY assertion `verify-range` makes. A wrap is only worth having if it
// is exactly as strong as what it replaces; a verifier that skipped the genesis pin because the
// receipt "came from" a folded proof would accept a fabricated-anchor range in a smaller package.
fn verify_snark_cmd(bin: &str) {
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(bin).expect("bin")).unwrap();
    let claimed = r.journal.decode::<RangeState>().ok().map(|rs| rs.self_id);
    verify_receipt_ex(&r, claimed);
    let rs: RangeState = r.journal.decode().unwrap();
    assert!(rs.self_id == METHOD_ID, "self_id != METHOD_ID");
    assert!(rs.kind == KIND_RANGE, "receipt is not a RangeState (domain tag)"); // H8
    assert_eq!(rs.lo, 1, "range must start at block 1 (genesis-anchored)");
    assert_genesis_in_boundary(&rs);
    let mut total = arr_u128(GENESIS_WORK);
    add256_host(&mut total, &rs.range_work);
    let sz = std::fs::metadata(bin).map(|m| m.len()).unwrap_or(0);
    println!(">>> SNARK RANGE PROOF [1..{}] VERIFIED — genesis-anchored, {} bytes.", rs.hi, sz);
    println!("  out_tip_hash {}  range_work {}  total_cum_work {}  UTXO leaves {}",
        hex(&rs.out_tip_hash), work_u128(&rs.range_work), work_u128(&total), rs.out_leaves);
}

// Verify a range receipt WITHOUT the genesis assertion — the CPU check a coordinator runs on each
// submitted contribution. Confirms the STARK is valid and reports the committed [lo,hi] + boundary
// tips, so the coordinator can chain ranges (out_tip of k == in_tip of k+1) into a genesis-anchored
// frontier without doing any proving/folding itself.
fn verify_any_cmd(bin: &str) {
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(bin).expect("bin")).unwrap();
    // The journal (public output) decodes without verification — read the proof's own committed guest id
    // first, so a verify failure can be classified as a build MISMATCH vs a genuinely INVALID proof.
    let claimed = r.journal.decode::<RangeState>().ok().map(|rs| rs.self_id);
    verify_receipt_ex(&r, claimed); // real STARK verification (distinguishes mismatch from forgery)
    let rs: RangeState = r.journal.decode().unwrap();
    assert!(rs.self_id == METHOD_ID, "self_id != METHOD_ID");
    assert!(rs.kind == KIND_RANGE, "receipt is not a RangeState (domain tag)"); // H8
    // If this range CLAIMS to connect to genesis, its full genesis in-boundary must be pinned — else a
    // prover fabricates the initial UTXO set / difficulty and the coordinator chains it into the frontier.
    if rs.in_tip_hash == arr(rev(hx(GENESIS_HASH))) {
        assert_genesis_in_boundary(&rs);
    }
    // Whether this receipt proves anything about the chain FROM GENESIS, as opposed to proving a
    // correct transition between two boundaries it states itself.
    //
    // Both conditions are required. `in_tip_hash == genesis` alone is what triggers the pin above,
    // but a receipt could carry the genesis in-tip while claiming lo != 1; the standalone verifier
    // (verifier/src/lib.rs) requires lo == 1 as one of its five assertions, so this agrees with it
    // rather than inventing a second, weaker notion of "anchored".
    let anchored = rs.in_tip_hash == arr(rev(hx(GENESIS_HASH))) && rs.lo == 1;
    // Expose FULL-boundary digests so the coordinator chains on `out_bhash(k) == in_bhash(k+1)` — the
    // complete seam check the guest fold does (tip + UTXO roots + leaves + difficulty + MTP window), not
    // just tip-hash. Without this a mid-chain range can fabricate its in-boundary UTXO set / difficulty.
    // in-boundary = the chain state after block lo-1; out-boundary = after block hi (H9 height binding).
    let in_bh = boundary_digest(rs.lo.saturating_sub(1), &rs.in_tip_hash, &rs.in_roots, rs.in_leaves, rs.in_nbits, rs.in_time, rs.in_epoch_start, &rs.in_recent);
    let out_bh = boundary_digest(rs.hi, &rs.out_tip_hash, &rs.out_roots, rs.out_leaves, rs.out_nbits, rs.out_time, rs.out_epoch_start, &rs.out_recent);
    // `anchored` is APPENDED, never inserted: the coordinator parses this line as
    // `dict(t.split("=",1) for t in line[len("RANGE-OK"):].split() if "=" in t)`, so a new key=value
    // token is picked up by older coordinators as an extra key and by newer ones as the flag. Moving
    // or renaming an existing token would not be safe; adding one is.
    println!("RANGE-OK lo={} hi={} in_tip={} out_tip={} out_leaves={} range_work={} in_bhash={} out_bhash={} anchored={}",
        rs.lo, rs.hi, hex(&rs.in_tip_hash), hex(&rs.out_tip_hash), rs.out_leaves, work_u128(&rs.range_work),
        hex(&in_bh), hex(&out_bh), if anchored { "yes" } else { "no" });
    // Say it in prose too. `RANGE-OK` reads as unqualified success, and for a mid-chain receipt that
    // is a stronger claim than the proof supports: it attests a correct transition between the
    // boundaries it states, NOT that those boundaries descend from the real genesis. A reader who
    // treats it as "this proves the chain from genesis" is wrong, and the exit code alone (0 here,
    // because the SNARK is valid) does not tell them so. Raised by external review 2026-08-01 (L-1).
    if !anchored {
        println!("NOTE: this range is NOT genesis-anchored — it proves a correct transition between");
        println!("      its own stated boundaries, not that they descend from the real genesis.");
        println!("      Anchoring is established by the connected chain (the board's frontier) or by");
        println!("      `verify-range` / `verify-chain`, which pin genesis. Use those to conclude more.");
    }
}

// `verify-chain <bin>`: verify a bare ChainState (mode-2/mode-5) receipt and PIN its committed anchor to
// the genesis anchor (S5). This is the chain-track analogue of verify-range's genesis in-boundary pin:
// without it an is_base=1 receipt built on a FABRICATED anchor (arbitrary height/UTXO/work/easy nbits) is
// journal-indistinguishable from a genuine genesis-anchored one. The expected anchor_id is the double-
// SHA256 of the canonical genesis-anchor journal — exactly what the guest commits in the base step.
fn verify_chain_cmd(bin: &str) {
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(bin).expect("bin")).unwrap();
    let claimed = r.journal.decode::<ChainState>().ok().map(|cs| cs.self_id);
    verify_receipt_ex(&r, claimed); // real STARK verification (distinguishes build-mismatch from forgery)
    let cs: ChainState = r.journal.decode().unwrap();
    assert!(cs.self_id == METHOD_ID, "self_id != METHOD_ID");
    assert!(cs.kind == KIND_CHAIN, "receipt is not a ChainState (domain tag)"); // H8
    let expected_anchor = bitcoin::hashes::sha256d::Hash::hash(&state_journal_bytes(&genesis_anchor())).to_byte_array();
    assert_eq!(cs.anchor_id, expected_anchor, "chain NOT anchored at genesis (S5): anchor_id mismatch");
    println!(">>> CHAIN PROOF [genesis..{}] VERIFIED — genesis-anchored, self-authenticating.", cs.height);
    println!("  tip_hash {}  cum_work {}  UTXO leaves {}", hex(&cs.tip_hash), work_u128(&cs.cum_work), cs.utxo_leaves);
}

// SEGMENTED proof: split the block's inputs into chunks, prove each chunk's scripts (mode 4), then
// aggregate (mode 5) — env::verify the chunks + do the cheap accumulator transition + block checks.
fn prove_seg() {
    use std::time::Instant;
    let (anchor, w) = build_full();
    let n = w.inputs.len();
    let nchunks: usize = std::env::var("HAZYNC_CHUNKS").ok().and_then(|s| s.parse().ok()).unwrap_or(2).max(1).min(n.max(1));
    let sz = n.div_ceil(nchunks);
    // ADVERSARIAL #2 (test-only, inert unless HAZYNC_H2_BADHEIGHT set): prove the chunk at height 1
    // (script flags 0) while aggregating into the real modern block. The chunk's committed binding
    // digest folds in flags(1)=0, but the aggregation recomputes it with the block's real flags, so the
    // digests differ and the aggregate MUST reject. Pre-fix (chunk committed bare coin leaves) this was
    // ACCEPTED — the segmented-path flag/witness hole. NEVER set in production.
    let h2_bad = std::env::var("HAZYNC_H2_BADHEIGHT").is_ok();
    let chunk_height = if h2_bad { 1 } else { w.height };
    println!("=== SEGMENTED PROOF block {}: {} inputs → {} chunks → aggregate (on GPU) ===", w.height, n, nchunks);
    if h2_bad { println!("  [H2-TEST] proving chunks at height {} (flags 0) — aggregate must REJECT", chunk_height); }
    let t = Instant::now();

    let mut chunk_receipts: Vec<risc0_zkvm::Receipt> = Vec::new();
    for c in 0..nchunks {
        let lo = c * sz;
        let hi = ((c + 1) * sz).min(n);
        if lo >= hi { break; }
        let mut b = ExecutorEnv::builder();
        b.segment_limit_po2(seg_po2());
        b.write(&4u32).unwrap();
        b.write(&chunk_height).unwrap();
        b.write(&header_hash(&w.header)).unwrap(); // block hash for flag exceptions
        b.write(&((hi - lo) as u32)).unwrap();
        for inp in &w.inputs[lo..hi] {
            b.write(&ChunkInput {
                raw_tx: w.txs[inp.tx_idx as usize].0.clone(), input_idx: inp.input_idx, prevouts: w.tx_prevouts[inp.tx_idx as usize].0.clone(),
                coin_height: inp.coin_height, coin_is_coinbase: inp.coin_is_coinbase, coin_mtp: inp.coin_mtp,
            }).unwrap();
        }
        let receipt = default_prover()  // succinct: lift now, cheap aggregate later (see prove_chunk note)
            .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct()).unwrap().receipt;
        receipt.verify(METHOD_ID).unwrap();
        println!("  chunk {} ({} inputs) proved ({:.0}s cum)", c, hi - lo, t.elapsed().as_secs_f64());
        chunk_receipts.push(receipt);
    }

    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    for r in &chunk_receipts { b.add_assumption(r.clone()); }
    b.write(&5u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    b.write(&(chunk_receipts.len() as u32)).unwrap();
    for r in &chunk_receipts { b.write(&r.journal.bytes).unwrap(); }
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap();
    let agg_res = default_prover().prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct());
    if h2_bad {
        match agg_res {
            Ok(_) => { println!(">>> ADVERSARIAL #2: FAIL — chunk at wrong height ACCEPTED (soundness hole!)"); std::process::exit(1); }
            Err(e) => { println!(">>> ADVERSARIAL #2: wrong-height chunk REJECTED ✓  ({})", format!("{e}").lines().next().unwrap_or("")); return; }
        }
    }
    let agg = agg_res.unwrap().receipt;
    agg.verify(METHOD_ID).unwrap();
    let tip: ChainState = agg.journal.decode().unwrap();
    assert!(tip.self_id == METHOD_ID, "S1: proof recursed against wrong image id");
    println!(">>> SEGMENTED BLOCK {} PROVED in {:.1}s — succinct receipt VERIFIED (chunks map, aggregate reduces).", w.height, t.elapsed().as_secs_f64());
    println!("  tip_hash {}  cum_work {}  UTXO leaves {}", hex(&tip.tip_hash), work_u128(&tip.cum_work), tip.utxo_leaves);
}

// ---- Multi-GPU fan-out: prove ONE chunk to a file (run one process per GPU via CUDA_VISIBLE_DEVICES),
// then aggregate from the chunk-receipt files. HAZYNC_CHUNKS = total chunks; chunk index from arg. ----
fn chunk_range(n: usize, nchunks: usize, idx: usize) -> (usize, usize) {
    let sz = n.div_ceil(nchunks);
    ((idx * sz).min(n), ((idx + 1) * sz).min(n))
}
fn nchunks_env() -> usize {
    std::env::var("HAZYNC_CHUNKS").ok().and_then(|s| s.parse().ok()).unwrap_or(2).max(1)
}

// `prove-chunk <i>`: prove chunk i's scripts, write the receipt to chunk_<i>.bin (or $HAZYNC_OUT).
fn prove_chunk(idx: usize) {
    use std::time::Instant;
    let (_anchor, w) = build_full();
    let n = w.inputs.len();
    let nchunks = nchunks_env().min(n.max(1));
    let (lo, hi) = chunk_range(n, nchunks, idx);
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap(); // block hash for flag exceptions
    b.write(&((hi - lo) as u32)).unwrap();
    for inp in &w.inputs[lo..hi] {
        b.write(&ChunkInput {
            raw_tx: w.txs[inp.tx_idx as usize].0.clone(), input_idx: inp.input_idx, prevouts: w.tx_prevouts[inp.tx_idx as usize].0.clone(),
            coin_height: inp.coin_height, coin_is_coinbase: inp.coin_is_coinbase, coin_mtp: inp.coin_mtp,
        }).unwrap();
    }
    let t = Instant::now();
    // SCALING: prove the chunk to a SUCCINCT receipt (not the default composite). This runs the
    // STARK-to-STARK "lift" NOW, in parallel across the chunk fleet — so agg-chunks resolves each
    // assumption cheaply instead of lifting all N composite receipts sequentially (the dominant cost
    // of the 741000 aggregate: ~1645s → expected to collapse to a cheap fold). See HAZYNC_ARCHITECTURE.md.
    let receipt = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct()).unwrap().receipt;
    receipt.verify(METHOD_ID).unwrap();
    let out = std::env::var("HAZYNC_OUT").unwrap_or_else(|_| format!("chunk_{idx}.bin"));
    std::fs::write(&out, bincode::serialize(&receipt).unwrap()).unwrap();
    println!("chunk {idx} ({} inputs) proved in {:.0}s -> {out}", hi - lo, t.elapsed().as_secs_f64());
}

// `agg-chunks`: read all chunk receipt files, aggregate into the block/chain proof.
fn agg_chunks() {
    use std::time::Instant;
    let (anchor, w) = build_full();
    let nchunks = nchunks_env().min(w.inputs.len().max(1));
    let mut receipts: Vec<risc0_zkvm::Receipt> = Vec::new();
    for i in 0..nchunks {
        let f = format!("chunk_{i}.bin");
        let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(&f).expect("chunk receipt file")).unwrap();
        r.verify(METHOD_ID).expect("chunk receipt verify");
        receipts.push(r);
    }
    println!("=== AGGREGATING {} chunk receipts for block {} ===", receipts.len(), w.height);
    let t = Instant::now();
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    for r in &receipts { b.add_assumption(r.clone()); }
    b.write(&5u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    b.write(&(receipts.len() as u32)).unwrap();
    for r in &receipts { b.write(&r.journal.bytes).unwrap(); }
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap();
    // Prove the aggregate to SUCCINCT too: the assumptions are already succinct (cheap resolve), and a
    // succinct block proof is a single fixed-size STARK — directly composable in the chain range-fold.
    let agg = default_prover()
        .prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct()).unwrap().receipt;
    agg.verify(METHOD_ID).unwrap();
    let tip: ChainState = agg.journal.decode().unwrap();
    assert!(tip.self_id == METHOD_ID, "S1: proof recursed against wrong image id");
    let out = std::env::var("HAZYNC_AGG_OUT").unwrap_or_else(|_| format!("block_{}.receipt", w.height));
    std::fs::write(&out, bincode::serialize(&agg).unwrap()).ok();
    println!(">>> BLOCK {} AGGREGATED in {:.1}s — succinct receipt VERIFIED, saved {out}.", w.height, t.elapsed().as_secs_f64());
    println!("  tip_hash {}  cum_work {}  UTXO leaves {}", hex(&tip.tip_hash), work_u128(&tip.cum_work), tip.utxo_leaves);
}

// ADVERSARIAL S1: prove a valid base (170), then attempt to fold 171 with a WRONG self_id.
// The guest's `assert(prev.self_id == self_id)` (and the unresolvable composition) must reject it.
fn prove_chain_bad() {
    let (mut forest, anchor) = seed_and_anchor();
    let mut recent = anchor.recent_times.clone();
    let w170 = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&recent));
    let r170 = prove_step(state_journal_bytes(&anchor), None, &w170, 1);
    advance_recent(&mut recent, 1_231_731_025);
    let cb171: Transaction = deserialize(&hx(CB171)).unwrap();
    let hdr171 = build_header(HASH170, &cb171.compute_txid().to_byte_array(), 1_231_731_401, 0x1d00ffff, 653_436_935);
    let w171 = build_block(&mut forest, hdr171, 171, CB171, &[], median_u32(&recent));
    let mut bad_id = METHOD_ID;
    bad_id[0] ^= 1; // corrupt the image id
    println!("=== ADVERSARIAL S1: folding block 171 with a WRONG self_id (must be rejected) ===");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.add_assumption(r170.clone());
    b.write(&2u32).unwrap();
    b.write(&r170.journal.bytes).unwrap();
    b.write(&w171).unwrap();
    b.write(&0u32).unwrap();
    b.write(&bad_id).unwrap(); // WRONG
    match default_prover().prove(b.build().unwrap(), METHOD_ELF) {
        Ok(_) => { println!(">>> ADVERSARIAL S1: FAIL — wrong self_id ACCEPTED (soundness hole!)"); std::process::exit(1); }
        Err(e) => println!(">>> ADVERSARIAL S1: wrong self_id REJECTED ✓  ({})", format!("{e}").lines().next().unwrap_or("")),
    }
}

// C1: fast, self-contained, EXECUTE-mode regression — no proving, no GPU, no external files. Runs
// block 170 through the whole consensus path (scripts, checks, accumulator, PoW, merkle, subsidy,
// BIP34/30, witness, self_id) and asserts the committed tip. Any consensus-logic regression trips
// either a chain_step assertion (execute → Err) or the tip mismatch.
fn regress() {
    let (mut forest, anchor) = seed_and_anchor();
    let w = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&anchor.recent_times));
    let mut b = ExecutorEnv::builder();
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    let s = default_executor().execute(b.build().unwrap(), METHOD_ELF).expect("regress: block 170 execute failed");
    let tip: ChainState = s.journal.decode().unwrap();
    let ok = tip.tip_hash == arr(rev(hx(HASH170))) && tip.height == 170 && tip.self_id == METHOD_ID;
    println!("[regress] block 170 chain_step (execute): tip {} height {} self_id-ok {}",
        if tip.tip_hash == arr(rev(hx(HASH170))) { "MATCH" } else { "MISMATCH" }, tip.height, tip.self_id == METHOD_ID);
    println!(">>> REGRESSION {}", if ok { "PASS ✓" } else { "FAIL ✗" });
    if !ok { std::process::exit(1); }
}

// ============================================================================================
// ADVERSARIAL SOUNDNESS SUITE — execute-mode, self-contained, no GPU, no external files. Each case
// builds a witness that exploits a specific hole from the 2026-07 soundness audit and asserts the
// guest REJECTS it, alongside an honest baseline that must be ACCEPTED (so a broken baseline can't
// make a malicious case look "rejected" for the wrong reason). Run with `host adversarial`; wired
// into CI. Holes: #1 host-controlled height, #3 in-block double-spend / ordering, #4 coinbase checks.
// (#2 segmented flag/witness binding needs proven chunks -> GPU box, see `prove-chunk-badheight`.)
// ============================================================================================

// Decode of the guest's mode-1 BlockOutput journal (field order MUST match the guest's BlockOutput).
#[derive(Deserialize)]
struct BlockOut {
    _script_results: Vec<i32>, _tx_checks: Vec<i32>, _coin_leaves: Vec<[u8; 32]>, _total_fee: i64,
    _pow_ok: bool, _merkle_ok: bool, _coinbase_val: i64, _subsidy: i64, _subsidy_ok: bool,
    all_ok: bool, _root_matches: bool,
}

// Execute one block witness in mode-1 (block_proof) and return the committed `all_ok` — which is
// independent of PoW/merkle, so a synthetic block with a dummy header still exercises the accumulator,
// script, coinbase and in-block-spend logic. A guest panic surfaces as Err => treated as rejected.
fn block_all_ok(w: &BlockWitness) -> bool {
    let mut b = ExecutorEnv::builder();
    b.write(&1u32).unwrap();
    b.write(w).unwrap();
    match default_executor().execute(b.build().unwrap(), METHOD_ELF) {
        Ok(s) => { let o: BlockOut = s.journal.decode().unwrap(); o.all_ok }
        Err(_) => false,
    }
}

// Build a synthetic OP_TRUE block: a coinbase, then `txs` (each a single-input tx) where inblock[i]
// marks txs[i] as spending an in-block-created coin (tx A's output 0) rather than the external coin C.
// Height 1000 => script flags 0, so bare OP_TRUE spends validate. Values: C=50.00001 BTC funds A
// (out 50 BTC), B/D spend A:0 (out 49.99999 / 49.99998), coinbase = 50 BTC + 2000 sat fees.
const SYNTH_H: u32 = 1000;
const SYNTH_T: u32 = 1_400_000_000;
fn synth_block(cb: &Transaction, txs: &[&Transaction], inblock: &[bool]) -> BlockWitness {
    let optrue = || ScriptBuf::from_bytes(vec![0x51]);
    let c_leaf = coin_leaf(&[0x11u8; 32], 0, 5_000_001_000, &[0x51], 1, false, 0);
    let mut forest = Forest::new();
    for i in 0..4u64 { forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat())); }
    forest.add(c_leaf);
    for i in 0..2u64 { forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat())); }
    let root_prev = wire_stump(&forest);

    let cb_txid = cb.compute_txid().to_byte_array();
    let mut txids = vec![cb_txid];
    let mut wtxids: Vec<[u8; 32]> = vec![[0u8; 32]]; // coinbase wtxid = zeros (BIP141)
    let mut inputs: Vec<BlockInput> = Vec::new();
    let mut wtxs: Vec<PackedBytes> = Vec::new();
    let mut wtx_prevs: Vec<PackedBytes> = Vec::new();
    for (i, tx) in txs.iter().enumerate() {
        txids.push(tx.compute_txid().to_byte_array());
        wtxids.push(tx.compute_wtxid().to_byte_array());
        if inblock[i] {
            // spends A:0 (in-block coin, created THIS block at height H, mtp = block_time). No
            // accumulator proof needed — the guest skips the delete for in-block coins.
            let prevouts = serialize(&vec![TxOut { value: Amount::from_sat(5_000_000_000), script_pubkey: optrue() }]);
            wtxs.push(PackedBytes(serialize(*tx))); wtx_prevs.push(PackedBytes(prevouts));
            inputs.push(BlockInput {
                tx_idx: i as u32, input_idx: 0,
                global_pos: 0, coin_height: SYNTH_H, coin_is_coinbase: 0, coin_mtp: SYNTH_T, tx_first: 1,
                proof_i: WireProof { leaf: [0u8; 32], position: 0, siblings: vec![] },
                proof_last: WireProof { leaf: [0u8; 32], position: 0, siblings: vec![] },
            });
        } else {
            // external: spends C from the accumulator (real inclusion proof).
            let prevouts = serialize(&vec![TxOut { value: Amount::from_sat(5_000_001_000), script_pubkey: optrue() }]);
            let pos = forest.find(&c_leaf).expect("C in accumulator");
            let last = forest.leaves.len() - 1;
            wtxs.push(PackedBytes(serialize(*tx))); wtx_prevs.push(PackedBytes(prevouts));
            inputs.push(BlockInput {
                tx_idx: i as u32, input_idx: 0,
                global_pos: pos as u64, coin_height: 1, coin_is_coinbase: 0, coin_mtp: 0, tx_first: 1,
                proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)),
            });
            forest.delete(pos);
        }
    }
    let root_next = wire_stump(&forest); // approximate — root_matches is not part of all_ok
    let header = build_header_v(1, HASH169, &[0u8; 32], SYNTH_T, 0x1d00ffff, 0);
    let (in_smt_root, smt) = smt_witness_standalone(cb_txid, cb_spendable_outputs(cb),
        &cb_spends_from(&inputs, &wtxs));
    BlockWitness { header, height: SYNTH_H, coinbase_tx: serialize(cb), txids, wtxids, root_prev, txs: wtxs, tx_prevouts: wtx_prevs, inputs, new_outputs: vec![], root_next, bip30: None, in_smt_root, smt }
}

// The four OP_TRUE transactions (built via the real bitcoin crate so txids/serialization are correct).
fn synth_txs() -> (Transaction, Transaction, Transaction, Transaction, Transaction) {
    let optrue = || ScriptBuf::from_bytes(vec![0x51]);
    let txout = |sat: u64| TxOut { value: Amount::from_sat(sat), script_pubkey: optrue() };
    let vin = |txid: Txid, vout: u32, ss: Vec<u8>| TxIn {
        previous_output: OutPoint { txid, vout }, script_sig: ScriptBuf::from_bytes(ss),
        sequence: Sequence::MAX, witness: Witness::new(),
    };
    let mktx = |input: Vec<TxIn>, output: Vec<TxOut>| Transaction {
        version: transaction::Version(1), lock_time: absolute::LockTime::ZERO, input, output,
    };
    let c_txid = Txid::from_byte_array([0x11u8; 32]);
    let a = mktx(vec![vin(c_txid, 0, vec![])], vec![txout(5_000_000_000)]);
    let a_txid = a.compute_txid();
    let b = mktx(vec![vin(a_txid, 0, vec![])], vec![txout(4_999_999_000)]);
    let d = mktx(vec![vin(a_txid, 0, vec![])], vec![txout(4_999_998_000)]);
    let cb_ok = mktx(vec![vin(Txid::all_zeros(), 0xffff_ffff, vec![0x51, 0x51])], vec![txout(5_000_002_000)]);
    let cb_bad = mktx(vec![vin(Txid::all_zeros(), 0xffff_ffff, vec![0x51; 101])], vec![txout(5_000_002_000)]);
    (cb_ok, cb_bad, a, b, d)
}

// #5: a 2-input tx whose FIRST input's prevouts blob carries a phantom high-value coin (the fee blob
// the host supplies is not, entry-for-entry, bound to accumulator-authenticated coins). `phantom=false`
// is the honest baseline (both inputs share the real [C1,C2] blob); `phantom=true` puts a ~21M BTC
// fake coin at position 1 of the first input's blob to inflate the fee -> mint via the coinbase. The
// #5 pre-pass must reject it (the two inputs' blobs differ => group check fails => all_ok=false).
fn synth_unbound_prevouts(phantom: bool) -> BlockWitness {
    let optrue = || ScriptBuf::from_bytes(vec![0x51]);
    let (v1, v2): (u64, u64) = (3_000_000_000, 2_000_000_000); // C1 + C2 = 50 BTC
    let c1_txid = Txid::from_byte_array([0x21u8; 32]);
    let c2_txid = Txid::from_byte_array([0x22u8; 32]);
    let c1_leaf = coin_leaf(&[0x21u8; 32], 0, v1, &[0x51], 1, false, 0);
    let c2_leaf = coin_leaf(&[0x22u8; 32], 0, v2, &[0x51], 1, false, 0);
    // 2-input tx spending C1 and C2, one OP_TRUE output (fee 1000 sat).
    let vin = |txid: Txid| TxIn { previous_output: OutPoint { txid, vout: 0 },
        script_sig: ScriptBuf::new(), sequence: Sequence::MAX, witness: Witness::new() };
    let t = Transaction { version: transaction::Version(1), lock_time: absolute::LockTime::ZERO,
        input: vec![vin(c1_txid), vin(c2_txid)],
        output: vec![TxOut { value: Amount::from_sat(v1 + v2 - 1000), script_pubkey: optrue() }] };
    let t_raw = serialize(&t);
    // coinbase: subsidy(1000)=50 BTC + 1000 sat fee.
    let cb = Transaction { version: transaction::Version(1), lock_time: absolute::LockTime::ZERO,
        input: vec![TxIn { previous_output: OutPoint { txid: Txid::all_zeros(), vout: 0xffff_ffff },
            script_sig: ScriptBuf::from_bytes(vec![0x51, 0x51]), sequence: Sequence::MAX, witness: Witness::new() }],
        output: vec![TxOut { value: Amount::from_sat(5_000_001_000), script_pubkey: optrue() }] };

    let real_blob = serialize(&vec![
        TxOut { value: Amount::from_sat(v1), script_pubkey: optrue() },
        TxOut { value: Amount::from_sat(v2), script_pubkey: optrue() }]);
    let phantom_blob = serialize(&vec![
        TxOut { value: Amount::from_sat(v1), script_pubkey: optrue() },
        TxOut { value: Amount::from_sat(2_100_000_000_000_000), script_pubkey: optrue() }]); // ~21M BTC fake

    let mut forest = Forest::new();
    for i in 0..4u64 { forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat())); }
    forest.add(c1_leaf); forest.add(c2_leaf);
    for i in 0..2u64 { forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat())); }
    let root_prev = wire_stump(&forest);

    let mk_input = |forest: &mut Forest, idx: u32, leaf: Hash| -> BlockInput {
        let pos = forest.find(&leaf).expect("coin in accumulator");
        let last = forest.leaves.len() - 1;
        let bi = BlockInput { tx_idx: 0, input_idx: idx,
            global_pos: pos as u64, coin_height: 1, coin_is_coinbase: 0, coin_mtp: 0, tx_first: (idx == 0) as u32,
            proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)) };
        forest.delete(pos); bi
    };
    // Both inputs share the tx's SINGLE prevouts blob (dedup). The phantom-fee attack now lives in that
    // shared blob: prevouts[1] is a fake ~21M-BTC coin instead of the real C2 — so input 1's guest-computed
    // coin leaf no longer matches the accumulator-proven C2, and the block is rejected (accumulator binding,
    // the same soundness the per-input blob-equality check used to enforce).
    let in0 = mk_input(&mut forest, 0, c1_leaf);
    let in1 = mk_input(&mut forest, 1, c2_leaf);
    let shared_blob = if phantom { phantom_blob } else { real_blob.clone() };

    let header = build_header_v(1, HASH169, &[0u8; 32], SYNTH_T, 0x1d00ffff, 0);
    // Both inputs here are non-coinbase, so the transition is just the coinbase insert.
    let unbound_smt = smt_witness_standalone(cb.compute_txid().to_byte_array(),
        cb_spendable_outputs(&cb), &[]);
    BlockWitness { header, height: SYNTH_H, coinbase_tx: serialize(&cb),
        txids: vec![cb.compute_txid().to_byte_array(), t.compute_txid().to_byte_array()],
        wtxids: vec![[0u8; 32], t.compute_wtxid().to_byte_array()],
        root_prev, txs: vec![PackedBytes(t_raw.clone())], tx_prevouts: vec![PackedBytes(shared_blob)],
        inputs: vec![in0, in1], new_outputs: vec![], root_next: wire_stump(&forest), bip30: None,
        in_smt_root: unbound_smt.0, smt: unbound_smt.1 }
}

// #1: on the real block-170 chain step, downgrade the host-supplied height. The guest must reject
// (its `w.height == prev.height+1` assert fires => execute Err).
fn adv_height_rejected() -> bool {
    let (mut forest, anchor) = seed_and_anchor();
    let mut w = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&anchor.recent_times));
    w.height = 1; // attacker: height 1 turns every soft-fork flag off + subsidy -> 50 BTC
    let mut b = ExecutorEnv::builder();
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap(); // is_base = 1
    b.write(&METHOD_ID).unwrap();
    default_executor().execute(b.build().unwrap(), METHOD_ELF).is_err()
}

fn adversarial() {
    println!("=== HAZYNC ADVERSARIAL SOUNDNESS SUITE (execute-mode) — every malicious witness must be REJECTED ===\n");
    let verdict = |rejected: bool| if rejected { "REJECTED ✓" } else { "ACCEPTED ✗ (SOUNDNESS HOLE)" };
    let mut pass = true;
    let (cb_ok, cb_bad, a, b, d) = synth_txs();

    // Baseline: an honest in-block spend (B spends A's output created in the same block) must be ACCEPTED.
    let honest = block_all_ok(&synth_block(&cb_ok, &[&a, &b], &[false, true]));
    println!("[baseline] honest in-block-spend block accepted ......... {}", if honest { "yes ✓" } else { "NO ✗ (baseline broken — fix before trusting the rejects)" });
    pass &= honest;

    let r1 = adv_height_rejected();
    println!("#1 host-controlled height (flag/subsidy downgrade) ...... {}", verdict(r1));
    pass &= r1;

    // #3a: B and D both spend A:0 in the same block (double-spend -> inflation).
    let r3a = !block_all_ok(&synth_block(&cb_ok, &[&a, &b, &d], &[false, true, true]));
    println!("#3 in-block coin spent twice (inflation) ................ {}", verdict(r3a));
    pass &= r3a;

    // #3b: B (spending A:0) placed BEFORE A creates it (spend-before-create / ordering).
    let r3b = !block_all_ok(&synth_block(&cb_ok, &[&b, &a], &[true, false]));
    println!("#3 spend-before-create ordering violation ............... {}", verdict(r3b));
    pass &= r3b;

    // #4: coinbase with a 101-byte scriptSig (bad-cb-length) now runs through CheckTransaction.
    let r4 = !block_all_ok(&synth_block(&cb_bad, &[&a, &b], &[false, true]));
    println!("#4 malformed coinbase (never CheckTransaction'd before) . {}", verdict(r4));
    pass &= r4;

    // #5: unbound fee-prevouts on a 2-input tx. Baseline (honest shared blob) must pass; the phantom
    // ~21M BTC coin in the first input's blob must be rejected.
    let honest2 = block_all_ok(&synth_unbound_prevouts(false));
    println!("[baseline] honest 2-input tx accepted ................... {}", if honest2 { "yes ✓" } else { "NO ✗ (baseline broken)" });
    pass &= honest2;
    let r5 = !block_all_ok(&synth_unbound_prevouts(true));
    println!("#5 unbound fee-prevouts (phantom coin -> inflation) ..... {}", verdict(r5));
    pass &= r5;

    println!("\n>>> ADVERSARIAL SUITE {}", if pass { "PASS ✓ — all holes closed" } else { "FAIL ✗ — a hole is OPEN" });
    if !pass { std::process::exit(1); }
}

// Execute one witness in mode-1 and return (all_ok, root_matches).
fn block_out(w: &BlockWitness) -> (bool, bool) {
    let mut b = ExecutorEnv::builder();
    b.write(&1u32).unwrap();
    b.write(w).unwrap();
    match default_executor().execute(b.build().unwrap(), METHOD_ELF) {
        Ok(s) => { let o: BlockOut = s.journal.decode().unwrap(); (o.all_ok, o._root_matches) }
        Err(_) => (false, false),
    }
}

// F3: the BIP30 grandfathered overwrite, tested on REAL block 91842 (coinbase-only, whose coinbase
// duplicates block 91812's still-unspent coinbase outpoint). The honest overwrite must ACCEPT with a
// matching root (superseded leaf deleted, new one added); skipping it, or claiming the wrong old height,
// must REJECT. Needs block_91842.json (fetch_block.py 91842).
fn check_bip30() {
    let (height, time, bits, nonce): (u32, u32, u32, u32) = (91842, 1_289_768_691, 453_931_606, 3_778_549_762);
    let header = build_header_v(1, PREV91842, &arr(rev(hx(MERKLE91842))), time, bits, nonce);
    let cb_hex = CB91842;
    let coinbase: Transaction = deserialize(&hx(cb_hex)).unwrap();
    let cb_txid = coinbase.compute_txid().to_byte_array();
    let old_height: u32 = 91812;          // block 91842 duplicates 91812's coinbase
    let (old_mtp, new_mtp) = (time, time); // test: seed and witness use the same value (a real run uses MTP(h-1))

    // root_prev = fillers + the SUPERSEDED coinbase outputs (this coinbase at old_height/old_mtp).
    let mut forest = Forest::new();
    for i in 0..4u64 { forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat())); }
    let mut superseded: Vec<Hash> = Vec::new();
    for v in 0..coinbase.output.len() {
        if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
        let l = out_leaf_of(&coinbase, &cb_txid, v, old_height, true, old_mtp);
        forest.add(l); superseded.push(l);
    }
    for i in 0..2u64 { forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat())); }
    let root_prev = wire_stump(&forest);

    // overwrite: delete the superseded leaves (against root_prev), then add the NEW coinbase outputs.
    let mut dels: Vec<Bip30Del> = Vec::new();
    for l in &superseded {
        let pos = forest.find(l).expect("superseded coin present");
        let last = forest.leaves.len() - 1;
        dels.push(Bip30Del { global_pos: pos as u64, proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)) });
        forest.delete(pos);
    }
    for v in 0..coinbase.output.len() {
        if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
        forest.add(out_leaf_of(&coinbase, &cb_txid, v, height, true, new_mtp));
    }
    let root_next = wire_stump(&forest);

    // F3 is about the ACCUMULATOR overwrite, not the BIP30 check — under the SMT these two blocks are
    // ordinary inserts once the earlier coinbase is spent, which `a_fully_spent_coinbase_can_be_
    // duplicated_with_no_special_case` covers directly. A standalone transition keeps this test on its
    // own subject.
    let f3_smt = smt_witness_standalone(cb_txid, cb_spendable_outputs(&coinbase), &[]);
    let mk = |bip30: Option<Bip30Overwrite>| BlockWitness {
        header: header.clone(), height, coinbase_tx: hx(cb_hex), txids: vec![cb_txid], wtxids: vec![[0u8; 32]],
        root_prev: root_prev.clone(), txs: vec![], tx_prevouts: vec![], inputs: vec![], new_outputs: vec![], root_next: root_next.clone(), bip30,
        in_smt_root: f3_smt.0, smt: f3_smt.1.clone(),
    };
    let honest = block_out(&mk(Some(Bip30Overwrite { old_height, old_mtp, dels: dels.clone() })));
    let skip = block_out(&mk(None));
    let wrong = block_out(&mk(Some(Bip30Overwrite { old_height: 91722, old_mtp, dels: dels.clone() }))); // wrong pair
    println!("=== F3 BIP30 grandfathered overwrite — REAL block {} (dup of 91812) ===", height);
    println!("[honest overwrite] accepted + root matches ... all_ok={} root_matches={}  (both true)", honest.0, honest.1);
    println!("[skip overwrite]   rejected (mandatory) ...... all_ok={}  (must be false)", skip.0);
    println!("[wrong old_height] rejected (delete misses) .. all_ok={}  (must be false)", wrong.0);
    let pass = honest.0 && honest.1 && !skip.0 && !wrong.0;
    println!(">>> F3 BIP30 OVERWRITE TEST {}", if pass { "PASS ✓" } else { "FAIL ✗" });
    if !pass { std::process::exit(1); }
}

// ============ ARCHIVE-NODE BRIDGE — persistent forest driver over a local bitcoind ============
// `host bridge`: drive ONE resident Forest forward over the chain from a local bitcoind, emitting per
// block a bundle {in-boundary, witness} with the REAL root_prev + inclusion proofs — so a prover can
// prove [n..n] with NO bridge_pass replay (kills the quadratic; closes S3). Reuses build_block_carried +
// push_mtp exactly; a parallel UTXO-metadata map (outpoint -> value,spk,creation-height,coinbase) supplies
// the prevouts build_block_carried needs.
/// One coinbase-output spend, with the proof of that coinbase's count under the root as it stands
/// when the spend is applied. Mirrors `hazync_coinbase_smt::bip30::Spend`.
#[derive(Serialize, Deserialize, Clone)]
struct SmtSpend { coinbase_txid: [u8; 32], current_count: u32, proof: SmtProof }

/// Everything the guest needs to run the BIP30 transition for one block.
///
/// The guest takes ONLY the proofs from here. `coinbase_txid`, `coinbase_outputs` and the spend list
/// are all things it derives for itself from data it already validates — anything it read instead of
/// derived would be something a prover could lie about, and the whole point of the structure is that
/// the check cannot be talked out of.
/// `coinbase_txid`/`coinbase_outputs` are CROSS-CHECKS, not inputs: the guest derives both for itself
/// and refuses the block if they disagree. They exist because a disagreement otherwise surfaces as an
/// unexplained `BadProof` three steps later — which is exactly what happened the first time the guest
/// counted the coinbase's outputs at the wrong point in the function.
#[derive(Serialize, Deserialize, Clone)]
struct SmtBlockWitness {
    coinbase_txid: [u8; 32],
    coinbase_outputs: u32,
    absence_proof: SmtProof,
    spends: Vec<SmtSpend>,
}

/// The coinbase txid of every input that spends a coinbase output, in the block's tx-then-input
/// order — derived the same way the guest derives it, from the same raw transaction bytes.
///
/// The guest does NOT take this list from the witness; it reads each prevout out of the transaction
/// itself. This exists so the host can build a witness whose proofs line up with what the guest will
/// independently compute, and any disagreement shows up as a refused proof rather than a bad one.
fn cb_spends_from(inputs: &[BlockInput], txs: &[PackedBytes]) -> Vec<[u8; 32]> {
    let mut out = Vec::new();
    for inp in inputs {
        if inp.coin_is_coinbase != 1 { continue; }
        let raw = txs.get(inp.tx_idx as usize)
            .unwrap_or_else(|| panic!("cb_spends_from: tx_idx {} out of range ({} txs)", inp.tx_idx, txs.len()));
        let tx: Transaction = deserialize(&raw.0)
            .unwrap_or_else(|e| panic!("cb_spends_from: tx_idx {} did not parse: {e}", inp.tx_idx));
        let i = tx.input.get(inp.input_idx as usize)
            .unwrap_or_else(|| panic!("cb_spends_from: input_idx {} out of range ({} inputs)",
                                      inp.input_idx, tx.input.len()));
        out.push(i.previous_output.txid.to_byte_array());
    }
    out
}

/// Count of a coinbase's SPENDABLE outputs — the value the guest derives from its own leaf gather.
fn cb_spendable_outputs(coinbase: &Transaction) -> u32 {
    coinbase.output.iter().filter(|o| out_spendable(o.script_pubkey.as_bytes())).count() as u32
}

/// A self-consistent SMT transition for a block considered in ISOLATION, for the fixture and
/// synthetic-block paths that have no chain history behind them.
///
/// The tree is seeded with exactly the coinbases this block spends, so the transition is valid on its
/// own terms. It is NOT the chain's real SMT state and must never be used for proving — only the
/// bridge, which replays from genesis, knows that. Its purpose is to keep the negative tests testing
/// what they are about (script flags, merkle mutation, unbound prevouts) rather than failing on an
/// unrelated BIP30 input they were never written to exercise.
fn smt_witness_standalone(
    coinbase_txid: [u8; 32],
    coinbase_outputs: u32,
    coinbase_spends: &[[u8; 32]],
) -> ([u8; 32], SmtBlockWitness) {
    let mut t = Smt::new();
    for s in coinbase_spends {
        let c = t.get(s).unwrap_or(0);
        t.insert(*s, c + 1);
    }
    let root_in = t.root();
    let w = smt_advance(&mut t, coinbase_txid, coinbase_outputs, coinbase_spends);
    (root_in, w)
}

/// Advance the coinbase SMT by one block and emit the proofs the guest will need.
///
/// PURE AND SEPARATE FROM THE BRIDGE ON PURPOSE. The order of operations here has to match
/// `bip30::apply_block` exactly — every proof is against the root as it stands at that step, not
/// against the incoming root — and a mismatch is refused by the guest rather than mis-folded, so it
/// shows up as a stalled board with no obvious cause. Extracting it means the agreement between the
/// two is a native test (`smt_emission_round_trips_through_apply_block`) instead of a comment.
///
/// `coinbase_spends` is the coinbase txid of every input in this block that spends a coinbase output,
/// in the block's own tx-then-input order.
///
/// # What the sequencing actually constrains — narrower than it looks
///
/// A proof for key `k` is made of the siblings OFF `k`'s path, so it is completely unaffected by any
/// change to `k`'s own value. Two consequences, both established by running the mistakes as positive
/// controls rather than by reasoning about them:
///
///   * Taking the absence proof after its own insert changes nothing — the two are byte-identical.
///   * Chained spends of the SAME coinbase do not invalidate each other's proofs either.
///
/// What does bite is ordering across DISTINCT keys: the coinbase insert sits on a path that is a
/// sibling of every other key that branches off it, so a spend proof taken before that insert is
/// stale and is refused. That is the one real constraint here, and it is the one the round-trip test
/// is built to catch — the first two controls passed against a deliberately broken implementation,
/// which is exactly why they are recorded here instead of being trusted as tests.
fn smt_advance(
    smt: &mut Smt,
    coinbase_txid: [u8; 32],
    coinbase_outputs: u32,
    coinbase_spends: &[[u8; 32]],
) -> SmtBlockWitness {
    // 1. Absence, against the INCOMING root — before any of this block's own updates.
    let absence_proof = smt.prove(&coinbase_txid);
    if coinbase_outputs > 0 { smt.insert(coinbase_txid, coinbase_outputs); }

    // 2. Decrement each spend, proving against the root as it stands at that step. This is what lets
    //    a block spend two outputs of the same coinbase: the second proof sees the first's effect.
    let mut spends = Vec::with_capacity(coinbase_spends.len());
    for t in coinbase_spends {
        let cur = smt.get(t).unwrap_or(0);
        assert!(cur > 0,
            "bridge: a block spends coinbase {} which the SMT holds at zero — the tree and the UTXO \
             set have diverged, which is a bug here and not a property of the chain",
            t.iter().map(|b| format!("{b:02x}")).collect::<String>());
        spends.push(SmtSpend { coinbase_txid: *t, current_count: cur, proof: smt.prove(t) });
        smt.insert(*t, cur - 1);
    }
    SmtBlockWitness { coinbase_txid, coinbase_outputs, absence_proof, spends }
}

#[derive(Serialize, Deserialize)]
struct Bundle {
    height: u32, in_tip: [u8; 32],
    in_roots: Vec<Option<[u8; 32]>>, in_leaves: u64,
    in_nbits: u32, in_time: u32, in_epoch_start: u32, in_recent: Vec<u32>,
    witness: BlockWitness,
}

// Regression test for the v0.9.0 bundle-parse bug. `PackedBytes` serialises via `serialize_bytes`, which
// serde_json emits as a JSON array (a *sequence*) since JSON has no native bytes type — so the deserialiser
// must accept a sequence, not only a bytes type. Any block with a non-empty `txs`/`tx_prevouts` (i.e. every
// block with a real spend; block 170 is the first) exercises this path. The whole adversarial+regression
// suite passed while this was broken because those prove from in-memory witnesses; ONLY the bridge →
// bundle_<n>.json → prove-range-bridge round-trip (this exact `to_vec` → `from_slice`) hit it. This test
// closes that coverage gap: it must PASS after the fix and would panic ("invalid type: sequence") before it.
fn bundle_roundtrip_test() {
    let mut fails = 0;
    // 1) PackedBytes alone, across cases that stress the JSON sequence path: empty, single, the full 0..=255
    //    range (bytes > 127 are where a signed/unsigned mixup would show), and a hand-picked mix.
    for case in [Vec::new(), vec![0u8], (0u8..=255).collect::<Vec<u8>>(), vec![255, 128, 0, 1, 127, 200]] {
        let j = serde_json::to_vec(&PackedBytes(case.clone())).expect("serialise PackedBytes");
        let back: PackedBytes = serde_json::from_slice(&j)
            .expect("PackedBytes JSON round-trip must parse (regression: v0.9.0 'invalid type: sequence')");
        if back.0 != case { println!(">>> BUNDLE-ROUNDTRIP: FAIL — PackedBytes {} bytes mismatch", case.len()); fails += 1; }
    }
    // 2) A full Bundle carrying a spend tx — the exact struct prove-range-bridge parses. Non-empty txs +
    //    tx_prevouts with bytes spanning the range, so this is the production shape that used to fail.
    let raw_tx: Vec<u8> = (0u8..=255).cycle().take(400).collect();
    let prevouts: Vec<u8> = (0u8..=255).rev().cycle().take(140).collect();
    let w = BlockWitness {
        header: vec![1, 2, 3], height: 170, coinbase_tx: vec![9, 9, 9],
        txids: vec![[7u8; 32], [8u8; 32]], wtxids: vec![[0u8; 32], [0u8; 32]],
        root_prev: WireStump { roots: vec![], num_leaves: 0 },
        txs: vec![PackedBytes(raw_tx.clone())], tx_prevouts: vec![PackedBytes(prevouts.clone())],
        inputs: vec![], new_outputs: vec![[5u8; 32]],
        root_next: WireStump { roots: vec![Some([2u8; 32])], num_leaves: 1 }, bip30: None,
        // Round-trip test only — never executed, so a bare standalone transition is enough.
        in_smt_root: [0u8; 32], smt: smt_witness_standalone([1u8; 32], 1, &[]).1,
    };
    let b = Bundle {
        height: 170, in_tip: [1u8; 32], in_roots: vec![None, Some([2u8; 32])], in_leaves: 42,
        in_nbits: 0x1d00_ffff, in_time: 1_231_731_025, in_epoch_start: 1_231_006_505, in_recent: vec![1, 2, 3],
        witness: w,
    };
    let j = serde_json::to_vec(&b).expect("serialise Bundle");
    let back: Bundle = serde_json::from_slice(&j)
        .expect("Bundle JSON round-trip must parse (regression: the v0.9.0 spend-block parse bug)");
    if back.witness.txs.first().map(|p| &p.0) != Some(&raw_tx) {
        println!(">>> BUNDLE-ROUNDTRIP: FAIL — Bundle.witness.txs mismatch"); fails += 1;
    }
    if back.witness.tx_prevouts.first().map(|p| &p.0) != Some(&prevouts) {
        println!(">>> BUNDLE-ROUNDTRIP: FAIL — Bundle.witness.tx_prevouts mismatch"); fails += 1;
    }
    if fails == 0 { println!(">>> BUNDLE-ROUNDTRIP: PASS — PackedBytes + full spend-block Bundle JSON round-trip"); }
    else { std::process::exit(1); }
}


// ============================================================================================
// G4 — proven assumeutxo: bind a UTXO snapshot to a range proof (#42)
// ============================================================================================
//
// Core already ships `assumeutxo`: a node adopts a UTXO set at height N on the authority of a hash
// its developers chose. This replaces that authority with a proof, the same substitution Hazync makes
// for `assumevalid`. A node that verifies the proof, rebuilds the accumulator from the snapshot, and
// finds the roots equal can start at N+1 without validating anything beneath it.
//
// TWO THINGS CORE'S SNAPSHOT DOES NOT GIVE YOU, and neither is an obstacle:
//
//   coin_mtp   The leaf commits MTP(coin_height - 1) so BIP68 time-locks are checked against Core's
//              own value, and Core's UTXO representation does not store it. It is a pure function of
//              the HEADER chain, which a node doing headers-first sync already has. Verified against
//              real mainnet data: 6/6 inputs from block 195,000 reproduce exactly.
//
//   leaf order The forest is an ORDERED array with swap-and-pop deletion, so its layout depends on
//              deletion history, not just on which coins are live. Two histories ending with the same
//              live set produce different roots. A snapshot is a set, so the order must be carried —
//              done here by EMITTING IN FOREST-POSITION ORDER, which costs zero extra bytes. The
//              records are exactly Core's content; only the sort is specified. (Core's own
//              `dumptxoutset` orders by outpoint, so the two files are not byte-identical.)
//
// Format, little-endian, no framing beyond what is written:
//   magic "HZSNAP01" | height u32 | count u64 | count x { txid[32] vout u32 value u64
//                                                        spk_len u32 spk[] height u32 coinbase u8 }
// Records appear in forest-position order: record i is the coin at accumulator position i.

const SNAP_MAGIC: &[u8; 8] = b"HZSNAP01";

fn snapshot_emit_cmd(dir: &str, out: &str) {
    let st = bridge_load_state(dir).expect("no bridge checkpoint in that directory");
    // Invert the utxo map by leaf so records can be written in forest-position order. The bridge keeps
    // `leaves` (ordered, the accumulator) and `utxo` (metadata, unordered) separately; the leaf hash is
    // the only thing linking them.
    let mut by_leaf: std::collections::HashMap<[u8; 32], (&([u8; 32], u32), &(u64, Vec<u8>, u32, bool))> =
        std::collections::HashMap::with_capacity(st.utxo.len());
    for (op, meta) in st.utxo.iter() {
        let mtp = mtp_at(meta.2);
        let leaf = coin_leaf(&op.0, op.1, meta.0, &meta.1, meta.2, meta.3, mtp);
        by_leaf.insert(leaf, (op, meta));
    }

    let mut buf: Vec<u8> = Vec::new();
    buf.extend_from_slice(SNAP_MAGIC);
    buf.extend_from_slice(&st.height.to_le_bytes());
    buf.extend_from_slice(&(st.leaves.len() as u64).to_le_bytes());
    let mut missing = 0usize;
    for leaf in &st.leaves {
        match by_leaf.get(leaf) {
            Some((op, meta)) => {
                buf.extend_from_slice(&op.0);
                buf.extend_from_slice(&op.1.to_le_bytes());
                buf.extend_from_slice(&meta.0.to_le_bytes());
                buf.extend_from_slice(&(meta.1.len() as u32).to_le_bytes());
                buf.extend_from_slice(&meta.1);
                buf.extend_from_slice(&meta.2.to_le_bytes());
                buf.push(meta.3 as u8);
            }
            None => missing += 1,
        }
    }
    // A leaf with no metadata means the two halves of the bridge state disagree. Emitting a short
    // snapshot would produce a file that simply fails to verify later, with no clue why.
    assert!(missing == 0, "{missing} accumulator leaves have no UTXO metadata — bridge state is inconsistent");
    std::fs::write(out, &buf).expect("write snapshot");
    println!("wrote {out}: height {}, {} coins, {} bytes (forest-position order)",
             st.height, st.leaves.len(), buf.len());
}

/// MTP(h-1) from the header chain — the value the leaf commits. Cached, because a snapshot has many
/// coins per height and each miss is 22 RPC round-trips.
fn mtp_at(coin_height: u32) -> u32 {
    use std::sync::Mutex;
    static CACHE: Mutex<Option<std::collections::HashMap<u32, u32>>> = Mutex::new(None);
    let mut g = CACHE.lock().unwrap();
    let c = g.get_or_insert_with(std::collections::HashMap::new);
    if let Some(v) = c.get(&coin_height) { return *v; }
    let lo = coin_height.saturating_sub(11);
    let mut ts: Vec<u32> = Vec::new();
    for h in lo..coin_height {
        let hash = bcli(&["getblockhash", &h.to_string()]);
        let hdr = bcli(&["getblockheader", &hash]);
        let t = hdr.split("\"time\":").nth(1).and_then(|x| x.split(|ch: char| !ch.is_ascii_digit()).find(|x| !x.is_empty()))
            .and_then(|x| x.parse::<u32>().ok()).expect("header time");
        ts.push(t);
    }
    ts.sort_unstable();
    let m = ts[ts.len() / 2];
    c.insert(coin_height, m);
    m
}

fn snapshot_verify_cmd(snap: &str, proof: &str) {
    let b = std::fs::read(snap).expect("read snapshot");
    assert!(b.len() > 20 && &b[..8] == SNAP_MAGIC, "not a HZSNAP01 snapshot");
    let height = u32::from_le_bytes(b[8..12].try_into().unwrap());
    let count = u64::from_le_bytes(b[12..20].try_into().unwrap()) as usize;

    // Rebuild every leaf FROM THE SNAPSHOT. Taking them from the bridge would only prove the bridge
    // agrees with itself; the whole point is to check the snapshot independently.
    let mut leaves: Vec<[u8; 32]> = Vec::with_capacity(count);
    let mut p = 20usize;
    for _ in 0..count {
        let txid: [u8; 32] = b[p..p + 32].try_into().unwrap(); p += 32;
        let vout = u32::from_le_bytes(b[p..p + 4].try_into().unwrap()); p += 4;
        let value = u64::from_le_bytes(b[p..p + 8].try_into().unwrap()); p += 8;
        let sl = u32::from_le_bytes(b[p..p + 4].try_into().unwrap()) as usize; p += 4;
        let spk = b[p..p + sl].to_vec(); p += sl;
        let ch = u32::from_le_bytes(b[p..p + 4].try_into().unwrap()); p += 4;
        let cb = b[p] != 0; p += 1;
        leaves.push(coin_leaf(&txid, vout, value, &spk, ch, cb, mtp_at(ch)));
    }
    assert!(p == b.len(), "trailing bytes in snapshot: parsed {p} of {}", b.len());

    let forest = Forest::from_leaves(leaves);
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(proof).expect("proof")).unwrap();
    let claimed = r.journal.decode::<RangeState>().ok().map(|rs| rs.self_id);
    verify_receipt_ex(&r, claimed);
    let rs: RangeState = r.journal.decode().unwrap();
    assert!(rs.self_id == METHOD_ID, "proof was made by a different guest");
    assert!(rs.kind == KIND_RANGE, "receipt is not a RangeState");

    let ok_h = rs.hi == height;
    let ok_n = rs.out_leaves == forest.leaves.len() as u64;
    let ok_r = normalize_host(rs.out_roots.clone()) == normalize_host(forest.roots());
    println!("snapshot height {height}, {} coins", forest.leaves.len());
    println!("  proof range        [{}..{}]", rs.lo, rs.hi);
    println!("  height matches     {ok_h}");
    println!("  leaf count matches {ok_n}   (snapshot {} vs proof {})", forest.leaves.len(), rs.out_leaves);
    println!("  ROOTS match        {ok_r}");
    if ok_h && ok_n && ok_r {
        println!(">>> SNAPSHOT BOUND TO PROOF — this is the UTXO set the proof attests to.");
        println!("    A node may adopt it at height {height} and validate from {} onward.", height + 1);
    } else {
        eprintln!(">>> NOT BOUND — the snapshot is not the set this proof commits to.");
        std::process::exit(1);
    }
}

fn bcli(args: &[&str]) -> String {
    let dd = std::env::var("HAZYNC_BITCOIN_DATADIR").unwrap_or_else(|_| "/root/.bitcoin".into());
    let o = std::process::Command::new("bitcoin-cli").arg(format!("-datadir={dd}")).args(args)
        .output().expect("run bitcoin-cli");
    if !o.status.success() { panic!("bitcoin-cli {args:?}: {}", String::from_utf8_lossy(&o.stderr)); }
    String::from_utf8_lossy(&o.stdout).trim().to_string()
}

type Utxo = std::collections::HashMap<([u8; 32], u32), (u64, Vec<u8>, u32, bool)>; // op -> value,spk,height,coinbase

// The block-JSON build_block_carried expects, sourcing prevouts from the UTXO map (external spends) or
// this block's own outputs (in-block spends).
fn bridge_block_json(block: &bitcoin::Block, height: u32, utxo: &Utxo) -> serde_json::Value {
    let hdr = &block.header;
    let mut cur: std::collections::HashMap<([u8; 32], u32), (u64, Vec<u8>)> = std::collections::HashMap::new();
    for t in &block.txdata {
        let txid = t.compute_txid().to_byte_array();
        for (v, o) in t.output.iter().enumerate() {
            cur.insert((txid, v as u32), (o.value.to_sat(), o.script_pubkey.as_bytes().to_vec()));
        }
    }
    let coinbase = &block.txdata[0];
    let txs: Vec<serde_json::Value> = block.txdata[1..].iter().map(|t| {
        let prevouts: Vec<serde_json::Value> = t.input.iter().map(|inp| {
            let op = inp.previous_output;
            let key = (op.txid.to_byte_array(), op.vout);
            if let Some((value, spk, ch, cb)) = utxo.get(&key) {
                serde_json::json!({ "value": value, "spk": hex(spk), "coin_height": ch, "coin_is_coinbase": *cb as u32 })
            } else {
                let (value, spk) = cur.get(&key).expect("prevout in neither UTXO map nor this block");
                serde_json::json!({ "value": value, "spk": hex(spk), "coin_height": height, "coin_is_coinbase": 0 })
            }
        }).collect();
        serde_json::json!({ "raw": hex(&serialize(t)), "prevouts": prevouts })
    }).collect();
    let mut prev = hdr.prev_blockhash.to_byte_array(); prev.reverse();  // -> DISPLAY order
    let mut mrk = hdr.merkle_root.to_byte_array(); mrk.reverse();
    serde_json::json!({
        "height": height, "bits": hdr.bits.to_consensus(), "time": hdr.time, "nonce": hdr.nonce,
        "version": hdr.version.to_consensus(), "prev": hex(&prev), "merkle": hex(&mrk),
        "coinbase_hex": hex(&serialize(coinbase)), "txs": txs,
    })
}

// Advance the UTXO map to mirror build_block_carried's forest transition (remove external spends, add new
// spendable outputs not spent within this block).
fn bridge_update_utxo(utxo: &mut Utxo, block: &bitcoin::Block, height: u32) {
    let mut spent: std::collections::HashSet<([u8; 32], u32)> = std::collections::HashSet::new();
    for t in block.txdata.iter().skip(1) {
        for inp in &t.input { spent.insert((inp.previous_output.txid.to_byte_array(), inp.previous_output.vout)); }
    }
    for k in &spent { utxo.remove(k); }
    for (ti, t) in block.txdata.iter().enumerate() {
        let txid = t.compute_txid().to_byte_array();
        let is_cb = ti == 0;
        for (v, o) in t.output.iter().enumerate() {
            if !out_spendable(o.script_pubkey.as_bytes()) { continue; }
            if spent.contains(&(txid, v as u32)) { continue; }
            utxo.insert((txid, v as u32), (o.value.to_sat(), o.script_pubkey.as_bytes().to_vec(), height, is_cb));
        }
    }
}

// The resident forest + UTXO-metadata map + MTP/difficulty carry, checkpointed so the bridge resumes
// mid-chain instead of rebuilding from genesis. `leaves` is the live UTXO set (swap-and-pop), so at the
// tip it is ~the UTXO count, not the full history. Borrowed on save (no clone of the multi-GB state),
// owned on load; field order MUST match between the two (bincode is positional).
//
// #54 adds `smt`: the coinbase-SMT entries (txid -> unspent output count) backing BIP30
// non-membership. Stored as a sorted Vec rather than the live `Smt` because bincode is positional and
// a HashMap's iteration order is not — a checkpoint that round-tripped differently each save would be
// a state that disagrees with itself.
//
// APPENDING THIS FIELD INVALIDATES EXISTING CHECKPOINTS, deliberately. bincode is positional, so an
// old state.bin fails to deserialise and the bridge rebuilds from genesis. That is the correct
// outcome and not merely tolerable: the SMT has to be built by replaying the chain anyway — there is
// no way to derive "which coinbases still have unspent outputs" from a checkpoint that never tracked
// it — so a resume that silently kept the old state would produce a tree missing all history before
// the upgrade.
#[derive(Serialize)]
struct BridgeStateRef<'a> {
    height: u32, leaves: &'a Vec<[u8; 32]>, utxo: &'a Utxo,
    win: &'a Vec<u32>, block_mtp: &'a Vec<u32>, nbits: u32, time: u32, epoch_start: u32,
    smt: Vec<([u8; 32], u32)>,
}
#[derive(Deserialize)]
struct BridgeState {
    height: u32, leaves: Vec<[u8; 32]>, utxo: Utxo,
    win: Vec<u32>, block_mtp: Vec<u32>, nbits: u32, time: u32, epoch_start: u32,
    smt: Vec<([u8; 32], u32)>,
}

fn bridge_load_state(dir: &str) -> Option<BridgeState> {
    std::fs::read(format!("{dir}/state.bin")).ok().and_then(|b| bincode::deserialize(&b).ok())
}
fn bridge_save_state(dir: &str, st: &BridgeStateRef) {
    let tmp = format!("{dir}/state.bin.tmp");
    std::fs::write(&tmp, bincode::serialize(st).unwrap()).expect("write checkpoint");
    std::fs::rename(&tmp, format!("{dir}/state.bin")).expect("commit checkpoint"); // atomic: never a torn state.bin
}

fn cmd_bridge() {
    let out_dir = std::env::var("HAZYNC_BRIDGE_OUT").unwrap_or_else(|_| "/root/bridge_bundles".into());
    std::fs::create_dir_all(&out_dir).unwrap();
    // Only advance/emit up to tip-FINALITY: the resident forest never enters the re-org zone, so any
    // shallower-than-FINALITY reorg leaves it untouched and every emitted bundle is on the final chain.
    let finality: u32 = std::env::var("HAZYNC_BRIDGE_FINALITY").ok().and_then(|s| s.parse().ok()).unwrap_or(100);
    let ckpt_every: u32 = std::env::var("HAZYNC_BRIDGE_CKPT").ok().and_then(|s| s.parse().ok()).unwrap_or(2000);
    let poll: u64 = std::env::var("HAZYNC_BRIDGE_POLL").ok().and_then(|s| s.parse().ok()).unwrap_or(30);
    let once = std::env::var("HAZYNC_BRIDGE_ONCE").is_ok();     // exit after catching up once (seeding / tests)
    let cap = std::env::var("HAZYNC_BRIDGE_TO").ok().and_then(|s| s.parse::<u32>().ok()); // optional hard height cap

    // Resume from the last checkpoint, or start fresh at genesis.
    // A checkpoint written before #54 has no `smt` field, so bincode refuses it and this falls through
    // to genesis. That is intended — see the note on BridgeState. The log line below says so out loud,
    // because "starting from genesis" after an upgrade otherwise looks like data loss.
    let (mut forest, mut utxo, mut win, mut block_mtp, mut nbits, mut time, mut epoch_start, mut done, mut smt) =
        if let Some(st) = bridge_load_state(&out_dir) {
            println!("bridge: resuming from checkpoint @ height {} ({} utxos, {} leaves, {} coinbase-SMT entries)",
                st.height, st.utxo.len(), st.leaves.len(), st.smt.len());
            (Forest::from_leaves(st.leaves), st.utxo, st.win, st.block_mtp, st.nbits, st.time, st.epoch_start,
             st.height, Smt::from_entries(st.smt))
        } else {
            if std::path::Path::new(&format!("{out_dir}/state.bin")).exists() {
                println!("bridge: checkpoint present but not readable under this build — \
                          rebuilding from genesis (expected on the #54 upgrade: the coinbase SMT \
                          cannot be derived from a checkpoint that never tracked it)");
            }
            println!("bridge: no checkpoint — starting from genesis");
            (Forest::new(), Utxo::new(), vec![GENESIS_TIME], vec![GENESIS_TIME],
             GENESIS_BITS, GENESIS_TIME, GENESIS_TIME, 0u32, Smt::new())
        };
    let mut last_ckpt = done;
    loop {
        let tip: u32 = bcli(&["getblockcount"]).parse().expect("getblockcount");
        let mut target = tip.saturating_sub(finality);
        if let Some(c) = cap { target = target.min(c); }
        if target > done {
            println!("bridge: advancing {}..={target} (node tip {tip}, finality {finality}) -> {out_dir}", done + 1);
            for h in (done + 1)..=target {
                let hash = bcli(&["getblockhash", &h.to_string()]);
                let raw = bcli(&["getblock", &hash, "0"]);
                let block: bitcoin::Block = deserialize(&hx(&raw)).expect("parse block");
                let j = bridge_block_json(&block, h, &utxo);
                let s = wire_stump(&forest);
                let in_recent = win.clone();
                let (in_nbits, in_time, in_epoch_start) = (nbits, time, epoch_start);
                let in_tip = block.header.prev_blockhash.to_byte_array(); // internal order = tip of h-1
                push_mtp(&j, &mut win, &mut block_mtp);
                let w = build_block_carried(&mut forest, &j, &block_mtp);

                // #54 — advance the coinbase SMT, in the SAME order the guest's apply_block does:
                // check-then-insert the new coinbase, then decrement each spent coinbase output. The
                // guest's proofs are sequenced against intermediate roots, so a different order here
                // produces proofs it will refuse. That coupling is documented in bip30.rs and is the
                // reason this block sits immediately after build_block_carried rather than anywhere
                // more convenient.
                let smt_root_in: [u8; 32] = smt.root();
                let smt_witness = {
                    let cb = block.txdata[0].compute_txid().to_byte_array();
                    // Count only SPENDABLE outputs: an OP_RETURN coinbase output can never be spent,
                    // so counting it would leave the entry permanently nonzero and reject-valid a
                    // legal duplicate for ever. Matches the accumulator's out_spendable rule exactly.
                    let nout = block.txdata[0].output.iter()
                        .filter(|o| out_spendable(o.script_pubkey.as_bytes())).count() as u32;
                    // `utxo` still holds the PRE-block state here, which is what tells us whether a
                    // spent coin was a coinbase output.
                    let mut cb_spends: Vec<[u8; 32]> = Vec::new();
                    for tx in block.txdata.iter().skip(1) {
                        for inp in &tx.input {
                            let key = (inp.previous_output.txid.to_byte_array(), inp.previous_output.vout);
                            if matches!(utxo.get(&key), Some((_, _, _, true))) { cb_spends.push(key.0); }
                        }
                    }
                    smt_advance(&mut smt, cb, nout, &cb_spends)
                };
                let mut w = w;
                w.in_smt_root = smt_root_in;
                w.smt = smt_witness;
                bridge_update_utxo(&mut utxo, &block, h);
                let bundle = Bundle { height: h, in_tip, in_roots: s.roots, in_leaves: s.num_leaves,
                    in_nbits, in_time, in_epoch_start, in_recent, witness: w };
                // atomic bundle write too — a prover polling the dir never reads a half-written bundle
                let bp = format!("{out_dir}/bundle_{h}.json");
                std::fs::write(format!("{bp}.tmp"), serde_json::to_vec(&bundle).unwrap()).unwrap();
                std::fs::rename(format!("{bp}.tmp"), &bp).unwrap();
                let bt = block.header.time; nbits = block.header.bits.to_consensus(); time = bt;
                if h % 2016 == 0 { epoch_start = bt; }
                done = h;
                if done - last_ckpt >= ckpt_every {
                    bridge_save_state(&out_dir, &BridgeStateRef { height: done, leaves: &forest.leaves,
                        utxo: &utxo, win: &win, block_mtp: &block_mtp, nbits, time, epoch_start,
                        smt: smt.entries() });
                    last_ckpt = done;
                    println!("bridge: checkpoint @ {done} ({} utxos, {} leaves)", utxo.len(), forest.leaves.len());
                }
                if h % 5000 == 0 { println!("bridge: emitted through {h}/{target}"); }
            }
            // checkpoint on catch-up so a one-shot run and each tip-follow cycle persist their progress
            if done > last_ckpt {
                bridge_save_state(&out_dir, &BridgeStateRef { height: done, leaves: &forest.leaves,
                    utxo: &utxo, win: &win, block_mtp: &block_mtp, nbits, time, epoch_start,
                        smt: smt.entries() });
                last_ckpt = done;
            }
            println!("bridge: caught up to {done} (node tip {tip})");
        }
        if once { break; }
        std::thread::sleep(std::time::Duration::from_secs(poll)); // follow the tip
    }
}

// `host prove-range-bridge <n>`: prove block n from its bridge bundle (mode 6), NO replay. Same output as
// prove-range (range_n.bin), so fold-range / verify-range / submit are unchanged.
fn cmd_prove_range_bridge(n: u32) {
    use std::time::Instant;
    let dir = std::env::var("HAZYNC_BRIDGE_OUT").unwrap_or_else(|_| "/root/bridge_bundles".into());
    let raw = std::fs::read(format!("{dir}/bundle_{n}.json")).expect("read bundle");
    let bd: Bundle = serde_json::from_slice(&raw).expect("parse bundle");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&6u32).unwrap();
    b.write(&bd.in_tip).unwrap();
    b.write(&bd.in_roots).unwrap();
    b.write(&bd.in_leaves).unwrap();
    b.write(&bd.in_nbits).unwrap();
    b.write(&bd.in_time).unwrap();
    b.write(&bd.in_epoch_start).unwrap();
    b.write(&bd.in_recent).unwrap();
    b.write(&bd.witness).unwrap();
    b.write(&METHOD_ID).unwrap();
    let t = Instant::now();
    let receipt = default_prover().prove_with_opts(b.build().unwrap(), METHOD_ELF, &ProverOpts::succinct())
        .expect("prove range (bridge)").receipt;
    receipt.verify(METHOD_ID).expect("verify");
    let out = std::env::var("HAZYNC_OUT").unwrap_or_else(|_| format!("range_{n}.bin"));
    std::fs::write(&out, bincode::serialize(&receipt).unwrap()).unwrap();
    println!("proved range [{n}..{n}] from bridge bundle in {:.1}s -> {out}", t.elapsed().as_secs_f64());
}

// Differential audit of the guest's script-flag schedule (script_flags::block_script_flags — the SAME
// module the guest compiles) against Core's canonical mainnet GetBlockScriptFlags. The guest applies
// P2SH|WITNESS|TAPROOT retroactively to genesis — stricter than Core's height-gated activation
// (P2SH@173805, segwit@481824, taproot@709632) — so the from-genesis prover need not special-case
// pre-activation blocks. That is SOUND because extra flags can only REJECT more, never accept a
// Core-invalid block, so a Hazync proof still implies Core-validity. This test proves that safety
// property (guest ⊇ Core at every boundary), that the buried soft-forks (DERSIG/CLTV/CSV/NULLDUMMY)
// flip at Core's EXACT heights, and that the two script_flag_exception blocks behave (BIP16 → no flags;
// taproot → TAPROOT cleared, base retained).
fn script_flags_test() {
    use script_flags::*;
    const P2SH_HEIGHT: u32 = 173_805; // BIP16Height (chainparams.cpp)
    const TAPROOT_HEIGHT: u32 = 709_632;
    // Core's GetBlockScriptFlags for a NON-exception block at height h (each flag active iff h >= its height).
    let core_flags = |h: u32| -> u32 {
        let mut f = 0u32;
        if h >= P2SH_HEIGHT { f |= P2SH; }
        if h >= BIP66_HEIGHT { f |= DERSIG; }
        if h >= BIP65_HEIGHT { f |= CLTV; }
        if h >= CSV_HEIGHT { f |= CSV; }
        if h >= SEGWIT_HEIGHT { f |= WITNESS | NULLDUMMY; }
        if h >= TAPROOT_HEIGHT { f |= TAPROOT; }
        f
    };
    let non_exc = [0u8; 32]; // any non-exception hash → the base schedule
    let (mut fails, mut checked) = (0u32, 0u32);
    let mut heights: Vec<u32> = vec![0, 1, 100_000, 227_931, 959_617];
    for h in [P2SH_HEIGHT, BIP66_HEIGHT, BIP65_HEIGHT, CSV_HEIGHT, SEGWIT_HEIGHT, TAPROOT_HEIGHT] {
        heights.push(h.saturating_sub(1)); heights.push(h); heights.push(h + 1);
    }
    for &h in &heights {
        let g = block_script_flags(h, &non_exc);
        let c = core_flags(h);
        checked += 1;
        // (a) monotonic strictness / soundness: the guest never CLEARS a flag Core sets.
        if c & !g != 0 { fails += 1; println!("  SOUNDNESS FAIL h={h}: core={c:#x} guest={g:#x} missing={:#x}", c & !g); }
        // (b) buried soft-forks EXACT: guest bit == Core bit at every boundary.
        for (bit, name) in [(DERSIG, "DERSIG"), (CLTV, "CLTV"), (CSV, "CSV"), (NULLDUMMY, "NULLDUMMY")] {
            if (g & bit) != (c & bit) { fails += 1; println!("  BURIED FAIL h={h} {name}: guest={:#x} core={:#x}", g & bit, c & bit); }
        }
        // (c) retroactive base (P2SH|WITNESS|TAPROOT) always on for non-exception blocks.
        if g & (P2SH | WITNESS | TAPROOT) != (P2SH | WITNESS | TAPROOT) { fails += 1; println!("  BASE FAIL h={h}: guest={g:#x}"); }
    }
    // Exception blocks — keyed by the guest-computed block hash (unforgeable).
    let bip16 = block_script_flags(BIP16_EXCEPTION_HEIGHT, &BIP16_EXCEPTION);
    if bip16 != 0 { fails += 1; println!("  BIP16 EXCEPTION FAIL: expected 0 (SCRIPT_VERIFY_NONE), got {bip16:#x}"); }
    let tap = block_script_flags(TAPROOT_EXCEPTION_HEIGHT, &TAPROOT_EXCEPTION);
    if tap & TAPROOT != 0 { fails += 1; println!("  TAPROOT EXCEPTION FAIL: TAPROOT must be OFF, got {tap:#x}"); }
    if tap & (P2SH | WITNESS) != (P2SH | WITNESS) { fails += 1; println!("  TAPROOT EXCEPTION FAIL: P2SH|WITNESS must be ON, got {tap:#x}"); }
    // Core's mapped exception value must be a subset of the guest's (guest ⊇ Core → still sound).
    if (0u32 & !bip16) != 0 || ((P2SH | WITNESS) & !tap) != 0 { fails += 1; println!("  EXCEPTION SUBSET FAIL"); }
    checked += 2;
    println!("script-flags differential: checked {checked} height/exception cases (guest ⊇ Core, buried forks exact)");
    if fails == 0 {
        println!(">>> SCRIPT-FLAGS TEST PASS ✓");
    } else {
        println!(">>> SCRIPT-FLAGS TEST FAIL — {fails} discrepancies");
        std::process::exit(1);
    }
}

fn main() {
    // Prove IN-PROCESS unless the operator asked for something else.
    //
    // risc0's default backend shells out to `r0vm`, a separate ~109 MB binary that is not part of this
    // release. A contributor who downloads only the prebuilt host — which is exactly what CONTRIBUTING
    // tells them to do — therefore gets `No such file or directory (os error 2)` on their first prove,
    // naming nothing. It affected the CPU binary specifically: the CUDA build links its prover in, so
    // the GPU path worked and the "no GPU still works" path did not.
    //
    // The binary is now built with risc0-zkvm's `prove` feature (see Cargo.toml), so the local prover
    // is linked in and default_prover() already selects it when RISC0_PROVER is unset. This makes the
    // intent explicit and survives a future features change; set only when unset, so RISC0_PROVER=ipc
    // / bonsai still work for anyone who wants them.
    if std::env::var_os("RISC0_PROVER").is_none() {
        std::env::set_var("RISC0_PROVER", "local");
    }
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::filter::EnvFilter::from_default_env())
        .init();
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "prove-chain-bad") { prove_chain_bad(); return; }
    if args.iter().any(|a| a == "adversarial") { adversarial(); return; }
    if args.iter().any(|a| a == "check-bip30") { check_bip30(); return; }
    if args.iter().any(|a| a == "regress") { regress(); return; }
    if let Some(p) = args.iter().position(|a| a == "prove-chunk") {
        let idx: usize = args.get(p + 1).and_then(|s| s.parse().ok()).expect("prove-chunk <index>");
        prove_chunk(idx);
        return;
    }
    if args.iter().any(|a| a == "agg-chunks") {
        agg_chunks();
        return;
    }
    if args.iter().any(|a| a == "prove-block") {
        prove_block();
        return;
    }
    if args.iter().any(|a| a == "test-locks") {
        test_locks_cmd();
        return;
    }
    if args.iter().any(|a| a == "test-merkle") {
        test_merkle_cmd();
        return;
    }
    if args.iter().any(|a| a == "check-full") {
        check_full();
        return;
    }
    if args.iter().any(|a| a == "check-ibd") {
        check_ibd();
        return;
    }
    if args.iter().any(|a| a == "prove-ibd") {
        prove_ibd();
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "prove-range") {
        let n: u32 = args.get(p + 1).and_then(|s| s.parse().ok()).expect("prove-range <n>");
        prove_range_cmd(n);
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "fold-range") {
        let (l, r, o) = (args.get(p + 1).expect("left"), args.get(p + 2).expect("right"), args.get(p + 3).expect("out"));
        fold_range_cmd(l, r, o);
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "extend-spine") {
        let (s, n, o) = (args.get(p + 1).expect("extend-spine <spine.bin> <next.bin> <out.bin>"),
                         args.get(p + 2).expect("extend-spine <spine.bin> <next.bin> <out.bin>"),
                         args.get(p + 3).expect("extend-spine <spine.bin> <next.bin> <out.bin>"));
        extend_spine_cmd(s, n, o);
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "verify-range") {
        verify_range_cmd(args.get(p + 1).expect("verify-range <bin>"));
        return;
    }
    if args.iter().any(|a| a == "method-id") {
        // Print THIS host's guest image id so a contributor can check it matches a proof's guest.
        println!("METHOD_ID {}", method_id_hex());
        println!("  u32x8   {:?}", METHOD_ID);
        return;
    }
    if args.iter().any(|a| a == "seg-po2") {
        // The segment po2 this binary would use, so the CLI's retry ladder can start from the binary's
        // own per-backend default (21 cuda / 20 CPU) instead of guessing and wasting a duplicate attempt.
        println!("{}", seg_po2());
        return;
    }
    if args.iter().any(|a| a == "script-flags-test") { script_flags_test(); return; }
    if args.iter().any(|a| a == "bundle-roundtrip-test") { bundle_roundtrip_test(); return; }
    if let Some(p) = args.iter().position(|a| a == "snapshot-emit") {
        snapshot_emit_cmd(args.get(p + 1).map(|s| s.as_str()).unwrap_or("/root/bridge_bundles"),
                          args.get(p + 2).expect("snapshot-emit <bridge_dir> <out.snap>"));
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "snapshot-verify") {
        snapshot_verify_cmd(args.get(p + 1).expect("snapshot-verify <snap> <proof>"),
                            args.get(p + 2).expect("snapshot-verify <snap> <proof>"));
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "verify-any") {
        verify_any_cmd(args.get(p + 1).expect("verify-any <bin>"));
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "snark-wrap") {
        snark_wrap_cmd(args.get(p + 1).expect("snark-wrap <range.bin> <out.snark>"),
                       args.get(p + 2).expect("snark-wrap <range.bin> <out.snark>"));
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "verify-snark") {
        verify_snark_cmd(args.get(p + 1).expect("verify-snark <out.snark>"));
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "verify-chain") {
        verify_chain_cmd(args.get(p + 1).expect("verify-chain <bin>"));
        return;
    }
    if args.iter().any(|a| a == "bridge") {
        cmd_bridge();
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "prove-range-bridge") {
        let n: u32 = args.get(p + 1).and_then(|s| s.parse().ok()).expect("prove-range-bridge <n>");
        cmd_prove_range_bridge(n);
        return;
    }
    if args.iter().any(|a| a == "prove-full") {
        prove_full();
        return;
    }
    if args.iter().any(|a| a == "prove-seg") {
        prove_seg();
        return;
    }
    if args.iter().any(|a| a == "prove-snark") {
        prove_snark();
        return;
    }
    if args.iter().any(|a| a == "prove-chain") {
        prove_chain();
        return;
    }
    println!("=== Hazync CHAIN PROOF — fold real mainnet blocks 170 → 171 → 172 (IVC transition) ===\n");

    // Running UTXO accumulator (the bridge). Seed with block-9's coinbase (spent in block 170) + filler.
    let mut forest = Forest::new();
    for i in 0..4u64 { forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat())); }
    let spk9 = ScriptBuf::from_bytes(hx(SPEND170_PREV_SPK));
    let spend170_tx: Transaction = deserialize(&hx(SPEND170)).unwrap();
    let op9 = spend170_tx.input[0].previous_output;
    // Block 9's coinbase output (height 9, coinbase) — spent by block 170, i.e. 161 blocks mature.
    forest.add(coin_leaf(&op9.txid.to_byte_array(), op9.vout, SPEND170_PREV_VALUE, spk9.as_bytes(), 9, true, 1_231_473_279));
    for i in 0..2u64 { forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat())); }

    // Anchor checkpoint = the trusted state at block 169 (interim: single-signer GHAST checkpoint).
    let anchor = ChainState {
        kind: KIND_CHAIN,
        tip_hash: arr(rev(hx(HASH169))), utxo_roots: forest.roots(), utxo_leaves: forest.leaves.len() as u64,
        cum_work: [0u8; 32], height: 169,
        prev_nbits: 0x1d00ffff, prev_time: 1_231_730_523, // block 169 (difficulty-1 epoch)
        epoch_start: 1_231_006_505, // epoch 0's first block = genesis timestamp
        // last 11 block timestamps up to 169 (approx; MTP unused pre-BIP113 at heights 170-172).
        recent_times: (0..11).map(|i| 1_231_729_000u32 + i * 140).collect(),
        anchor_id: [0u8; 32], self_id: METHOD_ID,
    };

    // Fold each real block. (170 has the P2PK spend; 171/172 are coinbase-only.)
    let blocks: Vec<(u32, Vec<u8>, &str, Vec<Spend>, &str)> = vec![
        (170, build_header(HASH169, &arr(rev(hx(MERKLE170))), 1_231_731_025, 0x1d00ffff, 1_889_418_792), CB170,
            vec![Spend { raw: hx(SPEND170), prev_value: SPEND170_PREV_VALUE, prev_spk: hx(SPEND170_PREV_SPK), coin_height: 9, coin_is_coinbase: true, coin_mtp: 1_231_473_279 }], HASH170),
        (171, vec![], CB171, vec![], HASH171), // header built below (merkle = coinbase txid)
        (172, vec![], CB172, vec![], HASH172),
    ];

    let mut state = anchor.clone();
    let mut recent = anchor.recent_times.clone();
    for (i, (height, hdr0, cb_hex, spends, expect_hash)) in blocks.into_iter().enumerate() {
        // For coinbase-only blocks (empty hdr0), build the header now: merkle = coinbase txid.
        let header = if hdr0.is_empty() {
            let cb: Transaction = deserialize(&hx(cb_hex)).unwrap();
            let (prev, time, nonce) = match height {
                171 => (HASH170, 1_231_731_401u32, 653_436_935u32),
                172 => (HASH171, 1_231_731_853, 1_565_279_797),
                _ => unreachable!(),
            };
            build_header(prev, &cb.compute_txid().to_byte_array(), time, 0x1d00ffff, nonce)
        } else { hdr0 };

        let create_mtp = median_u32(&recent);
        let blk_time = u32::from_le_bytes(header[68..72].try_into().unwrap());
        let w = build_block(&mut forest, header, height, cb_hex, &spends, create_mtp);
        advance_recent(&mut recent, blk_time);
        let is_base = if i == 0 { 1 } else { 0 };
        let (next, cycles) = chain_step(&state, &w, is_base);

        let hash_ok = next.tip_hash == arr(rev(hx(expect_hash)));
        println!("block {height:>3}: tip {} {}  height {}  cumwork {}  Δwork {}  ({} cyc){}",
            &hex(&next.tip_hash)[..16], if hash_ok { "✓" } else { "✗MISMATCH" }, next.height,
            work_u128(&next.cum_work), work_u128(&next.cum_work) - work_u128(&state.cum_work), cycles,
            if is_base == 1 { "  [base: anchored at 169]" } else { "  [recursion hook: prev is a chain proof]" });
        state = next;
    }

    println!("\n>>> CHAIN TIP at height {} — cumulative work {} over 3 real blocks, one linked UTXO root.",
        state.height, work_u128(&state.cum_work));
    println!(">>> Each step (all enforced in-guest, panic⇒reject): scripts + CheckTransaction + no-inflation");
    println!("    + PoW + merkle + subsidy + weight ≤4M + sigops ≤80k + difficulty-retarget + coinbase-maturity");
    println!("    + absolute-locktime finality + BIP68 relative-locktime + prevhash linkage + UTXO carry + work.");
    println!("    (block 170 spends block-9's coinbase 161 blocks later — the maturity rule is exercised for real.)");
    println!("    (Cryptographic recursion — env::verify(prev proof) — is the compiled hook; proving is deferred to the big box.)");

    // ---- Multi-tx segwit/P2SH/taproot validation with correct per-height flags + full sigops. ----
    println!("\n=== Multi-tx modern validation: real segwit/P2SH/taproot spends at height 800000 (all soft-forks active) ===");
    let base = std::env::var("HAZYNC_BASE")
        .unwrap_or_else(|_| format!("{}/hazync-build", std::env::var("HOME").unwrap_or_default()));
    let mut specs: Vec<(&str, SpendCheck)> = Vec::new();
    let cov: serde_json::Value = match std::fs::read_to_string(format!("{base}/coverage_spends.json")) {
        Ok(txt) => serde_json::from_str(&txt).unwrap(),
        Err(_) => {
            println!("    (skipped — modern-validation test vectors not found under {base}; set HAZYNC_BASE to the build dir to run this demo)");
            return;
        }
    };
    for (key, name) in [("v0_p2wpkh", "P2WPKH"), ("p2sh", "P2SH"), ("v0_p2wsh", "P2WSH-multisig")] {
        let j = &cov[key];
        specs.push((name, SpendCheck {
            raw_tx: hx(j["raw_tx"].as_str().unwrap()),
            prevouts: hx(j["prevouts"].as_str().unwrap()),
            block_height: 800_000,
        }));
    }
    for (file, name) in [("real_tap_full.json", "P2TR-keypath"), ("tapscript_full.json", "P2TR-script")] {
        if let Ok(txt) = std::fs::read_to_string(format!("{base}/{file}")) {
            let j: serde_json::Value = serde_json::from_str(&txt).unwrap();
            specs.push((name, SpendCheck {
                raw_tx: hx(j["raw_tx"].as_str().unwrap()),
                prevouts: hx(j["prevouts"].as_str().unwrap()),
                block_height: 800_000,
            }));
        }
    }
    let mut mb = ExecutorEnv::builder();
    mb.write(&3u32).unwrap();
    mb.write(&(specs.len() as u32)).unwrap();
    for (_, s) in &specs {
        mb.write(s).unwrap();
    }
    let sess = default_executor().execute(mb.build().unwrap(), METHOD_ELF).unwrap();
    let results: Vec<SpendResult> = sess.journal.decode().unwrap();
    println!("(flags 0x{:x} = P2SH|DERSIG|CLTV|CSV|WITNESS|NULLDUMMY|TAPROOT)", results.first().map(|r| r.flags).unwrap_or(0));
    for ((name, _), r) in specs.iter().zip(&results) {
        println!("  {:<14} script={} {}  tx_check={}  sigop_cost={}",
            name, r.script, if r.script == 1 { "VALID ✓" } else { "reject" }, r.tx_check, r.sigops);
    }
    let all_valid = results.iter().all(|r| r.script == 1 && r.tx_check == 1);
    println!(">>> multi-tx modern validation {} — segwit witness + P2SH + taproot verified with correct flags + full sigop cost.",
        if all_valid { "ALL VALID ✓" } else { "had rejects ✗" });
}

#[cfg(test)]
mod smt_bridge {
    use super::*;
    use hazync_coinbase_smt::bip30::{apply_block, BlockUpdate, Spend};

    fn txid(n: u64) -> [u8; 32] {
        use sha2::{Digest, Sha256};
        let mut b = [0u8; 32];
        b[..8].copy_from_slice(&n.to_le_bytes());
        Sha256::digest(b).into()
    }

    fn to_update(cb: [u8; 32], nout: u32, w: &SmtBlockWitness) -> BlockUpdate {
        BlockUpdate {
            coinbase_txid: cb,
            coinbase_outputs: nout,
            absence_proof: w.absence_proof.clone(),
            spends: w.spends.iter()
                .map(|s| Spend { coinbase_txid: s.coinbase_txid, current_count: s.current_count,
                                 proof: s.proof.clone() })
                .collect(),
        }
    }

    /// THE test this refactor exists for: what the bridge emits is what the guest can consume.
    ///
    /// The coupling is invisible in either half alone — the bridge produces well-formed proofs and
    /// `apply_block` verifies well-formed proofs, but if the two disagree about the ORDER the roots
    /// are taken in, every proof is against the wrong intermediate state. That fails closed, so it
    /// would present as a board that silently stops advancing rather than as a wrong answer.
    #[test]
    fn smt_emission_round_trips_through_apply_block() {
        let mut bridge = Smt::new();
        let mut guest_root = bridge.root();

        // A chain where coinbases accumulate and later blocks spend them, including two outputs of
        // the SAME coinbase inside one block — the case that only works if proofs are sequenced.
        for h in 0..40u64 {
            let cb = txid(h);
            let nout = if h % 5 == 0 { 2 } else { 1 };
            let spends: Vec<[u8; 32]> = if h >= 10 {
                if h % 5 == 0 { vec![txid(h - 10), txid(h - 10)] } else { vec![txid(h - 10)] }
            } else { Vec::new() };
            // txid(h-10) has 2 outputs exactly when (h-10) % 5 == 0, i.e. when h % 5 == 0.

            let w = smt_advance(&mut bridge, cb, nout, &spends);
            guest_root = apply_block(&guest_root, &to_update(cb, nout, &w))
                .unwrap_or_else(|e| panic!("guest refused the bridge's own block at height {h}: {e:?}"));
            assert_eq!(guest_root, bridge.root(), "roots diverged at height {h}");
        }
    }

    /// A block whose coinbase duplicates one that still has unspent outputs must be refused, using
    /// exactly the witness the bridge would have produced for it.
    #[test]
    fn the_bridge_cannot_produce_a_witness_that_passes_a_real_bip30_violation() {
        let mut bridge = Smt::new();
        let cb = txid(1);
        let w0 = smt_advance(&mut bridge, cb, 1, &[]);
        let root0 = apply_block(&Smt::new().root(), &to_update(cb, 1, &w0)).unwrap();

        // Now try the same coinbase again while it is still unspent.
        let mut replay = bridge.clone();
        let w1 = smt_advance(&mut replay, cb, 1, &[]);
        assert!(apply_block(&root0, &to_update(cb, 1, &w1)).is_err(),
                "a duplicate of an UNSPENT coinbase was accepted");
    }

    /// The two historical duplicates, in shape: once the earlier coinbase is fully spent, the later
    /// one is an ordinary insert. This is what retires the F3 grandfathered-overwrite special case.
    #[test]
    fn a_fully_spent_coinbase_can_be_duplicated_with_no_special_case() {
        let mut bridge = Smt::new();
        let dup = txid(91812);
        let w0 = smt_advance(&mut bridge, dup, 1, &[]);
        let mut root = apply_block(&Smt::new().root(), &to_update(dup, 1, &w0)).unwrap();

        // Spend it to zero via an ordinary later block.
        let w1 = smt_advance(&mut bridge, txid(999), 1, &[dup]);
        root = apply_block(&root, &to_update(txid(999), 1, &w1)).unwrap();

        // Now the duplicate is legal.
        let w2 = smt_advance(&mut bridge, dup, 1, &[]);
        root = apply_block(&root, &to_update(dup, 1, &w2))
            .expect("duplicating a fully-spent coinbase was rejected — this is legal under BIP30");
        assert_eq!(root, bridge.root());
    }
}

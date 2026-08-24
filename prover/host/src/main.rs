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
    header: Vec<u8>, height: u32, coinbase_tx: Vec<u8>, txids: Vec<[u8; 32]>,
    root_prev: WireStump, txs: Vec<PackedBytes>, tx_prevouts: Vec<PackedBytes>,
    inputs: Vec<BlockInput>, root_next: WireStump,
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
fn boundary_digest(height: u32, tip: &[u8; 32], roots: &[Option<[u8; 32]>], leaves: u64, nbits: u32, time: u32, epoch: u32, recent: &[u32], smt_root: &[u8; 32]) -> [u8; 32] {
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
    // AUDIT #3 F-3: the coinbase-SMT root is part of the boundary and MUST be in the seam digest.
    // Without it two ranges that disagree about BIP30 state but agree on everything else digest
    // IDENTICALLY, so the coordinator's out_bhash(k) == in_bhash(k+1) chaining cannot see the
    // disagreement. The guest's fold_range does assert SMT continuity, so folded proofs were always
    // safe — the hole was the verify-any chaining track the coordinator actually uses.
    m.extend_from_slice(smt_root);
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
    BlockWitness { header, height, coinbase_tx: hx(coinbase_hex), txids, root_prev, txs, tx_prevouts, inputs, root_next, bip30: None, in_smt_root, smt }
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
        // #83: use the REAL in-boundary retarget inputs when the fixture carries them. The old values
        // were fabricated — prev_time as "this block minus 600s", epoch_start as "minus 1000 blocks".
        // Harmless at a non-retarget height, where the guest carries nbits through unchanged; fatal at
        // a retarget height, where calc_next_bits consumes epoch_start and the expected target is then
        // derived from a timestamp that never existed. Block 481824 came back `block_valid=true
        // retarget_ok=false` for exactly that reason: the block was fine, the fixture could not
        // express it. Pre-#83 fixtures (130000/140000/741000) carry none of these and keep the old
        // behaviour, which is correct for them because none is a retarget height.
        prev_nbits: j["prev_bits"].as_u64().map(|v| v as u32).unwrap_or(bits),
        prev_time: j["prev_time"].as_u64().map(|v| v as u32).unwrap_or_else(|| time.saturating_sub(600)),
        epoch_start: j["epoch_start"].as_u64().map(|v| v as u32)
            .unwrap_or_else(|| time.saturating_sub(600 * 1000)),
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
    let mut w = BlockWitness { header, height, coinbase_tx: hx(cb_hex), txids, root_prev, txs, tx_prevouts, inputs, root_next, bip30: None, in_smt_root, smt };
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
        // `wtxids` and `new_outputs` were removed from the wire format — the guest recomputes both
        // and never read either, so they were pure transmission cost.
        let idlists = w.txids.len() * 32;
        let pct = |x: usize| if tot > 0 { x as f64 / tot as f64 * 100.0 } else { 0.0 };
        println!("WITNESS block {} inputs={} total={}B", w.height, n, tot);
        println!("  proof_siblings = {}B ({:.1}%)   raw_tx = {}B ({:.1}%)   prevouts = {}B ({:.1}%)", sibs, pct(sibs), rawtx, pct(rawtx), prevouts, pct(prevouts));
        println!("  txids = {}B ({:.1}%)", idlists, pct(idlists));
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
    BlockWitness { header, height, coinbase_tx: hx(cb_hex), txids, root_prev, txs, tx_prevouts, inputs, root_next, bip30, in_smt_root, smt }
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
    // AUDIT #3 F-3. #54 added the coinbase-SMT root to the boundary and pinned it in `rangestate`;
    // this gate — behind verify-range, verify-snark, verify-any's conditional pin, and therefore the
    // coordinator's spine — did not get it. A spine folded from a FABRICATED genesis SMT root passed
    // verify-range. Same drift class as F-2, and the reason the shared predicate is called here rather
    // than the assertion being copied a third time.
    //
    // Compared against `empty_root()` directly rather than via `rangestate::is_genesis_anchored`,
    // because the host deliberately keeps its own `RangeState` mirror — `check-rangestate.sh` gates
    // that mirror field-for-field against the shared crate, which is what stops THIS copy drifting.
    // A tree of empty hashes does not fold to zero, so a zeroed field is not accidentally correct.
    assert_eq!(rs.in_smt_root, hazync_coinbase_smt::empty_root(),
        "in-boundary coinbase-SMT (BIP30) root is not the empty tree — the range claims genesis but \
         starts from a fabricated BIP30 history");
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
    let in_bh = boundary_digest(rs.lo.saturating_sub(1), &rs.in_tip_hash, &rs.in_roots, rs.in_leaves, rs.in_nbits, rs.in_time, rs.in_epoch_start, &rs.in_recent, &rs.in_smt_root);
    let out_bh = boundary_digest(rs.hi, &rs.out_tip_hash, &rs.out_roots, rs.out_leaves, rs.out_nbits, rs.out_time, rs.out_epoch_start, &rs.out_recent, &rs.out_smt_root);
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
    let bounds = chunk_bounds(&w, nchunks_env());
    let nchunks = bounds.len();
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
    for (c, &(lo, hi)) in bounds.iter().enumerate() {
        let mut b = ExecutorEnv::builder();
        b.segment_limit_po2(seg_po2());
        b.write(&4u32).unwrap();
        b.write(&chunk_height).unwrap();
        b.write(&header_hash(&w.header)).unwrap(); // block hash for flag exceptions
        write_chunk_inputs(&mut b, &w, lo, hi);
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
// ---- Chunk packing: balance predicted work, not input counts (#132) ----
//
// An input's cost is dominated by EC verification — ~95% of proving, per docs/ACCELERATION.md — and how
// many signature checks an input performs depends on its script type, not on it being one input. A
// keypath taproot spend is one verify; a 15-of-15 bare multisig is fifteen. Splitting a block into equal
// INPUT COUNTS therefore leaves chunks with very unequal work, and since chunks prove in parallel a
// block's wall-clock is its slowest chunk. The straggler caps the fan-out however many GPUs are added.
//
// Chunks stay CONTIGUOUS and in input order; only their widths vary. That is not a simplification, it
// is required: the guest's `aggregate()` concatenates each chunk's `binds` in receipt order and
// `validate_block` indexes the result by the block's own input index. Reordering inputs across chunks
// would need each chunk to commit its indices and the aggregate to scatter rather than concatenate —
// a guest change, hence a new METHOD_ID, hence every existing proof invalidated. Widths are free.

// Per-input cost in guest cycles. FITTED to measurement, not assumed: `chunk-profile` was run over
// block 741000 in execute mode and these two coefficients reproduce every one of the 16 chunks' real
// cycle counts to within ~1%.
//
// REFITTED TWICE for #135, and the pattern is the point: every time a byte-scaling cost is removed the
// coefficient drops and the model must be re-measured, or the packer optimises against work that is no
// longer there.
//
//   182  serde `env::read` payload, transaction shipped per input
//    36  read_slice payload (the ~147 cycles/byte serde cost removed)
//     6  payload grouped per transaction (deserialise and Init now run once, not once per input)
//
// Each stale value did visible harm: at 182 the measured straggler was 1.64x, WORSE than not packing by
// cost at all, because byte-heavy chunks were rated far more expensive than they had become. The EC
// coefficient has not moved across any of it.
//
// What remains in the byte term is mostly `input_bind`, which still hashes the whole transaction once
// per input. That is ~2.5 cycles/byte of the 6 and is the next thing to disappear if it is ever hoisted.
//
// The byte term is the part that is easy to get wrong, and costing purely by EC verifies does get it
// wrong. Every ChunkInput carries the WHOLE spending transaction and the WHOLE prevouts blob, so a
// many-input transaction ships and re-hashes its entire body once per input. On block 741000's fattest
// chunks that marshalling is ~63% of the cycles — chunk 11 and chunk 1 have identical input counts and
// identical EC counts, and chunk 1 costs 2.5x more purely because its transactions are bigger. An
// EC-only model rates them equal and packs them as if they were.
//
// Only the RATIO matters: the packer compares costs and never predicts a wall-clock.
const COST_PER_EC_OP: u64 = 1_950_000;
// An input that verifies no signature still costs something to read, deserialise and hash. Measured at
// ~34K cycles on block 962,000's anchor spends, against ~1,953K for a P2WPKH.
const COST_INPUT_BASE: u64 = 34_000;
const COST_PER_INPUT_BYTE: u64 = 6;

/// Signature verifications this input performs, by script type. A prediction, not a guarantee — it is
/// used only to balance chunks, so being wrong costs some balance and never correctness.
fn predicted_ec_ops(raw_tx: &[u8], input_idx: u32, prevouts_blob: &[u8]) -> u64 {
    use bitcoin::blockdata::opcodes::all::{OP_CHECKSIG, OP_CHECKSIGADD, OP_CHECKSIGVERIFY};
    use bitcoin::blockdata::script::{Instruction, Script};

    // Anything unparseable is charged one verify: it should not happen, and a wrong guess here must not
    // be able to panic a prover run over what is only a scheduling hint.
    let Ok(tx) = deserialize::<Transaction>(raw_tx) else { return 1 };
    let Ok(prevouts) = deserialize::<Vec<TxOut>>(prevouts_blob) else { return 1 };
    let i = input_idx as usize;
    let (Some(txin), Some(prevout)) = (tx.input.get(i), prevouts.get(i)) else { return 1 };
    let spk = &prevout.script_pubkey;
    let wit = &txin.witness;

    // Tapscript's OP_CHECKSIGADD is not counted by count_sigops (it post-dates the legacy rules), so
    // taproot leaves need their own pass.
    let tapscript_ops = |leaf: &[u8]| -> u64 {
        Script::from_bytes(leaf)
            .instructions()
            .filter(|ins| {
                matches!(ins, Ok(Instruction::Op(op))
                    if *op == OP_CHECKSIG || *op == OP_CHECKSIGVERIFY || *op == OP_CHECKSIGADD)
            })
            .count() as u64
    };

    // Anyone-can-spend witness programs verify NOTHING: a v0 program that is not 20 or 32 bytes, or any
    // version above v1. The common case today is P2A, `OP_1 <0x4e73>` — Core v28's ephemeral anchor.
    // It is not a rounding error: **13.7% of block 962,000's 8,006 inputs are anchor spends**, each
    // measured at ~34K cycles against ~1,953K for a P2WPKH. Charging them one verify apiece put 2.13 G
    // cycles of a 16.2 G block on inputs that do essentially no work, and the packer then built chunks
    // around a cost that was not there — chunk 5 was predicted at 979,867,512 and measured 232,024,236.
    if let Some(ver) = spk.witness_version() {
        use bitcoin::blockdata::script::witness_version::WitnessVersion;
        let program_len = spk.len().saturating_sub(2);
        let spendable_by_signature = match ver {
            WitnessVersion::V0 => program_len == 20 || program_len == 32,
            WitnessVersion::V1 => program_len == 32,
            _ => false,
        };
        if !spendable_by_signature { return 0 }
    }

    if spk.is_p2tr() {
        // A trailing 0x50-prefixed annex is not part of the spend path.
        let mut n = wit.len();
        if n >= 2 && wit.last().is_some_and(|e| e.first() == Some(&0x50)) { n -= 1; }
        return match n {
            0 | 1 => 1, // key path (or malformed): one Schnorr verify
            _ => wit.iter().nth(n - 2).map_or(1, |leaf| tapscript_ops(leaf).max(1)),
        };
    }
    if spk.is_p2wpkh() { return 1; }
    if spk.is_p2wsh() {
        return wit.last().map_or(1, |ws| Script::from_bytes(ws).count_sigops().max(1) as u64);
    }
    if spk.is_p2sh() {
        // The redeem script is the last push of the scriptSig; a wrapped segwit spend then defers to the
        // witness exactly as the unwrapped form does.
        let redeem: Vec<u8> = txin
            .script_sig
            .instructions()
            .last()
            .and_then(|r| r.ok())
            .and_then(|ins| ins.push_bytes().map(|b| b.as_bytes().to_vec()))
            .unwrap_or_default();
        let rs = Script::from_bytes(&redeem);
        if rs.is_p2wpkh() { return 1; }
        if rs.is_p2wsh() {
            return wit.last().map_or(1, |ws| Script::from_bytes(ws).count_sigops().max(1) as u64);
        }
        return rs.count_sigops().max(1) as u64;
    }
    // Bare output: P2PK, P2PKH, bare multisig, or something unrecognised.
    spk.count_sigops().max(1) as u64
}

/// Predicted cost of every input of the block, in the order the block spends them.
fn input_costs(w: &BlockWitness) -> Vec<u64> {
    w.inputs
        .iter()
        .map(|inp| {
            let tx = &w.txs[inp.tx_idx as usize].0;
            let prevouts = &w.tx_prevouts[inp.tx_idx as usize].0;
            let ec = predicted_ec_ops(tx, inp.input_idx, prevouts);
            let bytes = (tx.len() + prevouts.len()) as u64;
            COST_INPUT_BASE + COST_PER_EC_OP * ec + COST_PER_INPUT_BYTE * bytes
        })
        .collect()
}

/// Runs needed to cover `costs` when no run may exceed `cap`. `cap` is always >= max(costs), so every
/// input fits somewhere and this terminates.
fn runs_at_cap(costs: &[u64], cap: u64) -> usize {
    let (mut runs, mut acc) = (1usize, 0u64);
    for &c in costs {
        if acc + c > cap { runs += 1; acc = c } else { acc += c }
    }
    runs
}

/// Split `costs` into contiguous runs, each within `cap`.
fn split_at_cap(costs: &[u64], cap: u64) -> Vec<(usize, usize)> {
    let (mut out, mut lo, mut acc) = (Vec::new(), 0usize, 0u64);
    for (i, &c) in costs.iter().enumerate() {
        if acc + c > cap && i > lo {
            out.push((lo, i));
            lo = i;
            acc = c;
        } else {
            acc += c;
        }
    }
    out.push((lo, costs.len()));
    out
}

/// Partition a block's inputs into at most `nchunks` contiguous runs, minimising the cost of the
/// heaviest run. Binary-searches the answer and checks feasibility greedily — exact, not a heuristic.
///
/// Guarantees, relied on by `prove_chunk`/`agg_chunks` and asserted in the tests: the runs are ordered,
/// non-empty, non-overlapping, and cover `0..n` exactly.
fn pack_chunks(costs: &[u64], nchunks: usize) -> Vec<(usize, usize)> {
    let n = costs.len();
    if n == 0 { return Vec::new() }
    let k = nchunks.max(1).min(n);
    if k == 1 { return vec![(0, n)] }

    let (mut lo, mut hi) = (costs.iter().copied().max().unwrap_or(1), costs.iter().sum::<u64>());
    while lo < hi {
        let mid = lo + (hi - lo) / 2;
        if runs_at_cap(costs, mid) <= k { hi = mid } else { lo = mid + 1 }
    }
    let mut runs = split_at_cap(costs, lo);

    // The optimal cap may need fewer than k runs. Idle provers help nobody, so keep splitting the widest
    // run until there are k of them — this can only lower the maximum, never raise it.
    while runs.len() < k {
        let Some(w) = (0..runs.len()).max_by_key(|&i| runs[i].1 - runs[i].0) else { break };
        let (a, b) = runs[w];
        if b - a < 2 { break } // nothing left wide enough to divide
        let mid = a + (b - a) / 2;
        runs[w] = (a, mid);
        runs.insert(w + 1, (mid, b));
    }
    runs
}

/// The block's chunk partition. Every command that touches chunks derives it from the witness through
/// this one function, so a fan-out across machines cannot disagree about which inputs a chunk holds.
///
/// `HAZYNC_CHUNK_PACK=count` restores the old equal-input-count split, for A/B measurement.
fn chunk_bounds(w: &BlockWitness, nchunks: usize) -> Vec<(usize, usize)> {
    let n = w.inputs.len();
    if std::env::var("HAZYNC_CHUNK_PACK").as_deref() == Ok("count") {
        if n == 0 { return Vec::new() }
        let k = nchunks.max(1).min(n);
        let sz = n.div_ceil(k);
        return (0..k).map(|c| (c * sz, ((c + 1) * sz).min(n))).filter(|(a, b)| a < b).collect();
    }
    pack_chunks(&input_costs(w), nchunks)
}

// `chunk-profile`: report how work is spread across a block's chunks, under both packing strategies.
// Prediction alone is free and needs no GPU; `HAZYNC_PROFILE_EXEC=1` additionally runs each chunk in
// execute mode for its real cycle count, which is the measurement #132 asks for before any GPU time is
// bought. Execute mode is slow but costs nothing but wall-clock.
fn chunk_profile() {
    let (_anchor, w) = build_full();
    let n = w.inputs.len();
    let nchunks = nchunks_env();
    let costs = input_costs(&w);
    let exec = std::env::var("HAZYNC_PROFILE_EXEC").is_ok();

    println!("=== CHUNK PROFILE block {}: {} inputs → {} chunks ===", w.height, n, nchunks);
    let total_ec: u64 = w.inputs.iter().map(|inp| predicted_ec_ops(
        &w.txs[inp.tx_idx as usize].0, inp.input_idx, &w.tx_prevouts[inp.tx_idx as usize].0)).sum();
    println!("predicted EC verifies: {} across {} inputs ({:.2} per input)",
        total_ec, n, total_ec as f64 / n.max(1) as f64);

    for (label, bounds) in [
        ("count-packed (old)", {
            let k = nchunks.max(1).min(n.max(1));
            let sz = n.div_ceil(k.max(1));
            (0..k).map(|c| (c * sz, ((c + 1) * sz).min(n))).filter(|(a, b)| a < b).collect::<Vec<_>>()
        }),
        ("cost-packed (new)", pack_chunks(&costs, nchunks)),
    ] {
        println!("\n--- {label}: {} chunks ---", bounds.len());
        let mut predicted: Vec<u64> = Vec::new();
        for (i, &(lo, hi)) in bounds.iter().enumerate() {
            let c: u64 = costs[lo..hi].iter().sum();
            predicted.push(c);
            let bytes: u64 = w.inputs[lo..hi].iter().map(|inp|
                (w.txs[inp.tx_idx as usize].0.len() + w.tx_prevouts[inp.tx_idx as usize].0.len()) as u64).sum();
            let ec: u64 = w.inputs[lo..hi].iter().map(|inp| predicted_ec_ops(
                &w.txs[inp.tx_idx as usize].0, inp.input_idx, &w.tx_prevouts[inp.tx_idx as usize].0)).sum();
            let cycles = if exec { Some(exec_chunk_cycles(&w, lo, hi)) } else { None };
            match cycles {
                Some(cy) => println!("  chunk {i:>3}  inputs {:>5}  ec {:>5}  bytes {:>9}  predicted {:>10}  cycles {:>14}", hi - lo, ec, bytes, c, cy),
                None     => println!("  chunk {i:>3}  inputs {:>5}  ec {:>5}  bytes {:>9}  predicted {:>10}", hi - lo, ec, bytes, c),
            }
        }
        // The straggler ratio is the number that matters: a block's wall-clock is its slowest chunk, so
        // this is the factor by which fan-out falls short of the work being evenly shared.
        let max = predicted.iter().copied().max().unwrap_or(0);
        let mean = predicted.iter().sum::<u64>() as f64 / predicted.len().max(1) as f64;
        println!("  straggler: max {} vs mean {:.0} = {:.2}x", max, mean, max as f64 / mean.max(1.0));
    }
}

/// Run one chunk's inputs through the guest in execute mode and return the cycles it took. Same mode 4
/// the prover uses, so the count is the real cost of proving that chunk, minus the proving itself.
fn exec_chunk_cycles(w: &BlockWitness, lo: usize, hi: usize) -> u64 {
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);
    let s = default_executor().execute(b.build().unwrap(), METHOD_ELF).expect("exec chunk");
    // Report what the chunk COMMITTED, not just how long it took. A payload-format change that
    // silently mis-reads its input would show up as fewer cycles and look like a win; the journal
    // digest is what makes an A/B trustworthy. #135.
    let d = <sha2::Sha256 as sha2::Digest>::digest(&s.journal.bytes);
    let j = &s.journal.bytes;
    let w = |i: usize| u32::from_le_bytes([j[i], j[i + 1], j[i + 2], j[i + 3]]);
    println!("        journal sha256 {}  kind={:#x} all_valid={} binds={}", hex(&d), w(0), w(4), w(8));
    s.cycles()
}
/// Write a chunk's inputs onto the guest's stdin (mode 4's payload), grouped by transaction.
///
/// #135. Two things are going on:
///
/// **Grouped by transaction.** A chunk used to carry a full copy of the spending transaction and its
/// prevouts for EVERY input, so a 501-input consolidation was shipped 501 times — block 741000 sent
/// 6,995,621 bytes of a distinct 123,883, a factor of 56.5. The guest then deserialised and ran
/// `PrecomputedTransactionData::Init` once per input, which is exactly the quadratic sighash work
/// BIP143 precomputation exists to avoid. Now each transaction goes once, followed by the indices of
/// the inputs this chunk owns.
///
/// **Raw bytes.** The blobs go through `write_slice` rather than serde `Vec<u8>`; serde walked risc0's
/// word stream a byte at a time at ~147 cycles/byte. Blobs are padded to a word so the u32 reads that
/// follow stay aligned, and the guest truncates back to the declared length.
///
/// Ordering is load-bearing. `w.inputs` is built transaction by transaction, inputs in index order, so
/// a contiguous chunk groups into consecutive transaction runs with no reordering. The aggregation
/// concatenates chunk binds and indexes them by the block's own input index — emit them in any other
/// order and it compares the wrong input, silently.
fn write_chunk_inputs(b: &mut risc0_zkvm::ExecutorEnvBuilder, w: &BlockWitness, lo: usize, hi: usize) {
    fn padded(v: &[u8]) -> Vec<u8> {
        let mut p = v.to_vec();
        p.resize(v.len().div_ceil(4) * 4, 0);
        p
    }
    // Consecutive runs sharing a tx_idx. Debug-asserted rather than assumed: if the witness builder
    // ever emits inputs out of order this must fail loudly here, not produce a chunk that binds the
    // wrong inputs.
    let mut groups: Vec<(u32, usize, usize)> = Vec::new();
    for i in lo..hi {
        let t = w.inputs[i].tx_idx;
        match groups.last_mut() {
            Some((gt, _, end)) if *gt == t => *end = i + 1,
            _ => groups.push((t, i, i + 1)),
        }
    }
    debug_assert!(
        w.inputs[lo..hi].windows(2).all(|p| (p[0].tx_idx, p[0].input_idx) < (p[1].tx_idx, p[1].input_idx)),
        "chunk inputs are not in (tx_idx, input_idx) order — grouping would reorder binds"
    );

    b.write(&(groups.len() as u32)).unwrap();
    for (tx_idx, gs, ge) in groups {
        let tx = &w.txs[tx_idx as usize].0;
        let prevouts = &w.tx_prevouts[tx_idx as usize].0;
        b.write(&(tx.len() as u32)).unwrap();
        b.write(&(prevouts.len() as u32)).unwrap();
        b.write(&((ge - gs) as u32)).unwrap();
        b.write_slice(&padded(tx));
        b.write_slice(&padded(prevouts));
        for inp in &w.inputs[gs..ge] {
            b.write(&inp.input_idx).unwrap();
            b.write(&inp.coin_height).unwrap();
            b.write(&inp.coin_is_coinbase).unwrap();
            b.write(&inp.coin_mtp).unwrap();
        }
    }
}

fn nchunks_env() -> usize {
    std::env::var("HAZYNC_CHUNKS").ok().and_then(|s| s.parse().ok()).unwrap_or(2).max(1)
}

// `prove-chunk <i>`: prove chunk i's scripts, write the receipt to chunk_<i>.bin (or $HAZYNC_OUT).
fn prove_chunk(idx: usize) {
    use std::time::Instant;
    let (_anchor, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).unwrap_or_else(|| panic!(
        "prove-chunk {idx}: block has only {} chunks at HAZYNC_CHUNKS={}", bounds.len(), nchunks_env()));
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap(); // block hash for flag exceptions
    write_chunk_inputs(&mut b, &w, lo, hi);
    let t = Instant::now();
    // EXECUTE FIRST, then prove (#145). This printed nothing until it finished, so while a chunk was
    // running its log was EMPTY and "wedged" was indistinguishable from "working". That is what hid
    // hazync#147 for 76 minutes and then 3h38m: the only thing that gave it away was nvidia-smi
    // showing 0% with the process alive, which is a thing you have to already suspect to go and look
    // at. A start line and periodic progress make a hang visible in the place anyone would look first.
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = risc0_zkvm::VerifierContext::default();
    let mut session = risc0_zkvm::ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF)
        .unwrap().run().unwrap();
    let nseg = session.segments.len();
    println!("chunk {idx}: {} inputs, {nseg} segments at po2 {} — proving", hi - lo, seg_po2());
    session.add_hook(FoldProgress {
        total: nseg,
        done: std::sync::atomic::AtomicUsize::new(0),
        t0: Instant::now(),
        every: (nseg / 20).max(1),
    });
    // SCALING: prove the chunk to a SUCCINCT receipt (not the default composite). This runs the
    // STARK-to-STARK "lift" NOW, in parallel across the chunk fleet — so agg-chunks resolves each
    // assumption cheaply instead of lifting all N composite receipts sequentially.
    let receipt = server.prove_session(&ctx, &session).unwrap().receipt;
    receipt.verify(METHOD_ID).unwrap();
    let out = std::env::var("HAZYNC_OUT").unwrap_or_else(|_| format!("chunk_{idx}.bin"));
    std::fs::write(&out, bincode::serialize(&receipt).unwrap()).unwrap();
    println!("chunk {idx} ({} inputs) proved in {:.0}s -> {out}", hi - lo, t.elapsed().as_secs_f64());
}

// `agg-chunks`: read all chunk receipt files, aggregate into the block/chain proof.
// Progress for a long prove (hazync#145). `agg-chunks` printed its header and then nothing at all
// for 35+ minutes, so a working fold and a hung one looked identical from the outside. That is not a
// cosmetic complaint: hazync#147 wedged twice, for 76 minutes and 3h38m, and what eventually gave it
// away was `nvidia-smi` showing 0% with the process alive -- not any output from the prover.
//
// risc0 fires this per segment. It costs nothing now: hooks used to force the sequential path away
// from the preflight pipelining, and that patch has been removed, so the sequential loop is the only
// path and a hook changes no behaviour at all.
struct FoldProgress {
    total: usize,
    done: std::sync::atomic::AtomicUsize,
    t0: std::time::Instant,
    every: usize,
}

impl risc0_zkvm::SessionEvents for FoldProgress {
    fn on_post_prove_segment(&self, _seg: &risc0_zkvm::Segment) {
        use std::sync::atomic::Ordering;
        let n = self.done.fetch_add(1, Ordering::Relaxed) + 1;
        if n % self.every != 0 && n != self.total { return; }
        let el = self.t0.elapsed().as_secs_f64();
        // Rate from work actually done, not from a guess. Early estimates are poor and say so by
        // being obviously early rather than by being hidden.
        let eta = if n > 0 { el / n as f64 * (self.total.saturating_sub(n)) as f64 } else { 0.0 };
        println!("    segment {n}/{}  {:.0}s elapsed, ~{:.0}s left", self.total, el, eta);
    }
}

// HAZYNC_AGG_EXECUTE=1 — execute mode 5 WITHOUT proving, and report its cycles.
//
// Settles which half of the aggregate is expensive. In execute mode `env::verify` merely RECORDS an
// assumption; RESOLVING it is recursion, and that cost lands in PROVING. So this cycle count is the
// block-validation work alone, and the gap between it and the measured prove wall-clock is what the
// sixteen assumption resolutions cost.
//
// It matters because the two pull in opposite directions: more chunks parallelise proving better
// and, if resolution dominates, make the aggregate worse. HAZYNC_CHUNKS has been treated as free.
// Host-side only; needs no GPU and does not move METHOD_ID.
//
// A FUNCTION RATHER THAN AN INLINE BLOCK, because check_seg_guards.sh requires a prove call within
// 25 lines of its segment_limit_po2 guard, and inline this pushed the aggregate's prove to 38 lines
// away. The guard was present and the check was right to complain: distance is what makes it
// readable at a glance that a prove path sets its segment size.
fn report_agg_execute_only(mut b: risc0_zkvm::ExecutorEnvBuilder) {
    use std::time::Instant;
    let t_x = Instant::now();
    let session = default_executor().execute(b.build().unwrap(), METHOD_ELF).expect("execute mode 5");
    let cycles = session.cycles();
    println!("=== AGGREGATE, EXECUTE ONLY (no proving) ===");
    println!("  block validation cycles  {cycles}");
    println!("  segments at po2 {}        ~{}", seg_po2(), cycles.div_ceil(1u64 << seg_po2()));
    println!("  executed in              {:.1}s", t_x.elapsed().as_secs_f64());
    println!();
    println!("  Scale: chunk 9 is 948,436,992 cycles and proved in ~915 s on a B200, so this much");
    println!("  block-validation work is roughly {:.0} s of segment proving. Whatever the FULL",
             cycles as f64 / 948_436_992.0 * 915.0);
    println!("  aggregate cost beyond that (measured: >3,300 s) is the assumption resolutions.");
}

fn agg_chunks() {
    use std::time::Instant;
    let (anchor, w) = build_full();
    // The partition decides how many chunk files exist — an uneven pack can yield fewer runs than
    // HAZYNC_CHUNKS asked for, and reading a fixed count would look for a file that was never written.
    let nchunks = chunk_bounds(&w, nchunks_env()).len();
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
    // Execute-only mode returns before proving; see report_agg_execute_only.
    if std::env::var("HAZYNC_AGG_EXECUTE").is_ok() { report_agg_execute_only(b); return; }

    // Prove the aggregate to SUCCINCT too: the assumptions are already succinct (cheap resolve), and a
    // succinct block proof is a single fixed-size STARK — directly composable in the chain range-fold.
    //
    // EXECUTE FIRST, then prove, rather than one opaque prove_with_opts (#145). Execution is a
    // couple of percent of the work and it yields the segment count, so the fold can say how much
    // there is to do BEFORE it starts doing it. Without that the only honest thing anyone could say
    // about a running fold was "it has not finished".
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = risc0_zkvm::VerifierContext::default();
    let mut session = risc0_zkvm::ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF)
        .unwrap().run().unwrap();
    let nseg = session.segments.len();
    println!("  {nseg} segments to prove at po2 {}", seg_po2());
    session.add_hook(FoldProgress {
        total: nseg,
        done: std::sync::atomic::AtomicUsize::new(0),
        t0: Instant::now(),
        every: (nseg / 20).max(1),      // ~20 lines whatever the size, so it scales with the fold
    });
    let agg = server.prove_session(&ctx, &session).unwrap().receipt;
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
    BlockWitness { header, height: SYNTH_H, coinbase_tx: serialize(cb), txids, root_prev, txs: wtxs, tx_prevouts: wtx_prevs, inputs, root_next, bip30: None, in_smt_root, smt }
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
        root_prev, txs: vec![PackedBytes(t_raw.clone())], tx_prevouts: vec![PackedBytes(shared_blob)],
        inputs: vec![in0, in1], root_next: wire_stump(&forest), bip30: None,
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

    // AUDIT #3 F-1. This used to build a transition against a FRESH, EMPTY tree — where absence proves
    // trivially, which is the one state these blocks are never in. Their duplicated coinbase is
    // present and UNSPENT (that is why the utreexo overwrite below has leaves to delete at all), so
    // the fixture now models exactly that: seed the tree with the duplicate, then take the
    // grandfathered overwrite path. Driving the real block from an empty tree is what let the SMT's
    // reject-valid behaviour on 91842/91880 sit green through a whole test suite.
    let f3_smt = {
        let cb_out = cb_spendable_outputs(&coinbase);
        let mut t = Smt::new();
        t.insert(cb_txid, cb_out.max(1));
        let root_in = t.root();
        (root_in, smt_advance(&mut t, cb_txid, cb_out, &[], true))
    };
    let mk = |bip30: Option<Bip30Overwrite>| BlockWitness {
        header: header.clone(), height, coinbase_tx: hx(cb_hex), txids: vec![cb_txid],
        root_prev: root_prev.clone(), txs: vec![], tx_prevouts: vec![], inputs: vec![], root_next: root_next.clone(), bip30,
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
    /// `Some(prior_count)` ONLY at the two blocks BIP30 grandfathers (91842, 91880), where the
    /// duplicated coinbase was still UNSPENT so no absence proof can exist. LAST, because risc0's
    /// serde is positional and appending is the only change that does not renumber every field.
    smt_overwrite: Option<u32>,
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
    let w = smt_advance(&mut t, coinbase_txid, coinbase_outputs, coinbase_spends, false);
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
    grandfathered: bool,
) -> SmtBlockWitness {
    // 1. Normally: absence, against the INCOMING root, before any of this block's own updates.
    //
    //    At 91842/91880 (audit#3 F-1) the duplicated coinbase is still UNSPENT — that is why BIP30
    //    exists and why Core exempts these two heights — so absence is unprovable and the witness
    //    carries a MEMBERSHIP proof of the prior count instead. The guest gates this on the block
    //    HASH it derives, so a spurious height match here is still rejected there.
    let absence_proof = smt.prove(&coinbase_txid);
    let smt_overwrite = if grandfathered {
        let prior = smt.get(&coinbase_txid).unwrap_or_else(|| panic!(
            "bridge: grandfathered BIP30 block duplicates coinbase {} which the SMT does not hold — \
             the tree and the chain have diverged",
            coinbase_txid.iter().map(|b| format!("{b:02x}")).collect::<String>()));
        Some(prior)
    } else {
        None
    };
    if coinbase_outputs > 0 { smt.insert(coinbase_txid, coinbase_outputs); }
    else if grandfathered { smt.remove(&coinbase_txid); }

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
    SmtBlockWitness { coinbase_txid, coinbase_outputs, absence_proof, spends, smt_overwrite }
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
        txids: vec![[7u8; 32], [8u8; 32]],
        root_prev: WireStump { roots: vec![], num_leaves: 0 },
        txs: vec![PackedBytes(raw_tx.clone())], tx_prevouts: vec![PackedBytes(prevouts.clone())],
        inputs: vec![],
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

/// Magic + version for the intermediate UTXO dump consumed by ghostd (see `cmd_dump_snapshot`).
const HZUTXO_MAGIC: &[u8; 8] = b"HZUTXO\0\0";
const HZUTXO_VERSION: u32 = 1;

/// Emit the bridge's UTXO set as an UNCOMPRESSED intermediate for ghostd to turn into a chainstate.
///
/// This is the emitter half of "proven assumeutxo": instead of trusting a developer-chosen snapshot
/// hash, ghostd rebuilds the accumulator from what this writes and checks its roots against the ones
/// a Hazync proof commits to.
///
/// WHY AN INTERMEDIATE RATHER THAN CORE'S SNAPSHOT FORMAT. Core serialises `Coin` COMPRESSED (varint
/// `height*2+coinbase`, `CTxOutCompressor` amount/script compression). Reproducing that byte-exactly
/// here would be a second implementation of a Core format living in a different language — the same
/// duplication the project refuses in the other direction, where ghostd delegates proof verification
/// to Rust rather than reimplementing risc0 in C++. So this writes plain fields and ghostd builds the
/// chainstate with Core's own serialisers.
///
/// WHAT IS DELIBERATELY ABSENT: the accumulator roots. A consumer must take those from the PROOF, not
/// from the file it is checking — writing them here would invite verifying the dump against itself.
///
/// `position` is the coin's index in the accumulator. It cannot be derived from the coin data: the
/// forest deletes by swap-and-shrink, so its layout is a function of the whole add/delete history,
/// not of the surviving set. Core's snapshot is txid-grouped and so cannot carry it implicitly either.
fn cmd_dump_snapshot(out_path: &str) {
    let dir = std::env::var("HAZYNC_BRIDGE_OUT").unwrap_or_else(|_| "/root/bridge_bundles".into());
    let st = bridge_load_state(&dir).unwrap_or_else(|| panic!("no bridge checkpoint at {dir}/state.bin"));

    // The forest holds one leaf per live coin. If these ever disagree the dump would be meaningless,
    // and silently so — a missing coin still produces a well-formed file that simply fails the root
    // check later, with nothing pointing at the cause.
    assert_eq!(st.utxo.len(), st.leaves.len(),
        "bridge checkpoint disagrees with itself: {} utxos vs {} accumulator leaves",
        st.utxo.len(), st.leaves.len());

    // Invert position->leaf into leaf->position. Leaves are coin-unique (an outpoint occurs once), so
    // a collision here means the checkpoint is corrupt rather than merely surprising.
    let mut pos_of: std::collections::HashMap<[u8; 32], u32> =
        std::collections::HashMap::with_capacity(st.leaves.len());
    for (i, leaf) in st.leaves.iter().enumerate() {
        if pos_of.insert(*leaf, i as u32).is_some() {
            panic!("duplicate accumulator leaf at position {i} — corrupt checkpoint");
        }
    }

    // Deterministic order: a dump that reordered between runs could not be diffed or reproduced.
    let mut keys: Vec<&([u8; 32], u32)> = st.utxo.keys().collect();
    keys.sort_unstable();

    let mut body: Vec<u8> = Vec::new();
    for k in &keys {
        let (value, spk, height, is_coinbase) = &st.utxo[*k];
        // Same convention the prover uses: block_mtp[h] == MTP(h-1), and a coin's mtp is that of the
        // block that created it. Indexing past the window means the checkpoint cannot describe its
        // own coins, which is a bug rather than a coin to skip.
        let coin_mtp = *st.block_mtp.get(*height as usize).unwrap_or_else(||
            panic!("no block_mtp for coin height {height} (window len {})", st.block_mtp.len()));
        let leaf = coin_leaf(&k.0, k.1, *value, spk, *height, *is_coinbase, coin_mtp);
        let pos = *pos_of.get(&leaf).unwrap_or_else(||
            panic!("coin {}:{} is not in the accumulator — checkpoint utxo/leaves disagree",
                k.0.iter().map(|b| format!("{b:02x}")).collect::<String>(), k.1));

        body.extend_from_slice(&k.0);
        body.extend_from_slice(&k.1.to_le_bytes());
        body.extend_from_slice(&value.to_le_bytes());
        body.extend_from_slice(&height.to_le_bytes());
        body.push(*is_coinbase as u8);
        body.extend_from_slice(&coin_mtp.to_le_bytes());
        body.extend_from_slice(&pos.to_le_bytes());
        body.extend_from_slice(&(spk.len() as u32).to_le_bytes());
        body.extend_from_slice(spk);
    }

    let mut out: Vec<u8> = Vec::with_capacity(body.len() + 32);
    out.extend_from_slice(HZUTXO_MAGIC);
    out.extend_from_slice(&HZUTXO_VERSION.to_le_bytes());
    out.extend_from_slice(&st.height.to_le_bytes());
    out.extend_from_slice(&(keys.len() as u64).to_le_bytes());
    out.extend_from_slice(&body);

    let tmp = format!("{out_path}.tmp");
    std::fs::write(&tmp, &out).expect("write utxo dump");
    std::fs::rename(&tmp, out_path).expect("commit utxo dump"); // atomic: never a torn dump
    println!("dump-snapshot: height {}, {} coins -> {} ({} bytes)",
        st.height, keys.len(), out_path, out.len());
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
                    smt_advance(&mut smt, cb, nout, &cb_spends, h == 91842 || h == 91880)
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

    // A prover memory failure surfaces as a raw unwrap deep inside the GPU HAL:
    //
    //   panicked at risc0-zkp/src/hal/cuda.rs:246: called `Result::unwrap()` on an `Err` value:
    //   allocation failed on evaluated: 134217728 bytes  Caused by: "out of memory"
    //
    // naming neither the knob that fixes it nor the fact that a smaller segment would just work. On a
    // CUDA 13 driver the default po2 21 fails on an OTHERWISE IDLE L40S with 45 GB free (#97), so this
    // is the first thing a contributor on UpCloud's current GPU image meets. `coordinator/hazync` walks
    // a retry ladder and absorbs it; a direct `host prove-block` gets the bare panic, which is what
    // RELEASE_PLAN.md §1.4 says must not happen — an OOM has to name HAZYNC_SEG_PO2 and a value to try.
    //
    // The hook ADDS the remedy and leaves the original panic intact. The underlying message is still
    // the most accurate description of what happened, and replacing it would trade one unhelpful
    // failure for another.
    let prior = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        prior(info);
        let msg = info.payload().downcast_ref::<String>().map(|s| s.as_str())
            .or_else(|| info.payload().downcast_ref::<&str>().copied())
            .unwrap_or("");
        if msg.contains("out of memory") || msg.contains("allocation failed") {
            let po2 = seg_po2();
            eprintln!("\nhazync: that is a prover memory failure, not a bad block.");
            eprintln!("  The usual cause is TWO proves sharing one card, not a segment that is too big.");
            eprintln!("  Measured on an L40S (46 GB): one prove at HAZYNC_SEG_PO2=21 peaks near 22 GB, so");
            eprintln!("  a single prove fits easily and two do not (#97). `hazync run` serialises GPU work");
            eprintln!("  through a lock; a direct `host prove-*` does not, so it can land on top of one.");
            eprintln!("  Look for another prove first:");
            eprintln!("      nvidia-smi --query-compute-apps=pid,used_memory --format=csv");
            eprintln!("  If the card really is yours alone, drop a rung — each step down roughly halves");
            eprintln!("  the working memory one segment needs, at a few seconds' cost per prove:");
            eprintln!("      HAZYNC_SEG_PO2={} <the same command again>", po2.saturating_sub(1));
        }
    }));

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
    if let Some(p) = args.iter().position(|a| a == "dump-snapshot") {
        let out = args.get(p + 1).expect("dump-snapshot <out-file>");
        cmd_dump_snapshot(out);
        return;
    }
    if args.iter().any(|a| a == "segment-size") {
        segment_size_cmd();
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "receipt-digest") {
        let f = args.get(p + 1).expect("receipt-digest <file>");
        receipt_digest_cmd(f);
        return;
    }
    if args.iter().any(|a| a == "seg-coordinate-tree") {
        seg_coordinate_tree_cmd();
        return;
    }
    if args.iter().any(|a| a == "seg-serve") {
        seg_serve_cmd();
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "seg-connect") {
        let addr = args.get(p + 1).expect("seg-connect <host:port>");
        seg_connect_cmd(addr);
        return;
    }
    if args.iter().any(|a| a == "seg-prove-one") {
        seg_prove_one_cmd();
        return;
    }
    if args.iter().any(|a| a == "seg-join") {
        seg_join_cmd();
        return;
    }
    if args.iter().any(|a| a == "seg-work") {
        seg_work_cmd();
        return;
    }
    if args.iter().any(|a| a == "seg-coordinate") {
        seg_coordinate_cmd();
        return;
    }
    if args.iter().any(|a| a == "seg-distribute") {
        seg_distribute_cmd();
        return;
    }
    if args.iter().any(|a| a == "segment-mem") {
        segment_mem_cmd();
        return;
    }
    if args.iter().any(|a| a == "exec-time") {
        exec_time_cmd();
        return;
    }
    if args.iter().any(|a| a == "vb-stages") {
        vb_stages_cmd();
        return;
    }
    if args.iter().any(|a| a == "chunk-profile") {
        chunk_profile();
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
            overwrite: w.smt_overwrite,
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

            let w = smt_advance(&mut bridge, cb, nout, &spends, false);
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
        let w0 = smt_advance(&mut bridge, cb, 1, &[], false);
        let root0 = apply_block(&Smt::new().root(), &to_update(cb, 1, &w0)).unwrap();

        // Now try the same coinbase again while it is still unspent.
        let mut replay = bridge.clone();
        let w1 = smt_advance(&mut replay, cb, 1, &[], false);
        assert!(apply_block(&root0, &to_update(cb, 1, &w1)).is_err(),
                "a duplicate of an UNSPENT coinbase was accepted");
    }

    /// AUDIT #3 F-1 — the REAL shape of 91842/91880, against a RESIDENT tree.
    ///
    /// The fixture that stood here drove the guest from a fresh, empty `Smt`, where absence proves
    /// trivially. That is the one state the real blocks are not in: their duplicated coinbases were
    /// overwritten while still UNSPENT, which is the entire reason BIP30 exists. Because the fixture
    /// never reproduced the precondition, the whole suite stayed green while a from-genesis prover
    /// would have stalled at 91841.
    #[test]
    fn the_grandfathered_overwrite_round_trips_against_a_resident_tree() {
        let dup = txid(91812);
        let mut bridge = Smt::new();
        bridge.insert(dup, 1);                       // present and UNSPENT — the real precondition
        let root = bridge.root();

        let w = smt_advance(&mut bridge, dup, 1, &[], true);
        assert_eq!(w.smt_overwrite, Some(1), "the bridge did not emit an overwrite claim");

        let out = apply_block(&root, &to_update(dup, 1, &w))
            .expect("the guest rejected real block 91842 — a from-genesis prover stalls at 91841");
        assert_eq!(out, root, "a byte-identical overwrite must leave the root unchanged");
        assert_eq!(out, bridge.root(), "bridge and guest diverged across the overwrite");
    }

    /// The exception must not be reachable on an ordinary block: without the grandfather flag the same
    /// state is a plain BIP30 violation and must be refused.
    #[test]
    fn the_same_state_without_the_grandfather_is_still_refused() {
        let dup = txid(91812);
        let mut bridge = Smt::new();
        bridge.insert(dup, 1);
        let root = bridge.root();
        let w = smt_advance(&mut bridge.clone(), dup, 1, &[], false);
        assert!(apply_block(&root, &to_update(dup, 1, &w)).is_err(),
                "a duplicate of an UNSPENT coinbase was accepted outside the two grandfathered blocks");
    }

    /// The two historical duplicates, in shape: once the earlier coinbase is fully spent, the later
    /// one is an ordinary insert. This is what retires the F3 grandfathered-overwrite special case.
    #[test]
    fn a_fully_spent_coinbase_can_be_duplicated_with_no_special_case() {
        let mut bridge = Smt::new();
        let dup = txid(91812);
        let w0 = smt_advance(&mut bridge, dup, 1, &[], false);
        let mut root = apply_block(&Smt::new().root(), &to_update(dup, 1, &w0)).unwrap();

        // Spend it to zero via an ordinary later block.
        let w1 = smt_advance(&mut bridge, txid(999), 1, &[dup], false);
        root = apply_block(&root, &to_update(txid(999), 1, &w1)).unwrap();

        // Now the duplicate is legal.
        let w2 = smt_advance(&mut bridge, dup, 1, &[], false);
        root = apply_block(&root, &to_update(dup, 1, &w2))
            .expect("duplicating a fully-spent coinbase was rejected — this is legal under BIP30");
        assert_eq!(root, bridge.root());
    }
}

#[cfg(test)]
mod chunk_packing_tests {
    use super::{pack_chunks, runs_at_cap};

    /// The invariants `prove_chunk` and `agg_chunks` rely on. If any of these break, chunks stop lining
    /// up with the block's inputs and the guest's `all_binds[idx]` lookup silently compares the wrong
    /// input — so these are correctness properties, not tidiness.
    fn assert_partitions(costs: &[u64], runs: &[(usize, usize)]) {
        assert!(!runs.is_empty() || costs.is_empty(), "no runs for a non-empty block");
        let mut expect = 0usize;
        for &(lo, hi) in runs {
            assert_eq!(lo, expect, "runs must be contiguous and in order: {runs:?}");
            assert!(lo < hi, "runs must be non-empty: {runs:?}");
            expect = hi;
        }
        assert_eq!(expect, costs.len(), "runs must cover every input exactly once: {runs:?}");
    }

    fn max_run(costs: &[u64], runs: &[(usize, usize)]) -> u64 {
        runs.iter().map(|&(lo, hi)| costs[lo..hi].iter().sum::<u64>()).max().unwrap_or(0)
    }

    #[test]
    fn partitions_hold_for_any_shape() {
        let shapes: Vec<Vec<u64>> = vec![
            vec![],
            vec![1_950_000],
            vec![1_950_000; 16],
            vec![1_950_000, 1_950_000, 29_250_000, 1_950_000, 1_950_000],          // one fat multisig input
            vec![29_250_000, 1_950_000, 1_950_000, 1_950_000, 1_950_000],          // fat input first
            vec![1_950_000, 1_950_000, 1_950_000, 1_950_000, 29_250_000],          // fat input last
            (0..100).map(|i| if i % 17 == 0 { 39_000_000 } else { 1_950_000 }).collect(),
        ];
        for costs in &shapes {
            for k in [1usize, 2, 3, 8, 16, 64] {
                assert_partitions(costs, &pack_chunks(costs, k));
            }
        }
    }

    #[test]
    fn never_returns_more_runs_than_asked_for() {
        let costs: Vec<u64> = (0..50).map(|i| 1_950_000 + i * 13_000).collect();
        for k in [1usize, 2, 7, 50, 500] {
            let runs = pack_chunks(&costs, k);
            assert!(runs.len() <= k.max(1), "asked for {k}, got {}", runs.len());
            assert!(runs.len() <= costs.len(), "more runs than inputs");
        }
    }

    #[test]
    fn uses_every_chunk_when_there_is_work_to_fill_it() {
        // An optimal cap can be reached with fewer runs than requested. Leaving provers idle is a real
        // cost, so the packer keeps dividing.
        let costs = vec![1_950_000u64; 32];
        assert_eq!(pack_chunks(&costs, 8).len(), 8);
        assert_eq!(pack_chunks(&costs, 32).len(), 32);
    }

    #[test]
    fn beats_equal_input_counts_on_a_skewed_block() {
        // Two 15-of-15 multisig inputs among singles, all landing in the same count-packed chunk.
        let mut costs = vec![1_950_000u64; 64];
        costs[4] = 1_950_000 * 15;
        costs[5] = 1_950_000 * 15;
        let k = 8;

        let by_count: Vec<(usize, usize)> =
            (0..k).map(|c| (c * 8, (c + 1) * 8)).collect();
        let by_cost = pack_chunks(&costs, k);
        assert_partitions(&costs, &by_cost);

        assert!(max_run(&costs, &by_cost) < max_run(&costs, &by_count),
            "cost packing did not lower the straggler: {} vs {}",
            max_run(&costs, &by_cost), max_run(&costs, &by_count));
    }

    #[test]
    fn minimises_the_heaviest_run() {
        // The binary search should land on a cap no smaller than achievable: one fewer must need more
        // runs than we are allowed.
        let costs: Vec<u64> = (0..40).map(|i| 100 + (i * 37) % 900).collect();
        let k = 6;
        let runs = pack_chunks(&costs, k);
        let cap = max_run(&costs, &runs);
        assert!(runs.len() <= k);
        assert!(cap >= *costs.iter().max().unwrap(), "a run cannot be lighter than its heaviest input");
        assert!(runs_at_cap(&costs, cap - 1) > k,
            "cap {cap} was not minimal — {} runs fit under {}", runs_at_cap(&costs, cap - 1), cap - 1);
    }

    /// Locks in the byte term. Block 741000's chunk 1 and chunk 11 hold the same number of inputs
    /// performing the same number of EC verifies, and chunk 1 still costs more because its
    /// transactions are 20x bigger. An EC-only model rates the two equal and packs them as though
    /// they were.
    ///
    /// The margin shrank when the guest stopped reading its payload through serde (#135): bytes were
    /// 63% of a fat chunk at 182 cycles each, and are ~25% at 36. The term is smaller, not gone —
    /// dropping it costs roughly 1.3x on the straggler here, and more on blocks with fatter
    /// transactions. Measured: chunk 1 109,113,751 cycles against chunk 11 83,264,971.
    #[test]
    fn marshalling_bytes_are_costed_not_just_signatures() {
        let real = |ec: u64, bytes: u64| super::COST_PER_EC_OP * ec + super::COST_PER_INPUT_BYTE * bytes;
        let (lean, fat) = (real(42, 37_933), real(42, 765_282));

        // Within 2% of what those two chunks actually measured — the coefficients are fitted, so this
        // fails loudly if either is changed without re-measuring. Values are post-dedup: chunk 1 fell
        // 221,538,730 -> 109,113,751 -> 86,495,630 across the two halves of #135.
        assert!((fat as f64 - 86_495_630.0).abs() / 86_495_630.0 < 0.02, "fat chunk: {fat}");
        assert!((lean as f64 - 82_090_232.0).abs() / 82_090_232.0 < 0.02, "lean chunk: {lean}");
        assert!(fat > lean, "an EC-only model cannot tell these apart: {fat} vs {lean}");

        // And the packer must act on it. Assert the PROPERTY, not a fixed partition: which split wins
        // depends on the coefficients, and an earlier version of this test hard-coded the answer that
        // was optimal at 182 cycles/byte and broke when the byte term was refitted.
        let costs = vec![lean, lean, fat, fat];
        let runs = pack_chunks(&costs, 2);
        assert_partitions(&costs, &runs);
        let best = (1..costs.len())
            .map(|split| {
                let l: u64 = costs[..split].iter().sum();
                let r: u64 = costs[split..].iter().sum();
                l.max(r)
            })
            .min()
            .unwrap();
        assert_eq!(max_run(&costs, &runs), best, "not the optimal 2-way split: {runs:?}");
    }

    #[test]
    fn single_input_and_single_chunk_degenerate_cleanly() {
        assert_eq!(pack_chunks(&[1_950_000], 8), vec![(0, 1)]);
        assert_eq!(pack_chunks(&[1_950_000; 5], 1), vec![(0, 5)]);
        assert!(pack_chunks(&[], 8).is_empty());
    }
}

/// `vb-stages` — hazync: cost each phase of the aggregate's `validate_block` by subtraction.
///
/// The aggregate is 3,636,355,430 cycles on block 962,000 — 3.8x a single chunk, 21% of the block's
/// total work, and SERIAL where chunks are parallel. Optimising it without knowing which phase it is
/// would be guessing, and this codebase has a habit of punishing that.
///
/// Runs the AGGREGATION path (scripts not re-verified), so the numbers describe mode 5 and not mode 1.
/// Execute mode: no GPU, no chunk receipts.
fn vb_stages_cmd() {
    let (_anchor, w) = build_full();
    let stages: &[(u32, &str)] = &[
        (0, "read witness + header/version"),
        (1, "+ per-tx output leaves (tx_out_leaves)"),
        (2, "+ created_at in-block-coin map"),
        (3, "+ input loop: binds & per-tx checks (NO utreexo delete)"),
        (4, "+ utreexo deletes"),
        (5, "+ utreexo adds + root compare"),
        (6, "+ merkle root"),
        (u32::MAX, "+ wtxids & witness commitment (FULL)"),
        // Inside the input loop, which is 73% of the total. Each `continue`s after one more call, so
        // the deltas isolate the per-input work that the phase ladder above could only report in bulk.
        (20, "  [loop] bare iteration (resolve tx/prevouts only)"),
        (21, "  [loop] + coin_leaf_only + bind digest"),
        (22, "  [loop] + per-TX check_tx & is_final_tx"),
        (23, "  [loop] + check_input_locks"),
        (24, "  [loop] + created_at lookup & in-block bookkeeping"),
        (3,  "  [loop] + utreexo proof build (NO delete)"),
        (4,  "  [loop] + utreexo delete"),
        // Not a phase: asserts the batched leaves and locks equal the per-input ones for EVERY input.
        // It costs more than the full run (it does both), which is the point — correctness, not speed.
        (30, "DIFFERENTIAL: batch vs per-input, every input"),
        (31, "DIFFERENTIAL: chunk-supplied leaves/seqs/wtxids vs recomputed"),
    ];
    println!("=== validate_block phase costs — block {} ===", w.height);
    println!("{:<52} {:>16} {:>16}", "phase", "cumulative", "this phase");
    let mut prev = 0u64;
    for (stage, label) in stages {
        let mut b = ExecutorEnv::builder();
        b.segment_limit_po2(seg_po2());
        b.write(&12u32).unwrap();
        b.write(&stage).unwrap();
        b.write(&w).unwrap();
        let s = default_executor().execute(b.build().unwrap(), METHOD_ELF).expect("execute mode 12");
        let c = s.cycles();
        let delta = c.saturating_sub(prev);
        println!("{label:<52} {c:>16} {delta:>16}");
        prev = c;
    }
    println!();
    println!("The largest 'this phase' is where the aggregate's time goes. Anything that is per-TX");
    println!("and pure is a parallelisation candidate; utreexo is sequential accumulator state.");
}

/// `segment-size` — how big is one segment on the wire?
///
/// Decides whether segment-level distribution is a LAN-only architecture or something ordinary nodes
/// could join. A worker proving a segment needs that segment; if it is hundreds of MB the network is
/// the bottleneck rather than the GPU, and the idea is a rack, not a network.
fn segment_size_cmd() {
    use risc0_zkvm::ExecutorImpl;
    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(9);
    let (_a, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);
    let session = ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF).unwrap().run().unwrap();
    let n = session.segments.len();
    let mut sizes: Vec<usize> = Vec::new();
    for r in session.segments.iter() {
        let seg = r.resolve().expect("resolve");
        sizes.push(bincode::serialize(&seg).expect("serialize").len());
    }
    sizes.sort_unstable();
    let tot: usize = sizes.iter().sum();
    println!("=== segment wire size — block {} chunk {} at po2 {} ===", w.height, idx, seg_po2());
    println!("  segments          {n}");
    println!("  total             {:.1} MB", tot as f64 / 1e6);
    println!("  mean              {:.2} MB", tot as f64 / n as f64 / 1e6);
    println!("  min / median / max  {:.2} / {:.2} / {:.2} MB",
        sizes[0] as f64 / 1e6, sizes[n / 2] as f64 / 1e6, sizes[n - 1] as f64 / 1e6);
    println!();
    println!("  A whole block is ~16x this. To distribute segment proving, each worker must receive");
    println!("  its segment: that is the mean above per segment proved, against ~4.2 s of GPU work.");
    let mean_mb = tot as f64 / n as f64 / 1e6;
    // MB -> Mbit is x8, then divide by the seconds of work it buys. (An earlier version of this line
    // multiplied by a further 1000 and printed a figure 1000x too large.)
    println!("  Break-even at 4.2 s/segment: {:.2} Mbit/s keeps one worker saturated.",
        mean_mb * 8.0 / 4.2);
    println!("  So the constraint on distributing segments is not bandwidth. It is the prover's");
    println!("  working set per segment, which this does not measure.");
}

// How much of a chunk's wall clock is EXECUTION rather than PROVING?
//
// This decides whether distributing segment proving can work at all. Segments are proved
// independently — that is the whole idea — but they are PRODUCED by one sequential executor pass.
// That pass is a serial floor no number of workers can cross: with P proving seconds spread over N
// cards, a chunk cannot finish faster than E + P/N.
//
// Measured here on the CPU that runs the harness, which is the conservative direction: this laptop is
// slower than the prover box, so the execution share reported is an UPPER bound on the real one.
fn exec_time_cmd() {
    use risc0_zkvm::ExecutorImpl;
    use std::time::Instant;
    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(9);

    let t0 = Instant::now();
    let (_a, w) = build_full();
    let t_build = t0.elapsed().as_secs_f64();

    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);
    let env = b.build().unwrap();

    let t1 = Instant::now();
    let session = ExecutorImpl::from_elf(env, METHOD_ELF).unwrap().run().unwrap();
    let t_exec = t1.elapsed().as_secs_f64();

    let n = session.segments.len();
    let po2 = seg_po2();
    let cyc_cap = (n as f64) * (1u64 << po2) as f64;

    // The one calibration point we have: chunk 9 of this block proved in 915 s on a B200
    // (948,436,992 cycles -> 1.036 M cycles/s). Anyone re-running on other hardware should
    // override it rather than trust the default.
    let prove_s: f64 = std::env::var("HAZYNC_PROVE_S").ok().and_then(|s| s.parse().ok()).unwrap_or(915.0);

    println!("=== execute vs prove — block {} chunk {} at po2 {} ===", w.height, idx, po2);
    println!("  witness build     {:.1} s", t_build);
    println!("  EXECUTION         {:.1} s   ({} segments, <= {:.0} M cycles)", t_exec, n, cyc_cap / 1e6);
    println!("  proving (B200)    {:.1} s   [override with HAZYNC_PROVE_S]", prove_s);
    println!();
    println!("  execution share of a 1-card chunk: {:.1}%", 100.0 * t_exec / (t_exec + prove_s));
    println!();
    println!("  Serial floor with segment proving spread over N cards (E + P/N):");
    for n_cards in [1usize, 4, 16, 30, 64, 1000] {
        let t = t_exec + prove_s / n_cards as f64;
        println!("    N={:<5} {:7.1} s   (speedup {:.1}x, ceiling {:.1}x)",
            n_cards, t, (t_exec + prove_s) / t, (t_exec + prove_s) / t_exec);
    }
    println!();
    println!("  The ceiling column is what execution alone caps the chunk at. If that number is");
    println!("  small, distributing segments cannot reach 10 minutes no matter how many cards join,");
    println!("  and the executor has to be parallelised or split before anything else matters.");
}

// Peak memory to prove ONE segment — the number that decides who can be a worker.
//
// §29 settled bandwidth for distributing segment proving (0.28 MB mean, 0.53 Mbit/s keeps a worker
// saturated) and §31 settled the serial floor (execution is 2.2%). The remaining unknown is the
// prover's WORKING SET per segment. It decides two different things:
//
//   CPU  — whether an ordinary Bitcoin Ghost node can take a segment. Nodes run in 3.87 GB.
//   CUDA — how many proves share one card, which is the #97 OOM in a different disguise.
//
// Reports VmHWM, the kernel's peak-RSS high-water mark. It is monotonic across the process, so the
// FIRST prove's increment is the real per-segment cost and later ones mostly reuse allocations; both
// are printed so that is visible rather than assumed. VRAM is not visible from inside the process —
// sample `nvidia-smi` alongside it.
fn segment_mem_cmd() {
    use risc0_zkvm::{ExecutorImpl, VerifierContext, get_prover_server};
    use std::time::Instant;

    fn kb(field: &str) -> f64 {
        std::fs::read_to_string("/proc/self/status").ok()
            .and_then(|s| s.lines().find(|l| l.starts_with(field))
                .and_then(|l| l.split_whitespace().nth(1).and_then(|v| v.parse::<f64>().ok())))
            .unwrap_or(0.0) / 1e6  // kB -> GB
    }

    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(9);
    let nseg: usize = std::env::var("HAZYNC_NSEG").ok().and_then(|s| s.parse().ok()).unwrap_or(3);

    let (_a, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);

    let t_exec = Instant::now();
    let session = ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF).unwrap().run().unwrap();
    let exec_s = t_exec.elapsed().as_secs_f64();
    let total = session.segments.len();

    let backend = std::env::var("RISC0_PROVER").unwrap_or_else(|_| "local".into());
    println!("=== per-segment prover working set — block {} chunk {} po2 {} ===", w.height, idx, seg_po2());
    println!("  backend {backend}   segments {total}   execution {exec_s:.1} s");
    println!("  after execute, before any prove:  VmHWM {:.2} GB   VmRSS {:.2} GB", kb("VmHWM:"), kb("VmRSS:"));
    println!();

    let opts = ProverOpts::default();
    let server = get_prover_server(&opts).expect("prover server");
    let ctx = VerifierContext::default();

    // Sample across the chunk rather than the first N: segment cost is not uniform, and the first
    // segment of a session is the least representative one.
    println!("  {:>4}  {:>8}  {:>10}  {:>10}  {:>10}", "seg", "prove s", "VmHWM GB", "VmRSS GB", "wire MB");
    let mut peak_delta = 0.0f64;
    let before_all = kb("VmHWM:");
    for k in 0..nseg.min(total) {
        let si = if nseg <= 1 { total / 2 } else { k * (total - 1) / (nseg - 1) };
        let seg = session.segments[si].resolve().expect("resolve");
        let wire = bincode::serialize(&seg).map(|v| v.len()).unwrap_or(0) as f64 / 1e6;
        let before = kb("VmHWM:");
        let t = Instant::now();
        let receipt = server.prove_segment(&ctx, &seg).expect("prove segment");
        let s = t.elapsed().as_secs_f64();
        receipt.verify_integrity_with_context(&ctx).expect("segment receipt integrity");
        let hwm = kb("VmHWM:");
        peak_delta = peak_delta.max(hwm - before);
        println!("  {:>4}  {:>8.1}  {:>10.2}  {:>10.2}  {:>10.2}", si, s, hwm, kb("VmRSS:"), wire);
    }

    // Lift and join are the price of a SMALL po2. Segment proving throughput turns out to be flat
    // across po2 (147.6 / 145.9 / 147.8 us per cycle at 18 / 19 / 20), so shrinking segments to fit a
    // node's RAM looks free -- until you count recursion. Every segment must be lifted to a succinct
    // receipt and then joined pairwise, and if lift cost is per-SEGMENT rather than per-CYCLE then
    // halving po2 doubles the recursion bill. That is the number that decides whether po2 18 holds up.
    if std::env::var("HAZYNC_LIFT").ok().as_deref() == Some("1") {
        // CONSECUTIVE segments, not one segment twice. join() checks continuity -- segment a's post
        // state must be segment b's pre state -- so join(x, x) fails an equality check on the state
        // digest rather than timing anything. A segment does not follow itself.
        let i = total / 2;
        let mut lifted = Vec::new();
        let mut lift_s = 0.0f64;
        for k in [i, i + 1] {
            let seg = session.segments[k].resolve().expect("resolve");
            let sr = prove_segment_resilient(&server, &ctx, &seg, &format!("segment {i}"));
            let t = Instant::now();
            lifted.push(server.lift(&sr).expect("lift"));
            lift_s = lift_s.max(t.elapsed().as_secs_f64());
        }
        println!();
        println!("  lift  {:.1} s  (per segment)", lift_s);
        let t = Instant::now();
        server.join(&lifted[0], &lifted[1]).expect("join");
        let join_s = t.elapsed().as_secs_f64();
        println!("  join  {:.1} s  (per PAIR, so ~1 per segment over a whole tree)", join_s);
        println!("  recursion for {} segments in this chunk: {:.0} s   ({:.0} s lift + {:.0} s join)",
            total, total as f64 * lift_s + (total - 1) as f64 * join_s,
            total as f64 * lift_s, (total - 1) as f64 * join_s);
        println!("  peak RSS after recursion: {:.2} GB", kb("VmHWM:"));
    }

    println!();
    println!("  peak RSS overall         {:.2} GB", kb("VmHWM:"));
    println!("  largest single-prove rise {:.2} GB  (first prove pays setup; later ones reuse)", peak_delta);
    println!("  rise across all proves    {:.2} GB", kb("VmHWM:") - before_all);
    println!();
    println!("  A Bitcoin Ghost node runs in 3.87 GB. The figure that has to fit under that is the");
    println!("  peak RSS of a worker that ONLY proves segments — it never executes, so it never holds");
    println!("  the witness. Subtract the pre-prove VmHWM above to get that.");
    println!("  VRAM is invisible from inside the process: sample nvidia-smi alongside for CUDA.");
}

// SEGMENT DISTRIBUTION, phase 0: prove a chunk with every segment routed through a wire.
//
// Block latency is chunk_work/N + aggregate and the aggregate does not divide, so measured near-tip
// numbers put the floor near 18 minutes at ANY card count. Distributing SEGMENTS is the only route
// under ten, because it moves parallelism below the level recursion charges for.
//
// This proves the decomposition is sound before any network exists. Each segment is serialised,
// deserialised, and proved from the deserialised copy; each receipt is serialised and deserialised
// again. If a segment or a receipt cannot survive that round trip, nothing distributed can work, and
// this is where it shows -- cheaply, on one machine, with no protocol to debug.
//
// Assembly is NOT reimplemented here. `assemble_from_segment_receipts` is the same code
// `prove_session` runs after its own loop, so the two paths cannot drift. The step that made this
// worth doing carefully is the journal/assumption merge into the LAST segment's claim: miss it and
// you get a receipt that fails its own verify with nothing pointing at why.
fn seg_distribute_cmd() {
    use risc0_zkvm::{ExecutorImpl, VerifierContext, Segment, SegmentReceipt};
    use risc0_zkvm::sha::Digestible;
    use std::time::Instant;

    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
    let (_a, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");

    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);

    let t_exec = Instant::now();
    let session = ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF).unwrap().run().unwrap();
    let exec_s = t_exec.elapsed().as_secs_f64();
    let total = session.segments.len();

    println!("=== segment-distributed chunk prove — block {} chunk {} po2 {} ===", w.height, idx, seg_po2());
    println!("  execution {exec_s:.1} s   segments {total}");

    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = VerifierContext::default();

    let mut receipts: Vec<SegmentReceipt> = Vec::with_capacity(total);
    let (mut wire_out, mut wire_back) = (0usize, 0usize);
    let t_prove = Instant::now();
    for (i, sref) in session.segments.iter().enumerate() {
        let seg = sref.resolve().expect("resolve");

        // OUT: coordinator -> worker.
        let bytes = bincode::serialize(&seg).expect("serialize segment");
        wire_out += bytes.len();
        let seg_wire: Segment = bincode::deserialize(&bytes).expect("deserialize segment");

        // The worker's entire job. Nothing here needs the session, the ELF, or the witness.
        let sr = prove_segment_resilient(&server, &ctx, &seg_wire, &format!("segment {i}"));

        // BACK: worker -> coordinator. Verified on arrival, because a worker is untrusted: a receipt
        // is self-verifying, so a bad worker can only fail to produce one, never forge one.
        let rb = bincode::serialize(&sr).expect("serialize receipt");
        wire_back += rb.len();
        let sr_wire: SegmentReceipt = bincode::deserialize(&rb).expect("deserialize receipt");
        sr_wire.verify_integrity_with_context(&ctx).expect("returned receipt failed verify");

        receipts.push(sr_wire);
        if i % 50 == 0 || i + 1 == total {
            println!("    segment {}/{}  {:.0}s elapsed", i + 1, total, t_prove.elapsed().as_secs_f64());
        }
    }
    let prove_s = t_prove.elapsed().as_secs_f64();

    let t_asm = Instant::now();
    let info = server.assemble_from_segment_receipts(&ctx, &session, receipts)
        .expect("assemble from distributed receipts");
    let asm_s = t_asm.elapsed().as_secs_f64();

    info.receipt.verify(METHOD_ID).expect("DISTRIBUTED RECEIPT FAILED verify against METHOD_ID");

    println!();
    println!("  execution   {exec_s:8.1} s");
    println!("  proving     {prove_s:8.1} s   ({total} segments, each through a bincode round trip)");
    println!("  assembly    {asm_s:8.1} s   (lift + join + succinct, coordinator-side)");
    println!("  TOTAL       {:8.1} s", exec_s + prove_s + asm_s);
    println!();
    println!("  wire out    {:8.2} MB   ({:.3} MB/segment)", wire_out as f64/1e6, wire_out as f64/total as f64/1e6);
    println!("  wire back   {:8.2} MB   ({:.3} MB/segment)", wire_back as f64/1e6, wire_back as f64/total as f64/1e6);
    println!();
    if let Ok(out) = std::env::var("HAZYNC_SEG_OUT") {
        std::fs::write(&out, bincode::serialize(&info.receipt).expect("serialize")).expect("write receipt");
        println!("  saved {out}");
    }
    println!(">>> DISTRIBUTED RECEIPT VERIFIED against METHOD_ID.");
    println!("    journal {} bytes, digest {}", info.receipt.journal.bytes.len(), hex(info.receipt.journal.digest().as_bytes()));
    println!();
    println!("  Every segment crossed a wire and every receipt was verified on arrival. What is NOT");
    println!("  proved here: that this is FASTER. One machine proved them in sequence. The point is");
    println!("  that the work is now in units a worker can take, and the journal digest above is what");
    println!("  a monolithic prove of the same chunk must produce.");
}

// SEGMENT DISTRIBUTION, phase 1/2: a work directory that separate processes can share.
//
// The coordinator cannot hand off its Session -- it holds `Box<dyn SegmentRef>` and does not
// serialise -- and it does not need to. Assembly needs the session (journal, assumptions, claim), so
// the coordinator executes, publishes segments, and assembles. ONLY segment proving leaves the
// process, which is the 76% worth moving.
//
// Claiming is an O_EXCL create of claim_NNNN. That is atomic on a local filesystem and on NFS, needs
// no lock server, and a worker that dies simply leaves a claim that a sweeper can expire. Receipts
// are written to a .tmp and renamed, so the coordinator never sees a half-written file -- it polls
// for existence and a rename is atomic.
fn seg_workdir() -> std::path::PathBuf {
    std::path::PathBuf::from(std::env::var("HAZYNC_WORKDIR").unwrap_or_else(|_| "/tmp/hazync-segwork".into()))
}

// Worker: claim segments from the directory, prove them, write receipts. Knows nothing about the
// block, the guest, or the session -- it needs only the segment in front of it.
fn seg_work_cmd() {
    use risc0_zkvm::{VerifierContext, Segment};
    use std::time::Instant;
    let dir = seg_workdir();
    let id = std::env::var("HAZYNC_WORKER_ID").unwrap_or_else(|_| "w0".into());
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = VerifierContext::default();

    let count: usize = loop {
        if let Ok(s) = std::fs::read_to_string(dir.join("MANIFEST")) {
            if let Ok(n) = s.trim().parse() { break n; }
        }
        std::thread::sleep(std::time::Duration::from_millis(300));
    };

    let mut done = 0usize;
    let t0 = Instant::now();
    for i in 0..count {
        let claim = dir.join(format!("claim_{i:04}"));
        // create_new is the whole mutual exclusion: exactly one worker wins each segment.
        if std::fs::OpenOptions::new().write(true).create_new(true).open(&claim).is_err() { continue; }
        let seg_path = dir.join(format!("seg_{i:04}.bin"));
        let bytes = match std::fs::read(&seg_path) { Ok(b) => b, Err(e) => { eprintln!("[{id}] seg {i} unreadable: {e}"); continue; } };
        let seg: Segment = bincode::deserialize(&bytes).expect("deserialize segment");
        let t = Instant::now();
        let sr = prove_segment_resilient(&server, &ctx, &seg, &format!("segment {i}"));

        // STEP 2. Lift here rather than on the coordinator. Lifts are per-segment and wholly
        // independent, and they are the largest term that does not divide: 886.7 s of 3081 s on
        // CPU, 679.3 s of 1167 s on GPU. Every worker doing its own removes all of it at once.
        //
        // The LAST segment is the exception and has to stay behind. The session journal digest and
        // assumption set are merged into its claim before it is lifted, and a worker has neither the
        // session nor any way to get it. So the last worker returns an unlifted SegmentReceipt and
        // the coordinator finishes that one itself.
        let lift_here = std::env::var("HAZYNC_WORKER_LIFTS").ok().as_deref() == Some("1") && i + 1 < count;
        if lift_here {
            let lifted = server.lift(&sr).expect("lift");
            let tmp = dir.join(format!("lift_{i:04}.tmp"));
            std::fs::write(&tmp, bincode::serialize(&lifted).expect("serialize lift")).expect("write lift");
            std::fs::rename(&tmp, dir.join(format!("lift_{i:04}.bin"))).expect("rename lift");
        } else {
            let tmp = dir.join(format!("rcpt_{i:04}.tmp"));
            std::fs::write(&tmp, bincode::serialize(&sr).expect("serialize receipt")).expect("write receipt");
            std::fs::rename(&tmp, dir.join(format!("rcpt_{i:04}.bin"))).expect("rename receipt");
        }
        done += 1;
        println!("[{id}] segment {i} {} in {:.1}s", if lift_here {"proved+lifted"} else {"proved"}, t.elapsed().as_secs_f64());
    }
    println!("[{id}] DONE {done} segments in {:.1}s", t0.elapsed().as_secs_f64());
}

// Coordinator: execute, publish, wait, assemble, verify.
fn seg_coordinate_cmd() {
    use risc0_zkvm::{ExecutorImpl, VerifierContext, SegmentReceipt};
    use risc0_zkvm::sha::Digestible;
    use std::time::Instant;

    let dir = seg_workdir();
    std::fs::create_dir_all(&dir).expect("create workdir");
    for e in std::fs::read_dir(&dir).unwrap().flatten() { let _ = std::fs::remove_file(e.path()); }

    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
    let (_a, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);

    let t_exec = Instant::now();
    let session = ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF).unwrap().run().unwrap();
    let exec_s = t_exec.elapsed().as_secs_f64();
    let total = session.segments.len();

    let t_pub = Instant::now();
    let mut wire_out = 0usize;
    for (i, sref) in session.segments.iter().enumerate() {
        let seg = sref.resolve().expect("resolve");
        let bytes = bincode::serialize(&seg).expect("serialize segment");
        wire_out += bytes.len();
        std::fs::write(dir.join(format!("seg_{i:04}.bin")), &bytes).expect("write segment");
    }
    // MANIFEST last: it is the signal that every segment is on disk, so a worker that sees it can
    // trust any index below the count.
    std::fs::write(dir.join("MANIFEST"), format!("{total}\n")).expect("write manifest");
    let pub_s = t_pub.elapsed().as_secs_f64();

    println!("=== coordinator — block {} chunk {} po2 {} ===", w.height, idx, seg_po2());
    println!("  execution {exec_s:.1} s   published {total} segments in {pub_s:.1} s ({:.2} MB)", wire_out as f64/1e6);
    println!("  workdir {}", dir.display());
    println!("  waiting for {total} receipts...");

    let t_wait = Instant::now();
    let mut last = 0usize;
    loop {
        let have = (0..total).filter(|i| dir.join(format!("rcpt_{i:04}.bin")).exists()).count();
        if have != last { println!("    {have}/{total} receipts  {:.0}s", t_wait.elapsed().as_secs_f64()); last = have; }
        if have == total { break; }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    let wait_s = t_wait.elapsed().as_secs_f64();

    let ctx = VerifierContext::default();
    let mut receipts: Vec<SegmentReceipt> = Vec::with_capacity(total);
    let mut wire_back = 0usize;
    for i in 0..total {
        let bytes = std::fs::read(dir.join(format!("rcpt_{i:04}.bin"))).expect("read receipt");
        wire_back += bytes.len();
        let sr: SegmentReceipt = bincode::deserialize(&bytes).expect("deserialize receipt");
        // Workers are untrusted. A receipt is self-verifying, so this is the entire defence.
        sr.verify_integrity_with_context(&ctx).expect("worker receipt failed verify");
        receipts.push(sr);
    }

    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let t_asm = Instant::now();
    let info = server.assemble_from_segment_receipts(&ctx, &session, receipts).expect("assemble");
    let asm_s = t_asm.elapsed().as_secs_f64();
    info.receipt.verify(METHOD_ID).expect("DISTRIBUTED RECEIPT FAILED verify");

    println!();
    println!("  execution        {exec_s:8.1} s");
    println!("  publish          {pub_s:8.1} s");
    println!("  worker wall      {wait_s:8.1} s   <- this is what more workers shrink");
    println!("  assembly         {asm_s:8.1} s   <- coordinator-side, does not distribute yet");
    println!("  TOTAL            {:8.1} s", exec_s + pub_s + wait_s + asm_s);
    println!("  wire out/back    {:.2} / {:.2} MB", wire_out as f64/1e6, wire_back as f64/1e6);
    println!();
    println!(">>> DISTRIBUTED RECEIPT VERIFIED against METHOD_ID");
    println!("    journal digest {}", hex(info.receipt.journal.digest().as_bytes()));
}

// Print a saved receipt's journal digest and verify it. The gate for segment distribution is that a
// distributed prove and a monolithic prove of the SAME chunk agree on this value -- proving that
// routing every segment across a wire changed nothing about what was proved.
//
// Printing the digest from `seg-distribute` alone proves nothing: it would agree with itself.
fn receipt_digest_cmd(path: &str) {
    use risc0_zkvm::sha::Digestible;
    let bytes = std::fs::read(path).unwrap_or_else(|e| panic!("read {path}: {e}"));
    let r: risc0_zkvm::Receipt = bincode::deserialize(&bytes).expect("deserialize receipt");
    match r.verify(METHOD_ID) {
        Ok(()) => println!("VERIFIED against METHOD_ID"),
        Err(e) => { println!("VERIFY FAILED: {e}"); std::process::exit(1); }
    }
    println!("journal_bytes {}", r.journal.bytes.len());
    println!("journal_digest {}", hex(r.journal.digest().as_bytes()));
}

// SEGMENT DISTRIBUTION step 3: run the join tree as distributed work items.
//
// The join tree is what caps a distributed prover. Segment proving divides across workers; assembly
// did not, because risc0's fold was strictly linear -- lift, join into an accumulator, lift, join --
// so every join depended on the one before it. With the fold rebalanced into a tree (see the
// vendored crate), the joins at a given level are independent of each other, and independent work
// is work a worker can take.
//
// Level l holds ceil(n_l / 2) joins over n_l receipts. Each is published, claimed with the same
// O_EXCL create the segment workers use, and collected before the next level starts. The barrier
// per level is unavoidable -- level l+1 consumes level l's output -- but the DEPTH is log2(N), so
// at 44 segments that is 6 barriers rather than 43 sequential steps.
//
// An odd receipt at the end of a level carries forward untouched. It keeps its position, so
// adjacency is preserved and the final claim is unchanged.
fn seg_join_cmd() {
    use risc0_zkvm::{VerifierContext, SuccinctReceipt, ReceiptClaim};
    use std::time::Instant;
    let dir = seg_workdir();
    let id = std::env::var("HAZYNC_WORKER_ID").unwrap_or_else(|_| "j0".into());
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = VerifierContext::default();
    let mut done = 0usize;
    let t0 = Instant::now();

    // Follow the coordinator level by level. JOINLEVEL names the level currently open for claiming;
    // JOINDONE appearing ends the run.
    let mut level = 0usize;
    loop {
        if dir.join("JOINDONE").exists() { break; }
        let lv = match std::fs::read_to_string(dir.join("JOINLEVEL")) {
            Ok(s) => s.trim().split(',').map(|x| x.to_string()).collect::<Vec<_>>(),
            Err(_) => { std::thread::sleep(std::time::Duration::from_millis(200)); continue; }
        };
        if lv.len() != 2 { std::thread::sleep(std::time::Duration::from_millis(200)); continue; }
        let (cur, npairs): (usize, usize) = (lv[0].parse().unwrap_or(0), lv[1].parse().unwrap_or(0));
        if cur < level { std::thread::sleep(std::time::Duration::from_millis(200)); continue; }
        level = cur;

        for p in 0..npairs {
            let claim = dir.join(format!("jclaim_{level}_{p:04}"));
            if std::fs::OpenOptions::new().write(true).create_new(true).open(&claim).is_err() { continue; }
            let a_path = dir.join(format!("jin_{level}_{:04}.bin", p * 2));
            let b_path = dir.join(format!("jin_{level}_{:04}.bin", p * 2 + 1));
            let (ab, bb) = match (std::fs::read(&a_path), std::fs::read(&b_path)) {
                (Ok(a), Ok(b)) => (a, b),
                _ => { eprintln!("[{id}] level {level} pair {p}: inputs missing"); continue; }
            };
            let a: SuccinctReceipt<ReceiptClaim> = bincode::deserialize(&ab).expect("deserialize a");
            let b: SuccinctReceipt<ReceiptClaim> = bincode::deserialize(&bb).expect("deserialize b");
            let t = Instant::now();
            // join asserts a.post == b.pre. A worker that returns a join of the wrong two receipts
            // cannot produce something that survives this, which is why untrusted joins are safe.
            let j = server.join(&a, &b).expect("join");
            let tmp = dir.join(format!("jout_{level}_{p:04}.tmp"));
            std::fs::write(&tmp, bincode::serialize(&j).expect("serialize join")).expect("write join");
            std::fs::rename(&tmp, dir.join(format!("jout_{level}_{p:04}.bin"))).expect("rename join");
            done += 1;
            println!("[{id}] level {level} pair {p} joined in {:.1}s", t.elapsed().as_secs_f64());
        }
        level += 1;
    }
    println!("[{id}] JOIN DONE {done} joins in {:.1}s", t0.elapsed().as_secs_f64());
}

// Coordinator for step 3: prove segments (workers), lift them, then drive the join tree as
// distributed levels, then assemble. HAZYNC_JOIN_DISTRIBUTED=1 selects this over the in-process
// assembly, so both paths stay reachable and comparable.
fn seg_coordinate_tree_cmd() {
    use risc0_zkvm::{ExecutorImpl, VerifierContext, SegmentReceipt, SuccinctReceipt, ReceiptClaim};
    use risc0_zkvm::sha::Digestible;
    use std::time::Instant;

    let dir = seg_workdir();
    std::fs::create_dir_all(&dir).expect("create workdir");
    for e in std::fs::read_dir(&dir).unwrap().flatten() { let _ = std::fs::remove_file(e.path()); }

    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(0);
    let (_a, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);

    let t_exec = Instant::now();
    let session = ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF).unwrap().run().unwrap();
    let exec_s = t_exec.elapsed().as_secs_f64();
    let total = session.segments.len();

    for (i, sref) in session.segments.iter().enumerate() {
        let seg = sref.resolve().expect("resolve");
        std::fs::write(dir.join(format!("seg_{i:04}.bin")), bincode::serialize(&seg).expect("ser")).expect("write");
    }
    std::fs::write(dir.join("MANIFEST"), format!("{total}\n")).expect("manifest");
    println!("=== coordinator (distributed join tree) — block {} chunk {} po2 {} ===", w.height, idx, seg_po2());
    println!("  execution {exec_s:.1} s   {total} segments published");

    let worker_lifts = std::env::var("HAZYNC_WORKER_LIFTS").ok().as_deref() == Some("1");

    let t_wait = Instant::now();
    loop {
        // With worker lifts, segments 0..N-2 arrive as lift_ files and only the last as rcpt_.
        let have = (0..total).filter(|i| {
            if worker_lifts && i + 1 < total { dir.join(format!("lift_{i:04}.bin")).exists() }
            else { dir.join(format!("rcpt_{i:04}.bin")).exists() }
        }).count();
        if have == total { break; }
        std::thread::sleep(std::time::Duration::from_millis(500));
    }
    let prove_s = t_wait.elapsed().as_secs_f64();

    let ctx = VerifierContext::default();
    let mut receipts: Vec<SegmentReceipt> = Vec::new();
    let first_rcpt = if worker_lifts { total - 1 } else { 0 };
    for i in first_rcpt..total {
        let sr: SegmentReceipt = bincode::deserialize(&std::fs::read(dir.join(format!("rcpt_{i:04}.bin"))).expect("read")).expect("de");
        sr.verify_integrity_with_context(&ctx).expect("worker receipt failed verify");
        receipts.push(sr);
    }

    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");

    // With HAZYNC_WORKER_LIFTS the workers have already lifted every segment but the last, so all
    // that remains here is the last one -- merge the session output into its claim, then lift it.
    // prepare_lifts merges into the LAST element of what it is given, so handing it a one-element
    // vector does exactly that and nothing else.
    let t_lift = Instant::now();
    let lifted = if worker_lifts {
        let mut v: Vec<SuccinctReceipt<ReceiptClaim>> = Vec::with_capacity(total);
        for i in 0..total - 1 {
            let r: SuccinctReceipt<ReceiptClaim> =
                bincode::deserialize(&std::fs::read(dir.join(format!("lift_{i:04}.bin"))).expect("read lift")).expect("de lift");
            r.verify_integrity_with_context(&ctx).expect("worker lift failed verify");
            v.push(r);
        }
        let last = receipts.pop().expect("last segment receipt");
        let mut tail = server.prepare_lifts(&ctx, &session, vec![last]).expect("merge+lift last");
        v.append(&mut tail);
        v
    } else {
        server.prepare_lifts(&ctx, &session, receipts).expect("prepare lifts")
    };
    let lift_s = t_lift.elapsed().as_secs_f64();
    println!("  lifted {} receipts in {:.1} s{}", lifted.len(), lift_s,
        if worker_lifts { "  (all but the last done by workers)" } else { "" });

    // Drive the tree. Publish a level, wait for its joins, feed the outputs in as the next level.
    let t_join = Instant::now();
    let mut level_recs: Vec<SuccinctReceipt<ReceiptClaim>> = lifted;
    let mut level = 0usize;
    while level_recs.len() > 1 {
        for (i, r) in level_recs.iter().enumerate() {
            std::fs::write(dir.join(format!("jin_{level}_{i:04}.bin")), bincode::serialize(r).expect("ser")).expect("write");
        }
        let npairs = level_recs.len() / 2;                 // odd tail carries, not joined
        let odd = level_recs.len() % 2 == 1;
        std::fs::write(dir.join("JOINLEVEL"), format!("{level},{npairs}\n")).expect("level");
        println!("    level {level}: {} receipts -> {npairs} joins{}", level_recs.len(), if odd {" (+1 carried)"} else {""});
        loop {
            let have = (0..npairs).filter(|p| dir.join(format!("jout_{level}_{p:04}.bin")).exists()).count();
            if have == npairs { break; }
            std::thread::sleep(std::time::Duration::from_millis(300));
        }
        let mut next: Vec<SuccinctReceipt<ReceiptClaim>> = Vec::with_capacity(npairs + 1);
        for p in 0..npairs {
            let r: SuccinctReceipt<ReceiptClaim> = bincode::deserialize(&std::fs::read(dir.join(format!("jout_{level}_{p:04}.bin"))).expect("read")).expect("de");
            r.verify_integrity_with_context(&ctx).expect("joined receipt failed verify");
            next.push(r);
        }
        if odd { next.push(level_recs.pop().expect("odd tail")); }
        level_recs = next;
        level += 1;
    }
    std::fs::write(dir.join("JOINDONE"), b"1").expect("done");
    let join_s = t_join.elapsed().as_secs_f64();

    let joined = level_recs.pop().expect("one receipt remains");
    let t_asm = Instant::now();
    let info = server.assemble_from_joined(&ctx, &session, joined).expect("assemble from joined");
    let asm_s = t_asm.elapsed().as_secs_f64();
    info.receipt.verify(METHOD_ID).expect("DISTRIBUTED-TREE RECEIPT FAILED verify");

    println!();
    println!("  execution      {exec_s:8.1} s");
    println!("  segment prove  {prove_s:8.1} s   (workers)");
    println!("  lift           {lift_s:8.1} s   (coordinator; step 2 moves this to workers)");
    println!("  join tree      {join_s:8.1} s   ({level} levels, distributed)");
    println!("  resolve+build  {asm_s:8.1} s");
    println!();
    println!(">>> DISTRIBUTED-TREE RECEIPT VERIFIED against METHOD_ID");
    println!("    digest {}", hex(info.receipt.journal.digest().as_bytes()));
}

// Prove exactly one segment, from a file, to a file. The whole job of a remote worker.
//
// It takes no session, no ELF, no METHOD_ID and no block -- a Segment carries everything the
// prover needs. That is why a worker on another machine, built by a different toolchain against a
// guest with a different image id, can prove segments for a session it knows nothing about and
// return receipts that verify there. It is also why workers can be untrusted: the receipt is
// self-verifying and the coordinator checks it on arrival.
fn seg_prove_one_cmd() {
    use risc0_zkvm::{VerifierContext, Segment};
    use std::time::Instant;
    let inp = std::env::var("HAZYNC_SEGFILE").expect("HAZYNC_SEGFILE");
    let out = std::env::var("HAZYNC_RCPTFILE").expect("HAZYNC_RCPTFILE");
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = VerifierContext::default();
    let seg: Segment = bincode::deserialize(&std::fs::read(&inp).expect("read segment")).expect("deserialize segment");
    let t = Instant::now();
    let sr = prove_segment_resilient(&server, &ctx, &seg, &inp);
    sr.verify_integrity_with_context(&ctx).expect("own receipt failed verify");
    std::fs::write(&out, bincode::serialize(&sr).expect("serialize receipt")).expect("write receipt");
    println!("proved one segment in {:.1}s -> {out}", t.elapsed().as_secs_f64());
}

// PUSH TRANSPORT (hazync#151). The coordinator sends work; it does not wait to be asked.
//
// The pull worker cost three SSH connections per segment -- claim, fetch, return -- at roughly
// 150 ms of setup each, against a segment that proves in 470 ms on an L40S at po2 18. A second
// matched GPU therefore added NOTHING: box 2 took 63% of the segments and the run got no faster,
// because it spent more time on round trips than on proving. Bandwidth was never the constraint;
// a segment is 0.06 MB. Connection setup and latency were.
//
// Pushing removes all three. The coordinator already holds the work list, so there is nothing to
// claim; one connection stays open for the whole session; and the pipelining comes free from TCP
// rather than from threads in the worker. The coordinator writes segment N+1 into the socket while
// the worker is still proving N, so the worker's next read returns from a buffer that already
// filled. At depth 4 that is 240 KB in flight, which no socket notices.
//
// Frame: [u32 index][u32 len][bytes]. Index 0xFFFF_FFFF means no more work.
const SEG_EOF: u32 = 0xFFFF_FFFF;
// A join job rather than a segment. The index space is otherwise segment indices, so the top bit is
// free and one connection can carry both kinds of work without a second protocol or a second socket.
// Body is [u32 len_a][a][u32 len_b][b] -- the two lifted receipts to join.
const JOIN_TAG: u32 = 0x8000_0000;

fn pack_pair(a: &[u8], b: &[u8]) -> Vec<u8> {
    let mut v = Vec::with_capacity(8 + a.len() + b.len());
    v.extend_from_slice(&(a.len() as u32).to_le_bytes());
    v.extend_from_slice(a);
    v.extend_from_slice(&(b.len() as u32).to_le_bytes());
    v.extend_from_slice(b);
    v
}

fn unpack_pair(body: &[u8]) -> Option<(&[u8], &[u8])> {
    if body.len() < 8 { return None; }
    let la = u32::from_le_bytes(body[0..4].try_into().ok()?) as usize;
    if body.len() < 4 + la + 4 { return None; }
    let lb = u32::from_le_bytes(body[4 + la..8 + la].try_into().ok()?) as usize;
    if body.len() < 8 + la + lb { return None; }
    Some((&body[4..4 + la], &body[8 + la..8 + la + lb]))
}

fn write_frame(s: &mut std::net::TcpStream, idx: u32, body: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    s.write_all(&idx.to_le_bytes())?;
    s.write_all(&(body.len() as u32).to_le_bytes())?;
    s.write_all(body)?;
    s.flush()
}

fn read_frame(s: &mut std::net::TcpStream) -> std::io::Result<(u32, Vec<u8>)> {
    use std::io::Read;
    let mut i = [0u8; 4];
    s.read_exact(&mut i)?;
    let idx = u32::from_le_bytes(i);
    if idx == SEG_EOF { return Ok((idx, Vec::new())); }
    let mut l = [0u8; 4];
    s.read_exact(&mut l)?;
    let mut body = vec![0u8; u32::from_le_bytes(l) as usize];
    s.read_exact(&mut body)?;
    Ok((idx, body))
}

// Worker: connect, then read-prove-write forever. It holds no work list, does no claiming and
// makes no decisions -- the coordinator drives. That also makes it simpler than the pull worker.
fn seg_connect_cmd(addr: &str) {
    use risc0_zkvm::{VerifierContext, Segment, SuccinctReceipt, ReceiptClaim};
    use std::time::Instant;
    let id = std::env::var("HAZYNC_WORKER_ID").unwrap_or_else(|_| "push1".into());
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let ctx = VerifierContext::default();
    let mut s = std::net::TcpStream::connect(addr).unwrap_or_else(|e| panic!("connect {addr}: {e}"));
    s.set_nodelay(true).ok();   // these are small frames; Nagle would add 40 ms for nothing
    println!("[{id}] connected to {addr}");

    let (mut done, t0) = (0usize, Instant::now());
    loop {
        let (idx, body) = match read_frame(&mut s) { Ok(v) => v, Err(e) => { println!("[{id}] link closed: {e}"); break; } };
        if idx == SEG_EOF { println!("[{id}] no more work"); break; }
        let t = Instant::now();

        // A join job carries two lifted receipts instead of a segment. Same connection, same loop.
        // join() asserts a.post == b.pre, so a worker cannot return a join of the wrong two receipts
        // and have it survive the next level -- untrusted joins are safe for the same reason
        // untrusted proving is.
        if idx & JOIN_TAG != 0 {
            let (ab, bb) = unpack_pair(&body).expect("malformed join pair");
            let a: SuccinctReceipt<ReceiptClaim> = bincode::deserialize(ab).expect("deserialize a");
            let b: SuccinctReceipt<ReceiptClaim> = bincode::deserialize(bb).expect("deserialize b");
            let j = server.join(&a, &b).expect("join");
            let out = bincode::serialize(&j).expect("serialize join");
            if let Err(e) = write_frame(&mut s, idx, &out) { println!("[{id}] send failed: {e}"); break; }
            done += 1;
            if done % 25 == 0 { println!("[{id}] join {} in {:.2}s ({done} done)", idx & !JOIN_TAG, t.elapsed().as_secs_f64()); }
            continue;
        }

        let seg: Segment = bincode::deserialize(&body).expect("deserialize segment");
        let sr = prove_segment_resilient(&server, &ctx, &seg, &format!("segment {idx}"));
        let lifted = server.lift(&sr).expect("lift");
        let out = bincode::serialize(&lifted).expect("serialize lift");
        if let Err(e) = write_frame(&mut s, idx, &out) { println!("[{id}] send failed: {e}"); break; }
        done += 1;
        if done % 25 == 0 || done < 3 {
            println!("[{id}] segment {idx} in {:.2}s ({done} done, {:.1}s elapsed)", t.elapsed().as_secs_f64(), t0.elapsed().as_secs_f64());
        }
    }
    println!("[{id}] PUSH DONE {done} segments in {:.1}s", t0.elapsed().as_secs_f64());
}

// Coordinator with a push transport. Executes, then serves segments to whoever connects, keeping
// several in flight per worker so the network never becomes the worker's critical path.
//
// One thread per connection, sharing a work queue behind a mutex. Each thread writes up to
// PUSH_DEPTH segments before reading the first receipt back; because the worker proves serially,
// receipts return in the order they were sent, so a VecDeque is enough to match them up.
//
// If a worker dies its in-flight segments go back on the queue and another worker takes them. That
// is the same reassignment the pull design got from expiring a stale claim, without needing claims.
fn seg_serve_cmd() {
    use risc0_zkvm::{ExecutorImpl, VerifierContext, SegmentReceipt, SuccinctReceipt, ReceiptClaim};
    use risc0_zkvm::sha::Digestible;
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};
    use std::time::Instant;

    let port: u16 = std::env::var("HAZYNC_PORT").ok().and_then(|s| s.parse().ok()).unwrap_or(9110);
    let depth: usize = std::env::var("HAZYNC_PUSH_DEPTH").ok().and_then(|s| s.parse().ok()).unwrap_or(4);
    let idx: usize = std::env::var("HAZYNC_CHUNK").ok().and_then(|s| s.parse().ok()).unwrap_or(0);

    let (_a, w) = build_full();
    let bounds = chunk_bounds(&w, nchunks_env());
    let (lo, hi) = *bounds.get(idx).expect("chunk index");
    let mut b = ExecutorEnv::builder();
    b.segment_limit_po2(seg_po2());
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap();
    write_chunk_inputs(&mut b, &w, lo, hi);

    let t_exec = Instant::now();
    let session = ExecutorImpl::from_elf(b.build().unwrap(), METHOD_ELF).unwrap().run().unwrap();
    let exec_s = t_exec.elapsed().as_secs_f64();
    let total = session.segments.len();

    // Serialise every segment once, up front. The alternative -- resolving on demand -- would put
    // disk work on the critical path of a worker that is waiting.
    let mut wire: Vec<Vec<u8>> = Vec::with_capacity(total);
    for sref in session.segments.iter() {
        wire.push(bincode::serialize(&sref.resolve().expect("resolve")).expect("serialize"));
    }
    let wire = Arc::new(wire);
    let bytes: usize = wire.iter().map(|v| v.len()).sum();

    println!("=== push coordinator — block {} chunk {} po2 {} ===", w.height, idx, seg_po2());
    println!("  execution {exec_s:.1} s   {total} segments, {:.1} MB, depth {depth}", bytes as f64/1e6);
    println!("  listening on 0.0.0.0:{port}");

    // Segments 0..total-1 go to workers. The LAST one is deliberately withheld: the session journal
    // and assumption set are merged into its claim before it is lifted, and a worker has no session,
    // so the coordinator proves that one itself. Handing it out anyway is what made the wait below
    // spin -- workers returned all `total` segments while the loop tested for exactly `total - 1`.
    let queue: Arc<Mutex<VecDeque<usize>>> = Arc::new(Mutex::new((0..total - 1).collect()));
    let out: Arc<Mutex<Vec<Option<SuccinctReceipt<ReceiptClaim>>>>> = Arc::new(Mutex::new(vec![None; total]));
    // Join work, fed one tree level at a time. Threads take from here once segments run out, so a
    // connection stays open across both phases instead of the fleet disbanding after proving and
    // leaving the coordinator to fold alone -- which is what left assembly flat at 373 s while
    // segment proving scaled 1.96x.
    let jobs: Arc<Mutex<VecDeque<(u32, Vec<u8>)>>> = Arc::new(Mutex::new(VecDeque::new()));
    let jout: Arc<Mutex<std::collections::HashMap<u32, SuccinctReceipt<ReceiptClaim>>>> =
        Arc::new(Mutex::new(std::collections::HashMap::new()));
    let alldone = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let listener = std::net::TcpListener::bind(("0.0.0.0", port)).expect("bind");
    listener.set_nonblocking(true).ok();

    let t_work = Instant::now();
    let ctx = VerifierContext::default();

    // NO thread::scope HERE, and that is the fix for a deadlock I introduced. The scope's implicit
    // join waited for the connection threads; the connection threads waited for join work; and the
    // join work was only published after the scope returned. A circular wait, and it hung a run for
    // 35 minutes with the GPU idle.
    //
    // The connection threads touch only Arc state -- the queues, the outputs, the serialised
    // segments -- and never the prover or the session, so they do not need to borrow anything and
    // can be plain detached threads. The main thread then stays free to drive the tree WHILE they
    // are still alive, which is the whole point.
    let acc_alldone = alldone.clone();
    let (aq, ao, aw, aj, ajo) = (queue.clone(), out.clone(), wire.clone(), jobs.clone(), jout.clone());
    let acceptor = std::thread::spawn(move || {
        let mut handles = Vec::new();
        while !acc_alldone.load(std::sync::atomic::Ordering::Relaxed) {
            match listener.accept() {
                Ok((mut s, peer)) => {
                    println!("  worker connected from {peer}");
                    let (queue, out, wire, jobs, jout, alldone) =
                        (aq.clone(), ao.clone(), aw.clone(), aj.clone(), ajo.clone(), acc_alldone.clone());
                    handles.push(std::thread::spawn(move || {
                        // Each thread builds its own VerifierContext: it holds Rc, so it is not Sync.
                        let ctx = &VerifierContext::default();
                        s.set_nodelay(true).ok();
                        let mut inflight: VecDeque<usize> = VecDeque::new();
                        loop {
                            while inflight.len() < depth {
                                let next = { queue.lock().unwrap().pop_front() };
                                match next {
                                    Some(i) => {
                                        if write_frame(&mut s, i as u32, &wire[i]).is_err() {
                                            queue.lock().unwrap().push_front(i);
                                            break;
                                        }
                                        inflight.push_back(i);
                                    }
                                    None => break,
                                }
                            }
                            if inflight.is_empty() {
                                // Segments are gone: take join work as each level is published, and
                                // idle between levels rather than disconnecting.
                                let job = { jobs.lock().unwrap().pop_front() };
                                if let Some((tag, body)) = job {
                                    if write_frame(&mut s, tag, &body).is_err() {
                                        jobs.lock().unwrap().push_front((tag, body));
                                        break;
                                    }
                                    match read_frame(&mut s) {
                                        Ok((rt, rb)) => {
                                            match bincode::deserialize::<SuccinctReceipt<ReceiptClaim>>(&rb) {
                                                Ok(r) if r.verify_integrity_with_context(ctx).is_ok() => {
                                                    jout.lock().unwrap().insert(rt, r);
                                                }
                                                _ => {
                                                    println!("  worker returned a bad join for {}", rt & !JOIN_TAG);
                                                    jobs.lock().unwrap().push_back((tag, body));
                                                }
                                            }
                                        }
                                        Err(_) => { jobs.lock().unwrap().push_front((tag, body)); break; }
                                    }
                                    continue;
                                }
                                if alldone.load(std::sync::atomic::Ordering::Relaxed) {
                                    let _ = write_frame(&mut s, SEG_EOF, &[]);
                                    break;
                                }
                                std::thread::sleep(std::time::Duration::from_millis(20));
                                continue;
                            }
                            match read_frame(&mut s) {
                                Ok((i, body)) => {
                                    let r: SuccinctReceipt<ReceiptClaim> = match bincode::deserialize(&body) {
                                        Ok(r) => r, Err(e) => { println!("  bad receipt for {i}: {e}"); break; }
                                    };
                                    if r.verify_integrity_with_context(ctx).is_err() {
                                        println!("  worker returned an invalid receipt for {i}");
                                        queue.lock().unwrap().push_back(i as usize);
                                    } else {
                                        out.lock().unwrap()[i as usize] = Some(r);
                                    }
                                    inflight.retain(|&x| x != i as usize);
                                }
                                Err(_) => {
                                    let mut q = queue.lock().unwrap();
                                    for i in inflight.drain(..) { q.push_front(i); }
                                    break;
                                }
                            }
                        }
                    }));
                }
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(std::time::Duration::from_millis(50));
                }
                Err(e) => { println!("  accept failed: {e}"); break; }
            }
        }
        for h in handles { let _ = h.join(); }
    });

    // Wait for every segment to come back before lifting. The threads stay alive throughout and
    // pick up join work as the tree below publishes it.
    // Report progress while segments come in. Without this the coordinator prints its header and
    // then nothing for the whole segment phase, which is both the #145 complaint again and, more
    // immediately, a run that any no-output watchdog will kill for looking wedged while it is
    // working perfectly well. It already killed one.
    let mut nextmark = 0usize;
    let step = (total / 20).max(1);
    loop {
        let have = { out.lock().unwrap().iter().filter(|r| r.is_some()).count() };
        if have >= nextmark {
            let el = t_work.elapsed().as_secs_f64();
            let eta = if have > 0 { el / have as f64 * (total - 1 - have) as f64 } else { 0.0 };
            println!("    {have}/{} segments  {el:.0}s elapsed, ~{eta:.0}s left", total - 1);
            nextmark = have + step;
        }
        if have >= total - 1 { break; }       // >= not ==: a strict equality here spins forever if
                                              // the count ever overshoots, which is exactly what
                                              // happened when the last segment was still queued.
        std::thread::sleep(std::time::Duration::from_millis(200));
    }
    let work_s = t_work.elapsed().as_secs_f64();

    // Every segment but the last arrives lifted. The last cannot -- the session journal and
    // assumptions merge into its claim before lifting and a worker has no session -- so it is
    // proved here. In this mode the coordinator keeps that one segment for itself.
    let opts = ProverOpts::succinct();
    let server = risc0_zkvm::get_prover_server(&opts).expect("prover server");
    let t_asm = Instant::now();
    let mut lifted: Vec<SuccinctReceipt<ReceiptClaim>> = Vec::with_capacity(total);
    {
        let o = out.lock().unwrap();
        for i in 0..total - 1 { lifted.push(o[i].clone().expect("missing lift")); }
    }
    let last_seg = session.segments[total - 1].resolve().expect("resolve last");
    let last_rcpt: SegmentReceipt = prove_segment_resilient(&server, &ctx, &last_seg, "last segment");
    let mut tail = server.prepare_lifts(&ctx, &session, vec![last_rcpt]).expect("merge+lift last");
    lifted.append(&mut tail);

    // DISTRIBUTE THE JOIN TREE. Publish a level as jobs, wait for the workers to return them, feed
    // the results in as the next level. The barrier between levels is unavoidable -- level l+1
    // consumes level l's output -- but the depth is log2(N), so 1,684 segments is eleven barriers
    // rather than 1,683 sequential joins.
    //
    // In-process joining is what left assembly flat at 373 s while segment proving scaled 1.96x on
    // two cards. Everything else divided; this was the part that did not.
    let mut level = lifted;
    let mut lv = 0u32;
    while level.len() > 1 {
        let npairs = level.len() / 2;
        let odd = level.len() % 2 == 1;
        {
            let mut q = jobs.lock().unwrap();
            for p in 0..npairs {
                let a = bincode::serialize(&level[p * 2]).expect("ser a");
                let b = bincode::serialize(&level[p * 2 + 1]).expect("ser b");
                q.push_back((JOIN_TAG | (lv << 16) | p as u32, pack_pair(&a, &b)));
            }
        }
        println!("    level {lv}: {} receipts -> {npairs} joins{}", level.len(), if odd { " (+1 carried)" } else { "" });
        loop {
            let have = { jout.lock().unwrap().len() };
            if have >= npairs { break; }
            std::thread::sleep(std::time::Duration::from_millis(50));
        }
        let mut next = Vec::with_capacity(npairs + 1);
        {
            let mut m = jout.lock().unwrap();
            for p in 0..npairs {
                next.push(m.remove(&(JOIN_TAG | (lv << 16) | p as u32)).expect("missing join result"));
            }
        }
        // An odd receipt carries forward untouched, keeping its position so adjacency holds.
        if odd { next.push(level.pop().expect("odd tail")); }
        level = next;
        lv += 1;
    }
    alldone.store(true, std::sync::atomic::Ordering::Relaxed);
    let info = server.assemble_from_joined(&ctx, &session, level.pop().expect("one left")).expect("assemble");
    let asm_s = t_asm.elapsed().as_secs_f64();
    info.receipt.verify(METHOD_ID).expect("PUSH RECEIPT FAILED verify");

    println!();
    println!("  execution      {exec_s:8.1} s");
    println!("  worker wall    {work_s:8.1} s   <- pushed, {} segments over the network", total - 1);
    println!("  assembly       {asm_s:8.1} s   <- last segment + join tree, coordinator-side");
    println!("  TOTAL          {:8.1} s", exec_s + work_s + asm_s);
    println!();
    println!(">>> PUSH-TRANSPORT RECEIPT VERIFIED against METHOD_ID");
    println!("    digest {}", hex(info.receipt.journal.digest().as_bytes()));
}

// Survive hazync#119: the CUDA prover intermittently returns a segment proof that fails its own
// internal verify. Reproduced this session at po2 22 on an idle card, with UNMODIFIED upstream
// scheduling, so it is the prover and not anything layered on it. Filed upstream as risc0 #3798 /
// #3799; no replies, repo dormant.
//
// We cannot fix it, but we do not have to lose work to it. The failure is DETECTABLE -- prove_segment
// verifies before returning -- and it is intermittent, so re-proving the same segment almost always
// succeeds. One bad segment killed a whole measurement run today; it should have cost one retry.
//
// LOUD ON PURPOSE. A silent retry would turn a known prover fault into an invisible one and hide any
// change in its rate, which is the number #119 actually needs. Every retry prints, and the total is
// reported at the end of a run, so the rate stays measurable. If a segment fails REPEATEDLY that is
// not #119 and the error is propagated unchanged.
fn prove_segment_resilient(
    server: &std::rc::Rc<dyn risc0_zkvm::ProverServer>,
    ctx: &risc0_zkvm::VerifierContext,
    seg: &risc0_zkvm::Segment,
    what: &str,
) -> risc0_zkvm::SegmentReceipt {
    const ATTEMPTS: usize = 3;
    let mut last: Option<String> = None;
    for attempt in 1..=ATTEMPTS {
        match server.prove_segment(ctx, seg) {
            Ok(r) => {
                if attempt > 1 {
                    println!("  [#119] {what}: succeeded on attempt {attempt}");
                    RETRIES_119.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                }
                return r;
            }
            Err(e) => {
                let msg = format!("{e:#}");
                // Only retry the fault we know is transient. An OOM, a malformed segment or a
                // missing zkr will fail identically every time, and retrying those wastes a card
                // for three times as long before saying the same thing.
                let transient = msg.contains("verification indicates proof is invalid")
                    || msg.contains("verify segment");
                println!("  [#119] {what}: attempt {attempt}/{ATTEMPTS} failed: {}",
                    msg.lines().next().unwrap_or("?"));
                if !transient {
                    panic!("{what}: not a #119 fault, not retrying: {msg}");
                }
                last = Some(msg);
            }
        }
    }
    panic!("{what}: failed {ATTEMPTS} times, last error: {}", last.unwrap_or_default());
}

static RETRIES_119: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

fn report_119_retries() {
    let n = RETRIES_119.load(std::sync::atomic::Ordering::Relaxed);
    if n > 0 {
        println!("  [#119] {n} segment(s) needed a retry this run — the rate is worth recording on the issue");
    }
}

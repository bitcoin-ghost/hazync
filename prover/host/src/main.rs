use bitcoin::consensus::{deserialize, serialize};
use bitcoin::hashes::Hash as _;
use bitcoin::{absolute, transaction, Amount, OutPoint, ScriptBuf, Sequence, Transaction, TxIn, TxOut, Txid, Witness};
use hazync_utreexo::{hash_leaf, Forest, Hash};
use methods::{METHOD_ELF, METHOD_ID};
use risc0_zkvm::{default_executor, default_prover, ExecutorEnv, ProverOpts};
use serde::{Deserialize, Serialize};

// H8 domain tags — first committed field of each recursion-consumed journal (must match the guest).
const KIND_CHAIN: u32 = 0xC4A1_0002;
const KIND_RANGE: u32 = 0xC4A1_0006;
const KIND_CHUNK: u32 = 0xC4A1_0004;

// ---- Wire format: MUST match the guest structs field-for-field, in order. ----
#[derive(Serialize, Deserialize, Clone)]
struct WireProof { leaf: [u8; 32], position: u64, siblings: Vec<[u8; 32]> }
#[derive(Serialize, Deserialize)]
struct BlockInput {
    raw_tx: Vec<u8>, input_idx: u32, prevouts: Vec<u8>, flags: u32,
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
    root_prev: WireStump, inputs: Vec<BlockInput>, new_outputs: Vec<[u8; 32]>, root_next: WireStump,
    bip30: Option<Bip30Overwrite>,
}
#[derive(Serialize, Deserialize, Clone)]
struct ChainState {
    kind: u32, // H8: == KIND_CHAIN
    tip_hash: [u8; 32], utxo_roots: Vec<Option<[u8; 32]>>, utxo_leaves: u64,
    cum_work: [u8; 32], height: u32,
    prev_nbits: u32, prev_time: u32, epoch_start: u32, recent_times: Vec<u32>,
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
fn rev(mut v: Vec<u8>) -> Vec<u8> { v.reverse(); v }
fn arr(v: Vec<u8>) -> [u8; 32] { v.try_into().unwrap() }

fn coin_leaf(txid_internal: &[u8; 32], vout: u32, value_sat: u64, spk: &[u8], height: u32, is_coinbase: bool, coin_mtp: u32) -> Hash {
    let mut b = Vec::with_capacity(53 + spk.len());
    b.extend_from_slice(txid_internal);
    b.extend_from_slice(&vout.to_le_bytes());
    b.extend_from_slice(&value_sat.to_le_bytes());
    b.extend_from_slice(spk);
    b.extend_from_slice(&height.to_le_bytes());
    b.push(is_coinbase as u8);
    b.extend_from_slice(&coin_mtp.to_le_bytes());
    hash_leaf(&b)
}
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
fn boundary_digest(tip: &[u8; 32], roots: &[Option<[u8; 32]>], leaves: u64, nbits: u32, time: u32, epoch: u32, recent: &[u32]) -> [u8; 32] {
    let mut m: Vec<u8> = Vec::new();
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
struct Spend { raw: Vec<u8>, prev_value: u64, prev_spk: Vec<u8>, flags: u32, coin_height: u32, coin_is_coinbase: bool, coin_mtp: u32 }

/// Build a block witness against (and advancing) the running accumulator `forest`.
fn build_block(
    forest: &mut Forest, header: Vec<u8>, height: u32, coinbase_hex: &str, spends: &[Spend], create_mtp: u32,
) -> BlockWitness {
    let coinbase: Transaction = deserialize(&hx(coinbase_hex)).unwrap();
    let cb_txid = coinbase.compute_txid().to_byte_array();
    let root_prev = wire_stump(forest);

    let mut txids = vec![cb_txid];
    let mut inputs = Vec::new();
    for sp in spends {
        let tx: Transaction = deserialize(&sp.raw).unwrap();
        txids.push(tx.compute_txid().to_byte_array());
        let op = tx.input[0].previous_output;
        let spk = ScriptBuf::from_bytes(sp.prev_spk.clone());
        let coin = coin_leaf(&op.txid.to_byte_array(), op.vout, sp.prev_value, spk.as_bytes(), sp.coin_height, sp.coin_is_coinbase, sp.coin_mtp);
        let prevouts = serialize(&vec![TxOut { value: Amount::from_sat(sp.prev_value), script_pubkey: spk }]);
        let pos = forest.leaves.iter().position(|x| *x == coin).expect("spent coin in accumulator");
        let last = forest.leaves.len() - 1;
        inputs.push(BlockInput {
            raw_tx: sp.raw.clone(), input_idx: 0, prevouts, flags: sp.flags,
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
    BlockWitness { header, height, coinbase_tx: hx(coinbase_hex), txids, wtxids, root_prev, inputs, new_outputs, root_next, bip30: None }
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
        recent_times: (0..11).map(|i| 1_231_729_000u32 + i * 140).collect(), self_id: METHOD_ID,
    };
    (forest, anchor)
}

fn header_170() -> Vec<u8> {
    build_header(HASH169, &arr(rev(hx(MERKLE170))), 1_231_731_025, 0x1d00ffff, 1_889_418_792)
}
fn spend_170() -> Spend {
    Spend { raw: hx(SPEND170), prev_value: SPEND170_PREV_VALUE, prev_spk: hx(SPEND170_PREV_SPK), flags: 0, coin_height: 9, coin_is_coinbase: true, coin_mtp: 1_231_473_279 }
}

// PROVE (not execute) the block-170 fold: a real STARK receipt attesting the block is valid and
// extends the anchor. Run on the VPS (this is memory-heavy). is_base=1 → no recursion assumption.
fn prove_block() {
    use std::time::Instant;
    let (mut forest, anchor) = seed_and_anchor();
    let w = build_block(&mut forest, header_170(), 170, CB170, &[spend_170()], median_u32(&anchor.recent_times));

    println!("=== PROVING block 170 chain_step (real STARK receipt) ===");
    let mut b = ExecutorEnv::builder();
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

    // Seed the accumulator: filler + EVERY input's spent coin (real height/coinbase/creation-MTP) + filler.
    let mut forest = Forest::new();
    for i in 0..4u64 { forest.add(hash_leaf(&[b"pre".as_slice(), &i.to_le_bytes()].concat())); }
    for p in &ptxs {
        for (i, o) in p.prevouts.iter().enumerate() {
            forest.add(leaf_of(&p.tx.input[i].previous_output, o, p.meta[i].0, p.meta[i].1, p.meta[i].2));
        }
    }
    for i in 0..2u64 { forest.add(hash_leaf(&[b"post".as_slice(), &i.to_le_bytes()].concat())); }

    let mut anchor = ChainState {
        kind: KIND_CHAIN,
        tip_hash: arr(rev(hx(prev))), utxo_roots: forest.roots(), utxo_leaves: forest.leaves.len() as u64,
        cum_work: [0u8; 32], height: height - 1,
        prev_nbits: bits, prev_time: time.saturating_sub(600), epoch_start: time.saturating_sub(600 * 1000),
        recent_times: (0..11).map(|i| time.saturating_sub(2000) + i * 100).collect(), self_id: METHOD_ID,
    };
    // COV-1 negative-test hook (test-only, inert unless HAZYNC_COV1_BADTIME set): make the previous 11
    // blocks' median-time-past equal THIS block's timestamp, so `time_ok = block_time > prev_mtp` is
    // false — the "time-too-old" rejection. create_mtp stays median(recent_times) so host↔guest remain
    // consistent; only time_ok fails. NEVER set in production.
    if std::env::var("HAZYNC_COV1_BADTIME").is_ok() { anchor.recent_times = vec![time; 11]; }

    // Build the witness: per tx a shared full-prevouts blob; per input a BlockInput (tx_first on input 0).
    let root_prev = wire_stump(&forest);
    let mut txids = vec![cb_txid];
    let mut wtxids: Vec<[u8; 32]> = vec![[0u8; 32]]; // coinbase wtxid = zeros (BIP141)
    let mut inputs: Vec<BlockInput> = Vec::new();
    // SEC-2 negative-test hook (test-only, inert unless HAZYNC_SEC2_BADPOS is set): corrupt the FIRST
    // spend's claimed global position while leaving its inclusion proof honest — the exact inconsistency
    // an honest witness-builder cannot express (both fields normally derive from the same `pos`). The
    // guest's hardened `delete` must reject it (`all_ok=false`, and the accumulator diverges so
    // `root_matches=false`). See SECURITY.md / ROADMAP (SEC-2). NEVER set in production.
    let sec2_bad = std::env::var("HAZYNC_SEC2_BADPOS").is_ok();
    for p in &ptxs {
        txids.push(p.txid);
        wtxids.push(p.tx.compute_wtxid().to_byte_array());
        let prevouts_blob = serialize(&p.prevouts);
        for i in 0..p.tx.input.len() {
            let (ch_i, cb_i, mtp_i) = p.meta[i];
            let coin = leaf_of(&p.tx.input[i].previous_output, &p.prevouts[i], ch_i, cb_i, mtp_i);
            let pos = forest.leaves.iter().position(|x| *x == coin).expect("input coin in accumulator");
            let last = forest.leaves.len() - 1;
            let mut global_pos = pos as u64;
            if sec2_bad && inputs.is_empty() {
                // a different but in-range index -> membership proof stays valid, position is a lie
                global_pos = if (pos as u64) < last as u64 { pos as u64 + 1 } else { (pos as u64).saturating_sub(1) };
                eprintln!("[SEC2-TEST] corrupting first spend global_pos {} -> {} (proof_i left honest)", pos, global_pos);
            }
            inputs.push(BlockInput {
                raw_tx: p.raw.clone(), input_idx: i as u32, prevouts: prevouts_blob.clone(), flags: 0,
                global_pos, coin_height: ch_i, coin_is_coinbase: cb_i as u32, coin_mtp: mtp_i, tx_first: (i == 0) as u32,
                proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)),
            });
            forest.delete(pos);
        }
    }
    // Created-output creation-MTP = median(anchor.recent_times) — the same MTP(height-1) the guest
    // computes (median of prev.recent_times) and now commits on created outputs. Consistent host↔guest.
    let cmtp = median_u32(&anchor.recent_times);
    let mut new_outputs: Vec<[u8; 32]> = Vec::new();
    for v in 0..coinbase.output.len() { if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; } let l = out_leaf_of(&coinbase, &cb_txid, v, height, true, cmtp); forest.add(l); new_outputs.push(l); }
    for p in &ptxs { for v in 0..p.tx.output.len() { if !out_spendable(p.tx.output[v].script_pubkey.as_bytes()) { continue; } let l = out_leaf_of(&p.tx, &p.txid, v, height, false, cmtp); forest.add(l); new_outputs.push(l); } }
    let root_next = wire_stump(&forest);
    let w = BlockWitness { header, height, coinbase_tx: hx(cb_hex), txids, wtxids, root_prev, inputs, new_outputs, root_next, bip30: None };
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
    println!("=== CHECK-FULL (execute, no proof) block {} — {} inputs ===", w.height, w.inputs.len());
    let mut b = ExecutorEnv::builder();
    b.write(&2u32).unwrap();
    b.write(&state_journal_bytes(&anchor)).unwrap();
    b.write(&w).unwrap();
    b.write(&1u32).unwrap();
    b.write(&METHOD_ID).unwrap();
    let t = Instant::now();
    let s = default_executor().execute(b.build().unwrap(), METHOD_ELF)
        .expect("CHECK-FULL FAILED: guest asserted a consensus flag false (see message above)");
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
        recent_times: vec![GENESIS_TIME], self_id: METHOD_ID,
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

    // This block's txids — an input spending one of these is an IN-BLOCK spend (H1): the coin was
    // created earlier in this same block, so it never entered the accumulator (ephemeral cancellation).
    let this_txids: std::collections::HashSet<[u8; 32]> =
        std::iter::once(cb_txid).chain(ptxs.iter().map(|p| p.txid)).collect();
    let mut spent_in_block: std::collections::HashSet<([u8; 32], u32)> = std::collections::HashSet::new();

    for p in &ptxs {
        txids.push(p.txid);
        wtxids.push(p.tx.compute_wtxid().to_byte_array());
        let prevouts_blob = serialize(&p.prevouts);
        for i in 0..p.tx.input.len() {
            let (ch, cb, mtp) = p.meta[i];
            let o = &p.prevouts[i];
            let op = p.tx.input[i].previous_output;
            let coin = coin_leaf(&op.txid.to_byte_array(), op.vout, o.value.to_sat(), o.script_pubkey.as_bytes(), ch, cb, mtp);
            if this_txids.contains(&op.txid.to_byte_array()) {
                // IN-BLOCK: coin never entered the accumulator. Dummy proof; the guest derives in-block
                // from leaf membership and skips the accumulator delete. Script still verifies.
                spent_in_block.insert((op.txid.to_byte_array(), op.vout));
                inputs.push(BlockInput {
                    raw_tx: p.raw.clone(), input_idx: i as u32, prevouts: prevouts_blob.clone(), flags: 0,
                    global_pos: 0, coin_height: ch, coin_is_coinbase: cb as u32, coin_mtp: mtp, tx_first: (i == 0) as u32,
                    proof_i: WireProof { leaf: coin, position: 0, siblings: vec![] },
                    proof_last: WireProof { leaf: coin, position: 0, siblings: vec![] },
                });
            } else {
                // EXTERNAL: prove inclusion in the carried forest, delete.
                let pos = forest.leaves.iter().position(|x| *x == coin)
                    .expect("spent coin not in carried accumulator (bad metadata)");
                let last = forest.leaves.len() - 1;
                inputs.push(BlockInput {
                    raw_tx: p.raw.clone(), input_idx: i as u32, prevouts: prevouts_blob.clone(), flags: 0,
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
    let self_mtp = block_mtp.get(height as usize).copied().unwrap_or(time);
    let mut new_outputs = Vec::new();
    let add_out = |tx: &Transaction, txid: &[u8; 32], is_cb: bool, forest: &mut Forest, no: &mut Vec<Hash>| {
        for v in 0..tx.output.len() {
            if !out_spendable(tx.output[v].script_pubkey.as_bytes()) { continue; }
            if spent_in_block.contains(&(*txid, v as u32)) { continue; }
            let l = out_leaf_of(tx, txid, v, height, is_cb, self_mtp);
            forest.add(l);
            no.push(l);
        }
    };
    add_out(&coinbase, &cb_txid, true, forest, &mut new_outputs);
    for p in &ptxs {
        add_out(&p.tx, &p.txid, false, forest, &mut new_outputs);
    }
    let root_next = wire_stump(forest);
    BlockWitness { header, height, coinbase_tx: hx(cb_hex), txids, wtxids, root_prev, inputs, new_outputs, root_next, bip30: None }
}

// Prove one chain step to a SUCCINCT receipt (FIX A): cheap composition for a long recursive chain.
fn prove_step_succinct(prev_journal: Vec<u8>, prev_receipt: Option<risc0_zkvm::Receipt>, w: &BlockWitness, is_base: u32) -> risc0_zkvm::Receipt {
    let mut b = ExecutorEnv::builder();
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
    out_tip_hash: [u8; 32], out_roots: Vec<Option<[u8; 32]>>, out_leaves: u64,
    out_nbits: u32, out_time: u32, out_epoch_start: u32, out_recent: Vec<u32>,
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
    r.verify(METHOD_ID).expect("verify");
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

// Verify a range receipt WITHOUT the genesis assertion — the CPU check a coordinator runs on each
// submitted contribution. Confirms the STARK is valid and reports the committed [lo,hi] + boundary
// tips, so the coordinator can chain ranges (out_tip of k == in_tip of k+1) into a genesis-anchored
// frontier without doing any proving/folding itself.
fn verify_any_cmd(bin: &str) {
    let r: risc0_zkvm::Receipt = bincode::deserialize(&std::fs::read(bin).expect("bin")).unwrap();
    r.verify(METHOD_ID).expect("verify"); // real STARK verification
    let rs: RangeState = r.journal.decode().unwrap();
    assert!(rs.self_id == METHOD_ID, "self_id != METHOD_ID");
    assert!(rs.kind == KIND_RANGE, "receipt is not a RangeState (domain tag)"); // H8
    // If this range CLAIMS to connect to genesis, its full genesis in-boundary must be pinned — else a
    // prover fabricates the initial UTXO set / difficulty and the coordinator chains it into the frontier.
    if rs.in_tip_hash == arr(rev(hx(GENESIS_HASH))) {
        assert_genesis_in_boundary(&rs);
    }
    // Expose FULL-boundary digests so the coordinator chains on `out_bhash(k) == in_bhash(k+1)` — the
    // complete seam check the guest fold does (tip + UTXO roots + leaves + difficulty + MTP window), not
    // just tip-hash. Without this a mid-chain range can fabricate its in-boundary UTXO set / difficulty.
    let in_bh = boundary_digest(&rs.in_tip_hash, &rs.in_roots, rs.in_leaves, rs.in_nbits, rs.in_time, rs.in_epoch_start, &rs.in_recent);
    let out_bh = boundary_digest(&rs.out_tip_hash, &rs.out_roots, rs.out_leaves, rs.out_nbits, rs.out_time, rs.out_epoch_start, &rs.out_recent);
    println!("RANGE-OK lo={} hi={} in_tip={} out_tip={} out_leaves={} range_work={} in_bhash={} out_bhash={}",
        rs.lo, rs.hi, hex(&rs.in_tip_hash), hex(&rs.out_tip_hash), rs.out_leaves, work_u128(&rs.range_work),
        hex(&in_bh), hex(&out_bh));
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
        b.write(&4u32).unwrap();
        b.write(&chunk_height).unwrap();
        b.write(&header_hash(&w.header)).unwrap(); // block hash for flag exceptions
        b.write(&((hi - lo) as u32)).unwrap();
        for inp in &w.inputs[lo..hi] {
            b.write(&ChunkInput {
                raw_tx: inp.raw_tx.clone(), input_idx: inp.input_idx, prevouts: inp.prevouts.clone(),
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
    b.write(&4u32).unwrap();
    b.write(&w.height).unwrap();
    b.write(&header_hash(&w.header)).unwrap(); // block hash for flag exceptions
    b.write(&((hi - lo) as u32)).unwrap();
    for inp in &w.inputs[lo..hi] {
        b.write(&ChunkInput {
            raw_tx: inp.raw_tx.clone(), input_idx: inp.input_idx, prevouts: inp.prevouts.clone(),
            coin_height: inp.coin_height, coin_is_coinbase: inp.coin_is_coinbase, coin_mtp: inp.coin_mtp,
        }).unwrap();
    }
    let t = Instant::now();
    // SCALING: prove the chunk to a SUCCINCT receipt (not the default composite). This runs the
    // STARK-to-STARK "lift" NOW, in parallel across the chunk fleet — so agg-chunks resolves each
    // assumption cheaply instead of lifting all N composite receipts sequentially (the dominant cost
    // of the 741000 aggregate: ~1645s → expected to collapse to a cheap fold). See SCALING.md.
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
    for (i, tx) in txs.iter().enumerate() {
        txids.push(tx.compute_txid().to_byte_array());
        wtxids.push(tx.compute_wtxid().to_byte_array());
        if inblock[i] {
            // spends A:0 (in-block coin, created THIS block at height H, mtp = block_time). No
            // accumulator proof needed — the guest skips the delete for in-block coins.
            let prevouts = serialize(&vec![TxOut { value: Amount::from_sat(5_000_000_000), script_pubkey: optrue() }]);
            inputs.push(BlockInput {
                raw_tx: serialize(*tx), input_idx: 0, prevouts, flags: 0,
                global_pos: 0, coin_height: SYNTH_H, coin_is_coinbase: 0, coin_mtp: SYNTH_T, tx_first: 1,
                proof_i: WireProof { leaf: [0u8; 32], position: 0, siblings: vec![] },
                proof_last: WireProof { leaf: [0u8; 32], position: 0, siblings: vec![] },
            });
        } else {
            // external: spends C from the accumulator (real inclusion proof).
            let prevouts = serialize(&vec![TxOut { value: Amount::from_sat(5_000_001_000), script_pubkey: optrue() }]);
            let pos = forest.leaves.iter().position(|x| *x == c_leaf).expect("C in accumulator");
            let last = forest.leaves.len() - 1;
            inputs.push(BlockInput {
                raw_tx: serialize(*tx), input_idx: 0, prevouts, flags: 0,
                global_pos: pos as u64, coin_height: 1, coin_is_coinbase: 0, coin_mtp: 0, tx_first: 1,
                proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)),
            });
            forest.delete(pos);
        }
    }
    let root_next = wire_stump(&forest); // approximate — root_matches is not part of all_ok
    let header = build_header_v(1, HASH169, &[0u8; 32], SYNTH_T, 0x1d00ffff, 0);
    BlockWitness { header, height: SYNTH_H, coinbase_tx: serialize(cb), txids, wtxids, root_prev, inputs, new_outputs: vec![], root_next, bip30: None }
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

    let mk_input = |forest: &mut Forest, idx: u32, leaf: Hash, blob: Vec<u8>| -> BlockInput {
        let pos = forest.leaves.iter().position(|x| *x == leaf).expect("coin in accumulator");
        let last = forest.leaves.len() - 1;
        let bi = BlockInput { raw_tx: t_raw.clone(), input_idx: idx, prevouts: blob, flags: 0,
            global_pos: pos as u64, coin_height: 1, coin_is_coinbase: 0, coin_mtp: 0, tx_first: (idx == 0) as u32,
            proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)) };
        forest.delete(pos); bi
    };
    // input 0 carries the phantom blob (if requested); input 1 always carries the real blob.
    let in0 = mk_input(&mut forest, 0, c1_leaf, if phantom { phantom_blob } else { real_blob.clone() });
    let in1 = mk_input(&mut forest, 1, c2_leaf, real_blob.clone());

    let header = build_header_v(1, HASH169, &[0u8; 32], SYNTH_T, 0x1d00ffff, 0);
    BlockWitness { header, height: SYNTH_H, coinbase_tx: serialize(&cb),
        txids: vec![cb.compute_txid().to_byte_array(), t.compute_txid().to_byte_array()],
        wtxids: vec![[0u8; 32], t.compute_wtxid().to_byte_array()],
        root_prev, inputs: vec![in0, in1], new_outputs: vec![], root_next: wire_stump(&forest), bip30: None }
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
        let pos = forest.leaves.iter().position(|x| *x == *l).expect("superseded coin present");
        let last = forest.leaves.len() - 1;
        dels.push(Bip30Del { global_pos: pos as u64, proof_i: wire_proof(&forest.prove(pos)), proof_last: wire_proof(&forest.prove(last)) });
        forest.delete(pos);
    }
    for v in 0..coinbase.output.len() {
        if !out_spendable(coinbase.output[v].script_pubkey.as_bytes()) { continue; }
        forest.add(out_leaf_of(&coinbase, &cb_txid, v, height, true, new_mtp));
    }
    let root_next = wire_stump(&forest);

    let mk = |bip30: Option<Bip30Overwrite>| BlockWitness {
        header: header.clone(), height, coinbase_tx: hx(cb_hex), txids: vec![cb_txid], wtxids: vec![[0u8; 32]],
        root_prev: root_prev.clone(), inputs: vec![], new_outputs: vec![], root_next: root_next.clone(), bip30,
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

fn main() {
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
    if let Some(p) = args.iter().position(|a| a == "verify-range") {
        verify_range_cmd(args.get(p + 1).expect("verify-range <bin>"));
        return;
    }
    if let Some(p) = args.iter().position(|a| a == "verify-any") {
        verify_any_cmd(args.get(p + 1).expect("verify-any <bin>"));
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
        recent_times: (0..11).map(|i| 1_231_729_000u32 + i * 140).collect(), self_id: METHOD_ID,
    };

    // Fold each real block. (170 has the P2PK spend; 171/172 are coinbase-only.)
    let blocks: Vec<(u32, Vec<u8>, &str, Vec<Spend>, &str)> = vec![
        (170, build_header(HASH169, &arr(rev(hx(MERKLE170))), 1_231_731_025, 0x1d00ffff, 1_889_418_792), CB170,
            vec![Spend { raw: hx(SPEND170), prev_value: SPEND170_PREV_VALUE, prev_spk: hx(SPEND170_PREV_SPK), flags: 0, coin_height: 9, coin_is_coinbase: true, coin_mtp: 1_231_473_279 }], HASH170),
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
        let linked = if i == 0 { true } else { true }; // enforced inside the guest (panics if not)
        println!("block {height:>3}: tip {} {}  height {}  cumwork {}  Δwork {}  ({} cyc){}",
            &hex(&next.tip_hash)[..16], if hash_ok { "✓" } else { "✗MISMATCH" }, next.height,
            work_u128(&next.cum_work), work_u128(&next.cum_work) - work_u128(&state.cum_work), cycles,
            if is_base == 1 { "  [base: anchored at 169]" } else { "  [recursion hook: prev is a chain proof]" });
        let _ = linked;
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

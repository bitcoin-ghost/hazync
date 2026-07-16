# Hazync — real proving on the VPS

Everything to date runs in RISC0 **execute** mode (cycle-accurate logic validation). This is the
step to **prove** — turn a validated block into a STARK receipt any node can verify. Do it on the
provisioned box (`provision-vps.sh`); proving is RAM-heavy (WSL2 11 GB can't).

## 1. Single-block proof — turnkey (works with the current guest)
```
cd hazync-zkvm/prover
cargo run --release -- prove-block
```
Proves the **block-170 fold** (`chain_step`, `is_base=1`): a STARK receipt attesting block 170 is
valid (real `VerifyScript` + `CheckTransaction` + no-inflation + PoW + merkle + subsidy + weight +
sigops + maturity + locktime + BIP68) and advances the accumulator to `UTXO_root_next`, committing
the `ChainState` journal. Then it **verifies the receipt** against `METHOD_ID` and prints time +
receipt size.

**Expect (CPU):** minutes for block 170 (~2.1M cycles). Bigger/witness-heavy blocks scale with
cycles. GPU (below) is ~10-100×. The receipt is the first real Hazync proof artifact.

## 2. GPU proving (recommended)
RISC0 proves on CUDA GPUs. Add the feature and rebuild:
```
# host/Cargo.toml:  risc0-zkvm = { version = "^3.0.5", features = ["cuda"] }
CUDA_VISIBLE_DEVICES=0 cargo run --release --features cuda -- prove-block
```
Needs an NVIDIA driver + CUDA 12.x. One high-end GPU ≈ 1-10M cycles/s. A busy block (billions of
cycles) needs the block **segmented and proved in parallel** across GPUs, then tree-aggregated
(§2.4) — that's the tip-prover cluster (`HAZYNC_ENGINE.md`), not the single-shot path here.

## 3. Recursive chain proof (`prove-chain`) — enable, then test on the box
The recursion binding (`env::verify(prev proof)` + host `add_assumption`) is the only piece not yet
exercised (it can't be validated in execute — it needs real proving). Enable it as follows, then run
`prove-chain`. **The one iteration point is the journal-byte encoding** — verify it once on the box.

### Guest change (`prover/guest/main.rs`, `chain_step`)
Read the prev proof's **authoritative journal bytes** (not a separate untrusted struct) + this
guest's own image id, verify the assumption, and decode `prev` from those bytes:
```rust
fn chain_step() {
    let prev_journal: Vec<u8> = env::read();   // the prev proof's committed journal bytes
    let w: BlockWitness = env::read();
    let is_base: u32 = env::read();
    let self_id: [u32; 8] = env::read();       // host passes METHOD_ID

    if is_base == 0 {
        // Composition: a receipt with (self_id, prev_journal) must exist — the previous chain proof.
        env::verify(self_id, &prev_journal).expect("prev chain proof invalid");
    }
    // Decode prev ChainState from the authoritative journal bytes (LE words):
    let words: Vec<u32> = prev_journal.chunks(4).map(|c| u32::from_le_bytes(c.try_into().unwrap())).collect();
    let prev: ChainState = risc0_zkvm::serde::from_slice(&words).expect("decode prev");
    /* ...rest unchanged: validate_block, linkage, carry, work, commit new ChainState... */
}
```

### Host change (`prover/host/main.rs`, a `prove_chain()` driver)
```rust
use risc0_zkvm::serde::to_vec;
fn state_journal_bytes(s: &ChainState) -> Vec<u8> {
    to_vec(s).unwrap().iter().flat_map(|w| w.to_le_bytes()).collect()   // == env::commit's journal bytes
}
fn prove_step(prev_journal: Vec<u8>, prev_receipt: Option<Receipt>, w: &BlockWitness, is_base: u32) -> Receipt {
    let mut b = ExecutorEnv::builder();
    if let Some(r) = prev_receipt { b.add_assumption(r); }   // discharge env::verify
    b.write(&2u32).unwrap();
    b.write(&prev_journal).unwrap();
    b.write(w).unwrap();
    b.write(&is_base).unwrap();
    b.write(&METHOD_ID).unwrap();                            // self image id for env::verify
    let receipt = default_prover().prove(b.build().unwrap(), METHOD_ELF).unwrap().receipt;
    receipt.verify(METHOD_ID).unwrap();
    receipt
}
// base 170: prove_step(state_journal_bytes(&anchor), None, &w170, 1)
// fold 171: prove_step(receipt170.journal.bytes.clone(), Some(receipt170), &w171, 0)
// fold 172: prove_step(receipt171.journal.bytes.clone(), Some(receipt171), &w172, 0)
```
`receipt172` is the **chain-tip proof** for 170→172. Verifying it (cheap) proves the whole range
without the blocks. **Iteration point:** confirm `state_journal_bytes(&anchor)` and the guest's
LE-word decode round-trip against a real `receipt.journal.bytes` — if RISC0's journal layout differs
(alignment/padding), adjust the byte↔word conversion on both sides until `env::verify` resolves.

## 4. SNARK wrap (final, for cheap universal verification)
Wrap the tip STARK to Groth16 (~200-300 B, verifiable on a phone / on-chain):
```rust
let snark = default_prover().prove_with_opts(env, ELF, &ProverOpts::groth16()).unwrap().receipt;
```
Needs the RISC0 Groth16 prover deps (x86 Linux; the box qualifies). This receipt is what light
clients / IBD verify.

## 5. Next after 2-3 blocks prove
- Measure real prove time + receipt size per block; project full-block cost.
- The tip-prover cluster: segment a full block, parallel-prove, tree-aggregate, serial fold
  (`HAZYNC_ENGINE.md` §throughput). The EC accelerator (patch 0003 / bigint2-on-real-libsecp) is the
  lever that makes holding the frontier near the tip feasible.

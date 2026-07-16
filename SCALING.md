# Hazync — scaling plan (fold optimisation + parallel backfill)

Future-work record. Captures the scaling analysis and the two known fixes, surfaced *before* review,
plus the GPU ceiling. Empirical core is done: per-input script cost linear ~2.7s/GPU-input (pure
Core, L40S), fan-out ~2×/2 GPUs verified, fold cost measured (below).

## 1. The fold cost is a COMPOSITE-RECEIPT problem (not flat per-chunk)
Two data points don't fit "~10s/chunk":
- block 130000: 2 chunks, 5 inputs/chunk → aggregate ~6.7s → **~3.4s/chunk**
- block 140000: 8 chunks, ~26 inputs/chunk → aggregate ~81s → **~10.1s/chunk**

Per-chunk verify scales with chunk *fatness*, not count. Cause: `default_prover().prove()` returns a
**`CompositeReceipt`** = a list of ~1M-cycle segment receipts. A fat chunk has more segments, and the
aggregation guest's `env::verify` verifies the whole list → cost grows with segment count. Not O(1).
- **Diagnostic to confirm:** a 3rd data point at a different inputs-per-chunk ratio (e.g. rerun 140000
  with `HAZYNC_CHUNKS` high → ~5 inputs/chunk; per-chunk verify should drop toward ~3.4s).

### FIX A — succinct receipts (cheap, high value; probably beats the tree at current scale)  ✅ IMPLEMENTED
Compress each chunk to a **`SuccinctReceipt`** BEFORE folding — `prove_with_opts(env, ELF,
&ProverOpts::succinct())` (or `prover.compress(&ProverOpts::succinct(), &composite)`). RISC0's
lift+join recursion runs **on the GPU** (parallel side), collapsing the segment list to one succinct
STARK. Then every `env::verify` in the aggregation is genuinely **O(1)**, independent of chunk size.
~one-line change to the chunk prover. Sequential fold goes flat + minimal.

**Implemented 2026-07-15** (host `prove-chunk`, `prove-seg` loop, and the final `agg-chunks`/`prove-seg`
aggregate all use `prove_with_opts(..., &ProverOpts::succinct())`). Rationale confirmed by the 741000
run: the ~1645s aggregate was RISC0 lifting all 16 **composite** chunk receipts to succinct *sequentially*
inside the aggregate's resolve step. Proving each chunk to succinct up front moves that lift into the
parallel chunk phase (spread across the GPU fleet), so the aggregate only does a cheap resolve. The
aggregate is now succinct too → each block proof is one fixed-size STARK, directly composable in the
chain range-fold, and saved to `block_<h>.receipt`. Compiles clean (host-only build, cached guest ELF);
**timing win to be measured on the next GPU box** (expected: aggregate collapses from ~1645s to ~tens of
seconds; total wall-clock ~unchanged or slightly up in the chunk phase, but the sequential tail — the
part that blocks the chain fold — goes flat).

## 2. Tree aggregation — constant work, RESTORED parallelism, log tail
Correction to earlier framing: a tree does NOT reduce fold *work* (still N−1 folds). It converts the
fold from **sequential → parallelizable**: all nodes at one tree level fold concurrently across GPUs;
only *depth* is sequential. log₂(140) ≈ 8 levels × ~10–20s ≈ **~2–3 min critical path** vs ~25 min
flat. Combined with FIX A (each node verifies two O(1) receipts), the aggregation layer becomes a
rounding error. This is §2.4's "balanced binary tree over ranges" at the block level.

## 3. Chain-level fold — the 100-day sequential floor (load-bearing for the Bitcoin costing)
The block→chain fold is **~10s of irreducibly-sequential recursion per block**.
- **Tip-following:** nothing (10s / 600s interval).
- **Historical backfill (rolling composition):** 900k blocks × ~10s ≈ **>100 days sequential**, no
  matter how many GPUs chew the scripts. This is the caveat a murchandamus-calibre reviewer spots.
The fix is §2.4's tree-over-ranges applied **at chain level** (below). Surface it in the post as a
known design element, not a discovered flaw.

## 4. Parallel backfill architecture (per-block proofs, then fold)
Per-block proofs are **independent** — block N's proof attests it's internally valid AND transitions
`root_prev(N) → root_next(N)`, depending only on N's data + inclusion proofs, not on other blocks'
*proofs*. So all ~900k blocks prove concurrently across unlimited servers. Pipeline:

1. **Bridge pass** — sequential but CHEAP (hashing only, no proving): replay the whole chain through
   the Utreexo accumulator once (one machine, ~hours–1 day for all of Bitcoin), recording
   `root_prev(N)` for every height + emitting each input's inclusion proof. This is what a Utreexo
   bridge node already does. **The ONLY irreducibly-sequential step — and it's not the expensive one.**
2. **Per-block proofs** — fully parallel, unlimited servers, the GPU-years bulk. Each proves one block
   against its bridge-supplied `root_prev(N)`. Output: succinct per-block proof.
3. **Fold** — **tree over ranges**, pairwise, parallel, log-depth. NOT the rolling chain we have now.

Result: backfill is **parallel end-to-end**; sequential parts are only the bridge hashing pass + the
log-depth fold tail, both small. "Hundreds of GPU-years, fully parallel" — no 100-day floor.

### Code delta from what we have (modest)
- **Per-block proof commits BOTH roots:** `(block_hash, prevhash, root_prev, root_next, cumwork)`.
  Our `ChainState` already commits `root_next`; also commit `root_prev` so folds check adjacency.
- **Pairwise range-fold mode** (variant of the existing `aggregate`): verify two succinct range
  proofs + check boundary — left.`tip_hash` == right.`prevhash`, left.`root_next` == right.`root_prev`,
  `cumwork` sums → emit combined range proof. Recurse in a tree → one `[genesis, tip]` proof, log depth.
- A short chain history folds trivially either way; the Bitcoin-wide backfill needs exactly this.

## 5. GPU ceiling — how far we can push proof-generation speed
- **UpCloud max = 3× L40S** (144 GB VRAM, ~€4.86/hr) — only ~1.5× our current 2-GPU box.
- **8× H100 SXM node** (Lambda / CoreWeave / RunPod / AWS p5) is the practical single-node ceiling,
  and H100 is *much* better for this — RISC0 proving is FFT + Merkle-hash → **memory-bandwidth bound**;
  H100 SXM ~3.35 TB/s vs L40S ~864 GB/s (~4×). So **~3–4×/GPU × 8 ≈ ~25–30× one L40S ≈ ~14× current.**
  A full block: ~2h → single-digit minutes.
- **Beyond one node: no ceiling.** Chunks are independent; the fan-out already writes receipts to
  files → a multi-node version needs only shared receipt storage (S3/object store) + chunk
  distribution. Backfill = hundreds–thousands of GPUs across many nodes.
- **Single-block theoretical floor:** with up to one chunk per input + tree+succinct folding, critical
  path ≈ `(one chunk's prove) + log(chunks)·O(1) fold` → a block proves in ~minutes regardless of size.

## Status / next
- Empirical core done (per-input cost, fan-out, fold cost + scaling story).
- ✅ (a) Full **segwit+taproot** block done (741000, 670 inputs, proven end-to-end — the composite
  aggregate cost 1645s, which *confirmed* the FIX-A diagnosis directly).
- ✅ (c) **FIX A implemented** — succinct chunk + aggregate receipts (host, compiles; timing to measure
  on next box).
- ✅ (d)+(e) **Parallel range-fold DONE** (2026-07-15, commit `c3a5d2f`): guest mode 6 `prove_range`
  (independent per-block proof) + mode 7 `fold_range` (pairwise adjacency-checked merge) + `RangeState`
  committing both boundary contexts; host `bridge_pass`/`prove-range`/`fold-range`/`verify-range` +
  `rangecluster.sh`. Validated on 2×L40S: blocks 1..8 → 8 parallel proofs (~1.2s) → tree fold 4→2→1 →
  genesis-anchored [1..8] receipt VERIFIED in 13s, exact work/leaf counts, block-8 hash matches mainnet.
  This is the parallel backfill: per-block proofs independent (parallel across the fleet), folds
  log-depth. Wall-clock ≈ one block prove + log₂(N) folds, not N sequential steps.
- ✅ **Archive-node bridge DONE** (hazync-during-IBD): a full node emits witnesses during ConnectBlock
  via a guarded `-hazyncwitness`-style hook; validated genesis→199 on real mainnet, witnesses
  byte-identical to the fetcher's, closes S3. (Implemented and validated locally.)
- REMAINING: (b) measure the succinct monolithic aggregate on a box for a composite-vs-succinct number
  (the range-fold already supersedes it for backfill).
- Then the Delving post writes itself: architecture, receipts, honest economics, both scaling fixes
  surfaced pre-emptively.

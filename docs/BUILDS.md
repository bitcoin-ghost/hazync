# The three channels

> ## ⚖ CORE SHIPS (v0.21.0). GHOST DOES NOT, AND CANNOT CONTRIBUTE TO THE BOARD
>
> Three channels, **one canonical guest id** (hazync#225):
>
> | channel | role | contributes to the board? |
> |---|---|---|
> | **stock** | the digest **oracle** — the fidelity floor every acceleration is checked against | no |
> | **CORE** (§2) | **canonical.** What the release binary builds and what the board counts | **yes** |
> | **GHOST** (§3) | experimental — where new levers land first | no |
>
> Stock does not get retired when it stops being the default: it is how any claim that an
> acceleration left consensus untouched is *made*. Core was cleared against it on block 962,000 —
> journal `4fb3e3c5…4656d` byte-identical, 4.095x fewer cycles.
>
> ⛔ **A Ghost build still proves into rejections.** It changes guest source, so it carries a
> different `METHOD_ID`, and the coordinator re-verifies every submission against its own:
>
> ```
> receipt rejected: your prover's guest image id (METHOD_ID) does not match this
> coordinator's — you built a different guest.
> ```
>
> That failure is silent in the worst way: `run-workers.sh` only checks the id at STARTUP
> (hazync#99), so a mismatched worker proves indefinitely into nothing. To contribute to
> hazync.org use the **release binary** or the reproducible build.
>
> ⚠ Promoting a channel is never just a flag. It is a new `METHOD_ID`, a full cutover, and **every
> existing proof invalidated** — v0.21.0 spends the WHOLE board to do it. The speed is not free.
> ⚠ Do not quote a block count here: the board keeps proving until the cutover lands, so any figure
> is stale the moment it is written. Measured 7,852 at 2026-09-07 09:56Z and climbing ~10/min;
> the real cost is whatever `/api/meta` reports the instant the swap happens.
>
> `provision-vps.sh` says the same of the #139 lever it can apply (patch 0005, phase 5b): *"⛔ It
> MOVES METHOD_ID, so a box provisioned with this must never produce a shipped proof"*, and it prints
> *"This box is for benchmarking only."* when it applies it.

⏰ **How the card counts here are made.** Cycle counts and journal digests were first measured in
execute mode on one laptop (block 962,000, single chunk, against a stock control built from the same
tree, 2026-09-01). §1 replaces the derived wall-clock with real GPU proving — but each build's 16
chunks were proved **serially on one card** (`scripts/gpu-benchmark.sh`), so the chunk times, straggler
and aggregate are measured and the **card count is computed from them**, not observed on that many
cards at once. The fleet check of that arithmetic is CORE on 8 × L40S, block 966,108, one chunk per
card: **15m13s measured against 15.2 min projected**, which puts a sub-10-minute block at **~13 L40S**
(`docs/history/BENCH_8xL40S_2026-09-08.md`). That is a different block from §1's, so the two card counts
are not directly comparable.

## 1 · Results — MEASURED ON HARDWARE, 2026-09-02

Two NVIDIA L40S. Block 962,000, 16 chunks, real proving. The chunk times, stragglers and aggregate
below are measured; the card count is computed from them for a 600 s block.

| build | chunk work | straggler | aggregate | **cards** |
|---|---|---|---|---|
| **CORE** | 4,029 s | **1.295** | 473 s | **10** |
| **GHOST** | 1,713 s | **1.438** | 473 s | **5** |

**Every build commits `4fb3e3c5e80417c87584a617d23b53d8c49940348c0e8d455f66299b4bd4656d`** with
`all_valid=1`, `binds=8006` — byte-identical to stock on both boxes, and the whole-block cycle counts
reproduced the laptop's figures exactly (GHOST 1,198,904,653 to the cycle).

### ⛔ Every derived figure moved the WRONG way when measured

| | derived | measured | |
|---|---|---|---|
| CORE cards | 9 | **10** | chunk work 3,521 → 4,029 s (+14%) |
| GHOST cards | 5 | **5** | chunk work 1,257 → 1,713 s (+36%) |
| CORE straggler | 1.210 | 1.295 | predicted well |
| GHOST straggler | 1.469 | 1.438 | predicted well |
| aggregate | 616 s *(fitted)* | 473 s | moved TOWARD us |

**The stragglers predicted within a few percent. The cycles→wall-clock conversion did not** — it was
optimistic by 14% for Core and 36% for Ghost. Cycle ratios are a good proxy for the SHAPE of a
distribution and a poor one for its absolute cost.

### ⏰ The aggregate: run a worker on the coordinator

```
1 remote worker, coordinator IDLE            772.4 s
1 remote worker + coordinator ALSO working   473.1 s   <- 1.63x, ZERO extra hardware
2 remote workers, coordinator idle           405.6 s   <- 1.90x
```

**`seg-serve` distributes but does not prove.** With one worker the coordinator's GPU sits at **0%
while a whole card does nothing.** Attaching a worker to it is free and is worth **a card on both
builds**. This required no code change.

⏰ **hazync#207 should be closed as ALREADY FIXED, not implemented.** Its case is that workers idle
through a serial execute it estimates at ~107 s; measured, that phase is **13.2 s**, almost certainly
fixed by `read_slice` (#136) landing after the N=2 ceiling was measured. The aggregate now scales
**1.90x on two workers**. Implementing its `run_with_callback` restructure would have rewritten a
function that has already caused two deadlocks, to fix something that is not broken.

⚠ **CORE's aggregate is ASSUMED equal to GHOST's**, not measured. The aggregate is receipt recursion
— 16 receipts, 323 segments at po2 21 — and that work is the same shape whichever build produced the
receipts; GHOST measured 12.9–13.2 s execute and 323 segments across three runs. Worth confirming if
Core's number ever becomes load-bearing.

### #119, on an L40S

**1 occurrence in 16 proves**, recovered on the first retry (81 s). Against 5-in-293 previously. Small
sample, but it is the first L40S data on an open, unexplained fault, and without a retry that single
occurrence costs the whole aggregate — `agg-chunks` needs all 16 receipts.

> ✅ **Fixed in v0.21.1 (#245).** The cause was a non-canonical witness cell from rv32im accum phase 3,
> at a measured ~1 in 2,200 po2=21 segments — not the card. Full chain in hazync#119.

## 2 · CORE — *Core's own code decides*

```bash
# patches (secp256k1 tree)
0012-select-field-bigint2-backend.patch
0013-lift-x-via-witness-hint.patch
# build
HAZYNC_FIELD_BIGINT2=1 HAZYNC_LIFTX_HINT=1 HAZYNC_ECMULT_WINDOW=21 cargo build --release
# packing constants -- Core's per-curve fit, BUILT IN since v0.21.0 (c12ad67); shown for reference
HAZYNC_COST_EC_OP=417798 HAZYNC_COST_SCHNORR_OP=462435
HAZYNC_COST_INPUT_BYTE=2 HAZYNC_COST_INPUT_BASE=41387
# run time -- every chunked command (prove-chunk, prove-seg, seg-serve) also needs this
HAZYNC_LIFTX_HINT=1
```

⏰ **The constants above are the PER-CURVE fit (2026-09-01), not the earlier curve-blind one.**
Separating ECDSA from Schnorr measured a ratio of **1.11x** where the earlier constants assumed
exactly 1.00x, and took the straggler **1.311 -> 1.210** — 8.89 to 8.28 cards. Eight cards needs
1.163; this is 3.4% over that line.

⚠ Core's cost model fits its own data at **6.6%** mean error against Ghost's **15.2%**. That is why
the same refit helps Core and hurts Ghost: Core's cost really is close to linear in
`(ecdsa, schnorr, bytes, inputs)`, and Ghost's is not.

libsecp keeps its wNAF, its GLV, its ECDSA logic and every check. Two changes beneath it: the field
*backend* (an interface libsecp already parameterises for its own use) and a pubkey-Y hint that
**libsecp's own `fe_sqr`/`fe_equal` verify before accepting** — a wrong or missing hint falls back to
its real sqrt.

## 3 · GHOST — *fastest wins*

```bash
# patches (secp256k1)          # patches (bitcoin-core)
0005-ecdsa-verify-group-arith-via-bigint2.patch   0009-sha-transform-fastpath.patch
0006-schnorr-verify-group-arith-via-bigint2.patch 0010-transformd64-via-accelerator.patch
0008-scalar-inverse-via-bigint2.patch
0012-select-field-bigint2-backend.patch
0013-lift-x-via-witness-hint.patch
# build
HAZYNC_BIGINT2_ECDSA=1 HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_BIGINT2_SCHNORR=1 \
HAZYNC_SCALAR_INV_ACCEL=1 HAZYNC_SHA_FASTPATH=1 HAZYNC_AGG_READSLICE=1 HAZYNC_SHA_D64_ACCEL=1 \
HAZYNC_ECMULT_WINDOW=21 cargo build --release
# packing constants -- Ghost's OWN fit (#227). The built-in defaults are Core's; do not use them here
HAZYNC_COST_EC_OP=85636  HAZYNC_COST_SCHNORR_OP=168542
HAZYNC_COST_INPUT_BYTE=2 HAZYNC_COST_INPUT_BASE=53162
```

⏰ **Since v0.21.0 the built-in constants ARE Core's per-curve fit** (§2). They are global consts in
`prover/host/src/main.rs`, each overridable by its `HAZYNC_COST_*` variable — there is no per-channel
default, so Ghost's fit is something a Ghost run sets in its own environment. Before `c12ad67` the
defaults were the older curve-blind constants, which is what "defaults" means in the Ghost
measurements below and in §1.

✅ **Ghost's calibration — MEASURED 2026-09-05** (`af0534c`, #227): two L40S, real GPU proving on block
962,000, 16 chunks, one arm per box, differing only in the four `HAZYNC_COST_*` values. Fitted on block
965,500 (7.7% Schnorr), intercept forced to zero; Schnorr:ECDSA **1.97x**, not the 13.77x Ghost had
been rescaled by — the hint removes decompression from *both* curves.

| arm | straggler | max | mean |
|---|---|---|---|
| defaults at the time (pre-`c12ad67`) | 1.462x | 149 s | 102 s |
| Ghost refit | **1.189x** | 124 s | 104 s |

§1's separate run measured 1.438 for Ghost at those same defaults. ⚠ Neither fit transfers: Core's
refit moved Ghost's straggler the wrong way in the earlier derived, cycle-based comparison
(1.407 → 1.884), and Ghost's constants have not been measured on Core.

⚠ **Re-run an outlier before believing it.** The refit first measured 1.567x — worse than doing nothing
— from one chunk at 4.89 s/segment, when 31 of 32 chunk timings sat at 2.4–3.0 s/segment. Re-run twice:
94 s both times, 2.69 s/segment. The segment straggler (57 → 49 max segments) had already said the
opposite; prefer it, since proving bills whole segments.

✅ **#139 and the field backend are orthogonal and stack**: 10.676x → 11.467x in execute-mode cycles,
+7.4%. #139 replaces `secp256k1_ecmult`; the field backend replaces the representation *underneath
everything else*.

### 3.1 · Ghost's next build — hazync#209

> ⚖ **#209 was closed on 2026-09-16 as superseded.** Core ships and is what the board counts; a Ghost build
> changes guest source, so it carries a different `METHOD_ID` and proves into rejections (top of this file).
> The levers below stay as the record of what was sized and why. They become shippable only on a re-baseline.

Ghost measured **5 cards** on block 962,000 (§1); a fourth needs a straggler ≤ 1.35. The refit above
meets that, but **the card count was not re-derived** at 1.189 — read it as "the straggler target is
met", not as four cards.

- **What pointed at the packer** (2026-09-05, #139 arm, block 741000): chunks 1 and 11 carry the same
  42 inputs and 42 EC verifies and differ only in bytes (765,282 vs 37,933). They measured 1.293x apart
  against a modelled 1.574x, because the byte term then in force (6 per byte) was ~2x the measured 3.13
  cycles/byte. Both fits now use 2.
- **Remaining levers**, as #209 listed them once Ghost was defined as "fastest wins": MSM batch
  verification (rejected before on fidelity, sized at one card; `docs/history/MSM_BATCH_VERIFY.md`);
  wholesale bigint2, which needs measuring from scratch because the recorded "15% faster" was never
  produced by a run (nothing called `hazync_ecdsa_verify_full` until `patches/0014`, `9b767b5`); and
  re-measuring the G3 Schnorr lane and the scalar inverse, both sized before the hint landed. The
  aggregate — 473.1 s with the coordinator working, 405.6 s on two workers (§1) — is the other
  candidate for a card.

## 4 · What is still NOT measured

1. **CORE's aggregate on block 962,000.** §1 assumes it equals Ghost's 473 s. CORE's aggregate has been
   measured on other blocks since — 203.8 s over 8 workers on 966,108
   (`docs/history/BENCH_8xL40S_2026-09-08.md`), 241.2 s over 27 on 966,256
   (`docs/history/MILESTONE_966256_RUN4_2026-09-10.md`) — but not on 962,000.
2. **The straggler above 8 chunks on a fleet.** Only N=8 has run one chunk per card; the ~13-L40S figure
   assumes the straggler does not worsen past that.
3. **Ghost's card count at its refit constants** (§3.1).

## 5 · Reproducing

```bash
scripts/field-backend-tests.sh                     # correctness gates, no GPU, ~3 min
HAZYNC_BLOCK=<block_962000.json> HAZYNC_CHUNKS=1 HAZYNC_PROFILE_EXEC=1 \
  ./target/release/host chunk-profile               # cycles + journal digest
HAZYNC_CHUNKS=16 ...                                # per-chunk cycles + measured straggler
```

⚠ `chunk-profile` executes the block **twice** (count-packed and cost-packed); budget accordingly.
⚠ The straggler is now reported on **measured cycles** as well as predicted. Only read the measured
one: the cost packer balances its own predictor by construction and will report a perfect 1.00x
however wrong that predictor is.

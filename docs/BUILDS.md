# The two builds

⏰ **Pinned 2026-09-01** against `feat/liftx-accel`. Everything here was measured in **execute mode
on one laptop** — CPU only, no GPU — on block 962,000, single chunk, against a stock control built
from the same tree. ⛔ **Wall-clock and therefore card counts are DERIVED, not measured.** Section 4
says exactly what a GPU run would settle.

## 1 · Results — MEASURED ON HARDWARE, 2026-09-02

Two NVIDIA L40S. Block 962,000, 16 chunks, real proving. **Every card count below is measured, not
derived**, which is what §4 of the previous revision said was outstanding.

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

## 2 · CORE — *Core's own code decides*

```bash
# patches (secp256k1 tree)
0012-select-field-bigint2-backend.patch
0013-lift-x-via-witness-hint.patch
# build
HAZYNC_FIELD_BIGINT2=1 HAZYNC_LIFTX_HINT=1 HAZYNC_ECMULT_WINDOW=21 cargo build --release
# packing constants -- REFITTED, and they matter: 1.557 -> 1.311 straggler
HAZYNC_COST_EC_OP=417798 HAZYNC_COST_SCHNORR_OP=462435
HAZYNC_COST_INPUT_BYTE=2 HAZYNC_COST_INPUT_BASE=41387
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
# packing constants -- DEFAULTS. Do NOT apply Core's refit here.
```

⛔ **The same refit helps Core and hurts Ghost.** Core 1.557 → 1.311; Ghost 1.407 → **1.884**, and
1.996 on GHOST. It costs a card. The likely cause is rescaling Ghost's Schnorr constant by the old
13.77x ECDSA ratio, when the hint removes decompression from *both* curves — taproot key-path spends
call `lift_x` too — so that ratio should have narrowed. **Ghost needs its own calibration; it does
not have one.**

✅ **#139 and the field backend are orthogonal and stack**: 10.676x → 11.467x, +7.4%. They accelerate
different things — #139 replaces `secp256k1_ecmult`; the field backend replaces the representation
*underneath everything else*.

## 4 · ⛔ What is NOT measured

0. ⏰ **How many GPUs.** **Two** measures both builds' 16-chunk proving in parallel and turns every
   derived card count into a measured one. **Four** is required for the aggregate: issue #207's
   ceiling is a table at 1, 2 and 4 workers, and an N=2 saturation cannot be reproduced or fixed with
   two boxes. The aggregate is 25% of Ghost's budget and 12% of Core's — it is where the remaining
   cards are. `scripts/gpu-benchmark.sh core|ghost` runs it.
1. **Wall-clock, on a GPU.** Every card count here converts cycles at a fixed ratio. Proving time is
   quantised by segment (`ceil(cycles / 2^po2)`), so the real straggler can differ from the cycle
   straggler. **This is the one thing that needs hardware.**
2. **GHOST's straggler at default constants** — assumed 1.407 from the no-backend arm, never
   measured for the backend arm. ⏰ It decides 5 cards vs 4: at 1.35 GHOST is **four cards**.
3. **Aggregate** — both modes assume 627 s and a 1.881x two-worker split, measured previously.

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

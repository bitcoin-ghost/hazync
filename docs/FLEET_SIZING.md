# Fleet sizing: how many cards does a tip block need?

Scope: how many GPUs it takes to prove one tip block inside Bitcoin's ~10-minute interval, which is the
threshold for tracking the chain rather than falling behind it.

**This is an ESTIMATE built on one measured throughput figure, one measured aggregate, and a modelled
cycle count. Nobody has run more than TWO workers.** Everything past N=2 is extrapolation, and the
sections below say which parts are which. Read §4 before spending money on it.

## 1. The inputs

| quantity | value | status |
|---|---|---|
| L40S throughput | **978,435 cyc/s** | MEASURED (2026-08-18) |
| H100 throughput | 926,349 cyc/s | MEASURED — 3.9x the HBM bandwidth, **0.95x** the throughput |
| B200 | 8.7% slower than L40S | MEASURED (#178/#180), even with native `sm_100` |
| L4 (cheap tier) | **37% more expensive** per proof | MEASURED (#180) |
| block 962,000 cycles, post-#136/#137 | **14.34 G** | **MODELLED** from #138's coefficients |
| aggregate, L40S, canonical guest | **1,565.9 s** | MEASURED (#180, v0.19.0 signed binary) |
| non-divisible portion | **~83 s** | INFERRED — see §3 |

⚠ **Use cycles/sec, never wall-clock, when comparing across cards.** Segment counts and wall times are
not comparable across guest baselines: the L40S chunk figure of 1,779 s predates #136/#137 and so
carries roughly twice the cycles of today's guest. Dividing with it gives ~50 cards and is wrong.
`ACCELERATION.md` says the same thing and it is easy to miss.

## 2. One card

```
chunks     14.057e9 / 978,435 cyc/s  = 14,367 s   (MEASURED 2026-08-27, was 14,656 modelled)
aggregate  measured                  =  1,566 s
                                       ────────
total                                 ≈ 15,933 s  ≈ 4.4 GPU-hours per block
```

So **a single L40S proves a tip block in about four and a half hours.**

## 3. N cards, with the floor that does not divide

Naive division gives `16,222 / 28 = 579 s = 9.7 min`, and that is the number to stop quoting. It
assumes everything parallelises. It does not.

The aggregate's execution/preflight and its assembly critical path run **once per block**, whatever the
fleet size. From the v0.19.0 release notes, on one coordinator and two workers: *"375 segments in 789 s
of worker wall, **assembly 55.7 s** of which resolves 4.9 s"*, plus ~27 s of aggregate execution.

    wall(N) ≈ 83 + (16,222 − 83) / N

| cards | wall | under 10 min? |
|---|---|---|
| 1 | 4.5 h | |
| 16 | 17.7 min | ✗ |
| 24 | 12.6 min | ✗ |
| **28** | **11.0 min** | **✗ — just short** |
| **32** | **9.8 min** | **✓** |
| 40 | 8.1 min | ✓ |
| 64 | 5.6 min | ✓ |
| ∞ | **1.4 min** | the hard floor |

**~32 L40S is the 10-minute number. 28 lands at about 11 minutes.**

At ~€1.13/card/hour, 32 cards is ~€36/hour, or **~€6 per block** at six blocks an hour.

⚠ **Amdahl is the point, not a footnote.** The whole achievement of #148/#153/#156/#157/#158 was
shrinking the serial section from **>55 min** — where a 10-minute block was impossible at ANY fleet
size — to ~1.4 min. Shrinking is not eliminating: at a 600-second target, an 83-second floor is 14% of
the budget, and it is why the card count is 32 rather than 28.

## 4. Why not to buy 32 cards on this page alone

Four things could each move the answer, in rough order of how much:

1. ~~**Coordinator egress may bind first.**~~ ✅ **MEASURED 2026-08-26 — RETIRED.** It is **~1.3 GB per
   block, about 18 Mbps** sustained, not the ~26 GB / ~320 Mbps this section estimated. Egress does not
   cap the fleet at any plausible size. See §6.
2. **The 83 s floor is inferred from a two-worker run**, not measured as a floor. With 32 workers the
   assembly critical path could shorten (more resolves distributed) or lengthen (more coordination).
   It is the difference between 28 and 32 cards.
3. ~~**14.34 G is modelled**, not measured.~~ ✅ **MEASURED 2026-08-27 — RETIRED.** `HAZYNC_PROFILE_EXEC=1
   chunk-profile` on block 962,000, production cost-packed partition, gives **14.057 G** — the model was
   **+2.0%** high. Chunk work on one L40S is **14,367 s**, not 14,656 s, and the one-card total is
   **15,933 s**. Needed no GPU and no chunk receipts. See `SIXTEEN_CARD_PLAN.md` §2.1, which also records
   that the measured straggler is **1.059x** where the packer's own metric — computed from predictions —
   reports 1.00x.
4. **Nothing above N=2 has ever been run.** Near-linear scaling is plausible -- chunks are independent
   and every proof is verified separately -- but it is an assumption, not a result.

**What would settle it:** a real scaling run at N = 2, 4, 8 (E5 is now done — see §6). Three points on the curve
distinguish "linear" from "saturating" long before anyone commits to a fleet, and the gap between those
two outcomes is worth far more than the ~€7/hour between 28 and 32 cards.

## 5. Which card, and why not the others

**L40S**, on measurement rather than preference:

- **H100** has 3.9x the memory bandwidth and returns **0.95x** the throughput. The bandwidth model for
  this workload is dead; the run is host-bound, and bandwidth cannot fill an idle gap.
- **B200** is 8.7% slower than the L40S at ~3x the power, even after native `sm_100` removed the JIT.
- **L4** is the cheap tier and comes out **37% more expensive per proof**: assembly scales with segment
  count, and the VRAM ladder's wall penalty hides it.

The card axis is closed. What is open is host-side scheduling (worker processes per card, experiment
**E6**) and the guest's cycle count -- see `PERF_INVESTIGATION_2026-08-26.md`.


## 6. Coordinator egress, measured — the risk is retired

§4 originally listed egress as the **largest** reason not to trust the card count. It was an estimate,
it was wrong by ~20x, and it is now measured.

Segment production needs **no GPU**: `seg-serve` runs `ExecutorImpl::run()` and `bincode::serialize`,
then prints its totals *before* it starts listening. So this was measurable on a laptop all along, and
filing it as Tier 1 "needs one card" was a mistake.

**MEASURED**, block 962,000, four chunks each at two segment sizes:

| po2 | segments/chunk | MB/chunk | KB/segment |
|---|---|---|---|
| 20 | 844–913 | 118.7–135.8 | 141–149 |
| **22** | **202–218** | **55.3–62.0** | **273–284** |

Chunk-to-chunk spread at po2 22 is ~4%, so cost-packing is working and a mean extrapolates safely.

| component | per block | basis |
|---|---|---|
| 16 chunks @ po2 22 | **~949 MB** | MEASURED |
| aggregate @ po2 22 | ~390 MB | ESTIMATE — see below |
| **total** | **~1.34 GB** | |
| **sustained for a 600 s block** | **~2.2 MB/s ≈ 18 Mbps** | |

⚠ The aggregate figure is an **estimate**, not a measurement. The aggregate consumes chunk RECEIPTS,
which only exist after proving, so it cannot be measured without a GPU. It is scaled from the one
recorded `seg-serve` line for an aggregate — `186 segments, 261.9 MB` at po2 23, i.e. **1.41 MB/segment**
— using the po2 slope the chunk table above measures (~1.48x more total bytes per step down). Even if
that estimate is off by 3x, egress remains under 60 Mbps.

**18 Mbps does not cap anything.** Two consequences:

- Egress is removed as a fleet-planning risk. What remains uncertain about the card count is the
  **83 s serial floor** (§3) and the fact that **nothing above N=2 has ever been run** — not bandwidth.
- **Wire compression is pointless.** `PERF_INVESTIGATION_2026-08-26.md` §5.1 proposed it as the lever
  if egress bound. It does not bind, so that closes too.

⚠ **How the estimate went wrong, because both halves are traps this repo has already documented.**
The ~26 GB figure multiplied two errors: a segment size of ~4 MB inferred from the push-budget default
(real chunk segments are 273–284 KB; the ~4 MB figure belongs to the AGGREGATE, whose segments carry
chunk receipts as assumptions), and a count of 415 segments/chunk, which is the **pre-#136/#137**
number — those changes halved the cycles and so halved the segments to ~213. This file's own §1 warns
against exactly that second mistake.

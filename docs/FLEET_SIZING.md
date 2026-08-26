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
chunks     14.34e9 / 978,435 cyc/s  ≈ 14,656 s
aggregate  measured                 =  1,566 s
                                      ────────
total                                ≈ 16,222 s  ≈ 4.5 GPU-hours per block
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

1. **Coordinator egress is uncosted and may bind first.** The coordinator pushes every segment: ~6,600
   per block at up to ~4 MB, an ESTIMATED **~26 GB per block from a single host**, or ~320 Mbps
   sustained to hold 10 minutes. If the link saturates around 15 workers the fleet caps there and card
   count stops mattering. This is experiment **E5** and it should run first.
2. **The 83 s floor is inferred from a two-worker run**, not measured as a floor. With 32 workers the
   assembly critical path could shorten (more resolves distributed) or lengthen (more coordination).
   It is the difference between 28 and 32 cards.
3. **14.34 G is modelled**, not measured. It carries two independent cross-checks, but it is not a
   direct count of today's guest on today's block.
4. **Nothing above N=2 has ever been run.** Near-linear scaling is plausible -- chunks are independent
   and every proof is verified separately -- but it is an assumption, not a result.

**What would settle it:** E5, plus a real scaling run at N = 2, 4, 8. Three points on the curve
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

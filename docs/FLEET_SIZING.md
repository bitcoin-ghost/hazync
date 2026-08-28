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

⚠ **That is the LATENCY number — one block, start to finish, inside 600 s.** `GOALS.md` quotes
**~29 L40S** for the same 600 s target and is not in conflict: it prices **throughput** — keeping up
with the chain at a bounded lag, where consecutive blocks overlap. The two framings differ by more
than three cards, because under throughput the aggregate never has to distribute (per-block aggregates
run concurrently on different blocks), so caveat 5 below does not apply to it. **Always say which
framing, and which po2, a fleet size assumes.**

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
2. ~~**The 83 s floor is inferred from a two-worker run**~~ ⚠ **PARTLY ANSWERED 2026-08-27, and the
   shape was wrong.** N=4 and N=8 were run on one box, one card, one binary, with only N moving: the
   aggregate went **115.8 s → 116.6 s, +0.7%**. It is not a small additive floor that grows with
   coordination — it is the **whole aggregate**, ~1,575 s on block 962,000, and it is **constant in N**.
   ⛔ What is still unmeasured is whether that aggregate **distributes across cards** — see the new
   caveat below, which this page's arithmetic silently assumes.
3. ~~**14.34 G is modelled**, not measured.~~ ✅ **MEASURED 2026-08-27: 14.057 G.** The model was
   **+2.0% high**. This reason is retired.
4. ~~**Nothing above N=2 has ever been run.**~~ ✅ **N=4 and N=8 RUN 2026-08-27.** Chunk work scales
   near-perfectly — 1,511 vs 1,525 card-seconds across the two arms, ~1% apart, with a cross-box
   control. Near-linear scaling is now a result, not an assumption.

~~**What would settle it:** a real scaling run at N = 2, 4, 8~~ ✅ **That run happened on 2026-08-27**
(E5 was already done — see §6). The curve is linear on the chunk side and flat on the aggregate side.

⛔ **5. THE ONE THIS PAGE DOES NOT LIST, AND IT IS THE LARGEST.** The `wall(N)` model above divides the
**entire** 16,222 s — aggregate included — by `N`, leaving only 83 s undivided. That assumes the
aggregate **fully distributes across cards**, which is claimed by #153/#157/#161 and **has never been
exercised**. It needs >= 2 boxes, not one, so none of the 2026-08-27 work touched it.

If it does not distribute, the same measured inputs give a very different answer:

| | 32 cards | 48 cards |
|---|---|---|
| aggregate fully distributes (this page's assumption) | **9.0 min** ✅ | 6.0 min ✅ |
| only its segments distribute, resolution serial | 12.2 min | **9.2 min** ✅ |
| nothing distributes | **34 min** ❌ | 31 min ❌ |

⇒ **"~32 L40S" is conditional on an unexercised claim.** Do not quote it without saying so.

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

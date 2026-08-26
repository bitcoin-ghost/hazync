# The sixteen-card plan — one denominator, and the layer nobody has measured

**Target, stated as a number instead of an aspiration: prove a near-tip block on 16 L40S.**

This document exists because the board has many levers quoted in incompatible units — execute-mode
cycles, one-card wall-clock, per-chunk speedups, block-level bounds — and no shared denominator in
which they compose. That is why the work has felt like shooting in the dark, and it is why the same
ideas keep being re-proposed and re-retracted. A lever that cannot be priced against the target is not
a lever, it is an opinion.

Everything below is labelled MEASURED / FROM CODE / ESTIMATE / UNKNOWN, per
`PERF_INVESTIGATION_2026-08-26.md`'s convention.

---

## 1. The denominator

Every lever gets exactly one price: **its factor on divisible card-seconds per block**, plus its
effect on **the serial floor**, plus its **fidelity cost**, plus whether it **moves `METHOD_ID`**.
Four columns, no exceptions. Anything quoted in other units gets converted before it is discussed.

From `FLEET_SIZING.md` (MEASURED throughput, MEASURED aggregate, MODELLED cycle count):

```
one card   chunks 14,656 s  +  aggregate 1,566 s  =  16,222 s
floor      ~83 s does not divide  (INFERRED from a two-worker run)
wall(N)    83 + (16,222 - 83) / N
```

**Two different bars, and they are not the same number.**

| framing | what it means | bar at N=16 |
|---|---|---|
| **latency** | one block, start to finish, inside 600 s | **1.95x** on divisible work |
| **throughput** | keep up with the chain at a fixed lag | **1.69x** — `16,222 / (16 x 600)` |

Today: `wall(16) = 83 + 16,139/16 = 1,092 s = 18.2 min`.

⚠ **The floor is the hidden multiplier on the difficulty.** At an 83 s floor the bar is 1.95x. If
coordination at N=16 pushes the floor to 150 s the bar becomes 2.24x; at 300 s it becomes 3.36x.
**Nothing above N=2 has ever been run**, so the floor at 16 workers is UNKNOWN and it is worth more
than any micro-optimisation on this page. Measure it before trusting any row below.

### 1.1 The throughput framing is cheaper, and nothing in the design forbids it

FROM CODE: `prove_chunk` takes no previous receipt. The chain dependency is `add_assumption(prev)` in
`prove_step` / `prove_step_succinct` (`main.rs:396, 1024`) — i.e. **only the per-block fold is
sequential; chunk proving of block h+1 does not wait on block h's proof.**

So a fleet may run blocks overlapped, at a bounded lag, and the 83 s floors of consecutive blocks
overlap with the next block's chunk work. That converts the latency bar into the throughput bar and
**drops the requirement from 1.95x to 1.69x for free** — at the cost of always being a block or two
behind the tip, which for a proving system that tracks the chain is a design choice, not a defect.

This has never been written down as an option. It should be priced before anyone buys a card.

---

## 2. The scoreboard, and the arithmetic that follows from it

| lever | factor on divisible work | floor | fidelity | new `METHOD_ID`? | status |
|---|---|---|---|---|---|
| Tier 0 guest codegen (`-O3`, rust LTO+CGU=1, window 20) | **1.012x** | — | none | **yes** | MEASURED (`TIER0_RESULTS_2026-08-26.md`) |
| Worker processes per card (E6) | **≤1.09x** | — | none | no | see §3 — ceiling corrected downward |
| Preflight overlap (E9, risc0#3201 fork) | **≤1.09x** | — | none | no | see §3 — **should now close** |
| More cards | — (latency only) | — | none | no | not a cost lever at all |
| **#139 middle path** | 5.25x | — | **~85% of Core** | yes | measured execute-derived |
| **#139 wholesale + type-aware packer** | **6.95x** | — | **~95% of Core** | yes | 7.18x MEASURED at proving time |
| Bounded-lag pipelining (§1.1) | 1.15x-equivalent (bar 1.95x → 1.69x) | **removes it** | none | no | UNPRICED — free to settle |

**The conclusion is arithmetic, not judgement:**

```
all known zero-fidelity levers, multiplied:  1.012 x 1.09  =  1.106x
wall(16) after all of them:  83 + 14,595/16  =  995 s  =  16.6 min
target:                                          600 s  =  10.0 min
```

⛔ **16 cards is not reachable on the known zero-fidelity levers. They take 18.2 min to 16.6 min,
against a 10.0 min target.** Every remaining item on the board, added together and all landing
perfectly, closes about a fifth of the gap in seconds and none of it in fidelity terms.

That leaves exactly three routes, and they are not alternatives to each other — they are a decision,
a measurement, and a search:

1. **Take a fidelity trade (#139).** Even the *middle* path overshoots: `wall(16) = 350 s = 5.8 min`.
   Wholesale with the type-aware packer gives 307 s, and reaches 600 s on **~7 cards**.
2. **Take the free 1.15x-equivalent** by accepting bounded lag (§1.1) — real, but not sufficient alone.
3. **Find a lever class nobody has enumerated.** §4 argues there is one, and names where it is.

⚠ **The fidelity decision has more slack than the board presents.** The target needs 1.95x; the
smallest #139 variant delivers 5.25x. Nobody has asked *what the smallest concession worth 1.95x looks
like* — the board only prices the two variants that happen to exist. That question is open and it is
the right one to ask before spending 85% of Core.

---

## 3. Correction, MEASURED tonight: the GPU is not 65% idle

`PERF_INVESTIGATION_2026-08-26.md` §4.2 states *"the GPU is **65% idle** waiting on one host thread"*,
and E6/E9 — including a proposed **fork** of risc0 for its GPU locking — are motivated by filling that
idle time. §1 of the same document says the opposite, *"the GPU is already 65% busy, so perfect
scheduling is worth at most ~1.5x"*. Both cannot be right, and they imply ceilings a factor of two apart.

**MEASURED, `hazync-l40s4`, E7, two full po2-22 chunk proves, `nvidia-smi` sampled once per second**
(`~/hazync-l40s-evidence-2026-08-26/x/e7v.*.22`):

| arm | samples | mean util | util = 0 | util < 50% | util >= 90% |
|---|---|---|---|---|---|
| baseline po2 22 | 389 s | **91.7%** | 9 s (2.3%) | 7.7% | 85.1% |
| patched po2 22 | 390 s | **91.5%** | 9 s (2.3%) | 6.4% | 82.3% |

The zeros are the first **8 seconds** of each run — execute, before proving starts. After that the
card is essentially never idle.

**Consequences:**

- **E6's ceiling is ~1.09x, not 1.20-1.45x.** There is no 65% of idle time to fill; there is 8.5%.
- ⛔ **E9 should close.** It carries a CUDA deadlock we have already hit once (#147/#148) and would
  now be a *fork* of a dormant upstream (`PERF_INVESTIGATION` §E10), to chase at most 1.09x. That is
  not a trade worth making. It was gated on E6; it is now closed by measurement instead.
- The "65% idle" figure's actual evidence is `vmstat` showing **0.86 busy cores** — which demonstrates
  the *host* is single-threaded. It says nothing about the GPU, and it was read as if it did.

⚠ **The caveat that matters, and it is the whole reason for §4.** `nvidia-smi utilization.gpu` reports
the fraction of time at least one kernel was *resident*. It is not occupancy, not achieved bandwidth,
not arithmetic throughput. A card can read 100% "utilised" while delivering a few per cent of its
capability. **So this measurement kills the idle-gap framing and says nothing at all about efficiency
inside the kernels.** Those are different questions, and only one of them has ever been asked.

---

## 4. The loudest unexplained signal on the board

Three MEASURED results that the board files separately as closed doors:

| | vs L40S |
|---|---|
| **H100** — 3.9x the memory bandwidth | **0.95x** the throughput |
| **B200** — newer architecture, native `sm_100`, ~3x the power | **0.91x** |
| **L4** — cheap tier | 37% more expensive per proof |

`FLEET_SIZING.md` §5 concludes *"the card axis is closed"*. That is the correct operational conclusion
and the wrong diagnostic one. **Performance that is invariant to a 4x change in memory bandwidth and to
two architecture generations is not a fact about cards. It is a fact about our kernels.** A workload
that scaled with the hardware would not do this. Something else is the binding constraint, and the
board has never named it because **the layer it lives in has never been measured**.

FROM CODE, and checked rather than assumed: `grep -riE "nsight|ncu|nsys|occupancy"` across the repo
returns **nothing**. Every profile ever taken here is either guest cycles (`HAZYNC_PROFILE_EXEC`) or
wall-clock. **Nobody has ever looked inside a CUDA kernel on this project.**

### 4.1 And that layer is exempt from the constraint that orders everything else

`ACCELERATION.md`: *"There is exactly one pinned id... every past re-baseline reset the board to
genesis. There have been six."* The `METHOD_ID` cost is what makes the cycles axis expensive and what
forces #136, #137 and #139 to be batched.

**Kernel-level work does not move `METHOD_ID` and costs no fidelity.** It changes how a proof is
computed, not what is proven. And #182 has just demonstrated that we can vendor and patch `risc0-sys`
successfully, against an upstream that `PERF_INVESTIGATION` §E10 establishes is **dormant** — five
housekeeping commits in three months, our two issues unanswered.

So: risc0's CUDA kernels are effectively ours, we have the mechanism to change them, changing them is
free of every constraint that makes the other axes expensive, and **we have never looked at them.**
That is the unexplored lever class, and it is where the three-architecture invariance points.

---

## 5. The method: four layers that must reconcile to the same 399 seconds

This is the answer to "how do we know where to look". Not a longer list of ideas — a profile that
**accounts for the same wall-clock at every level of the stack**. If the layers do not sum to the same
number, the profile is wrong, and no lever derived from it can be trusted. That reconciliation is the
guard; it is what we have never had, and its absence is exactly how a stale lever survived five days
and a bandwidth estimate came out 20x wrong.

Workload for every layer: **one po2-22 chunk prove, block 962,000, chunk 9** — the chunk already
verified to profile +0.3% off the sixteen-chunk mean, so it is a fair sample. Wall to account for:
**~399 s**, of which **8 s is execute** (MEASURED, §3).

| layer | the question it answers | tool | what it can see | what it CANNOT see |
|---|---|---|---|---|
| **L1 guest** | what are we proving | `HAZYNC_PROFILE_EXEC=1`, `chunk-profile` | cycles by function | anything about proving cost |
| **L2 risc0 phases** | where the 399 s goes *inside* a prove | `RUST_LOG=risc0_zkvm=debug` + our own span timers | execute / preflight / witgen / NTT / hash / merkle split | why any phase is slow |
| **L3 host** | is the host feeding the card or blocking it | `perf record` + flamegraph on the prove | serial host sections, syscall stalls, the single-thread claim | GPU-side efficiency |
| **L4 device** | when the GPU says 100%, what is it *doing* | **`nsys`** (launch gaps, stream serialisation) + **`ncu`** (occupancy, achieved bandwidth, instruction mix) | the invariance in §4 | what is worth proving |

**L1 is done and exhausted** — 1.16% left, `TIER0_RESULTS_2026-08-26.md`.
**L2 and L3 have never been run.** **L4 has never been run and is the one that matters.**

### 5.1 The four hypotheses L4 separates, and what each would mean

The §4 invariance has exactly four candidate explanations. They are distinguishable in one `ncu` run.

| # | hypothesis | signature | if true |
|---|---|---|---|
| **H1** | **launch-bound** — many short kernels, per-launch overhead dominates | `nsys` shows gaps between kernels; mean kernel duration in µs | large, zero-fidelity, zero-`METHOD_ID` lever: fuse kernels / CUDA graphs |
| **H2** | **dependency-bound** — kernels serialised, each too small to fill the card | low occupancy, one stream, no overlap | restructure the schedule; also zero-fidelity |
| **H3** | **host round-trip per kernel** | `perf` (L3) shows host wake per launch, correlated in `nsys` | fixable host-side; connects to the single-thread evidence |
| **H4** | **genuinely compute-bound at high efficiency** | high occupancy, near roofline | the card axis really is closed, the kernels are fine, **and only the cycles axis remains** — which makes #139 the answer by elimination |

⚠ **H4 is the outcome that would settle the fidelity argument by removing the alternative.** So this
run is decision-relevant whichever way it lands, which is the property every experiment here should
have and most have not. Note also that H4 is in tension with the bandwidth invariance: a
compute-bound kernel on an H100 with 3.9x the bandwidth should still not return 0.95x.

### 5.2 The rule that stops the re-work: re-rank forward, not backward

Every lever must be priced **against the profile that will exist after the other levers land**, not
against today's. The board has been bitten by this twice already and both were caught late:

- **The packer.** #139 makes ECDSA and Schnorr diverge by 13.8x per verification, and the packer has no
  input-type term. Un-refitted, #139 delivers **2.95x instead of 6.95x** — the refit is a
  *prerequisite*, worth 2.36x, and it was found after #139 had been argued about for weeks.
- **The aggregate.** It is 1,566 s — **9.6% of one-card cost today, and 43% after #139 lands**
  (`14,656/6.95 + 1,566 = 3,675 s`). It has **never been profiled**, and `HAZYNC_AGG_EXECUTE=1`
  (`main.rs:2054`) means its cycles can be profiled tonight, on a laptop, for nothing.

**A lever's rank is a function of the plan, not of the measurement.** Re-rank after every landing.

---

## 6. What to do, in order

### Free, no GPU — tonight

| | why |
|---|---|
| **Profile the aggregate** — `HAZYNC_AGG_EXECUTE=1` | it is 43% of the post-#139 cost and has never been looked at (§5.2) |
| **Price bounded-lag pipelining** (§1.1) | drops the bar 1.95x → 1.69x, costs nothing, needs only a design decision |
| **Close E9 in the docs** | measured out at ≤1.09x (§3); it carries deadlock risk and a fork |
| **Correct §4.2's "65% idle"** in `PERF_INVESTIGATION_2026-08-26.md` | it contradicts §1 of its own file and the measurement (§3) |

### One card, one day — the run that decides the project

**L2 + L3 + L4 on the same chunk-9 prove, reconciled to the same 399 s.** One L40S, a few hours.
Deliverable: a single table where execute + preflight + witgen + NTT + hash + merkle sum to the wall,
each attributed host-side or device-side, and `ncu` occupancy/bandwidth figures for the top five
kernels by total time. Then answer H1-H4.

⚠ **Do not run any further micro-experiment before this.** Every lever left on the board is worth
≤1.09x, and the profile that would tell us where the missing 1.8x lives has never been taken. Running
more E-series experiments first is precisely the shooting-in-the-dark this document exists to stop.

### Then, and only then

- If **H1/H2/H3**: a zero-fidelity, zero-`METHOD_ID` lever class opens in kernels we already vendor.
  Price it in the §1 denominator and re-rank.
- If **H4**: the efficiency axis is closed by measurement, and the 16-card target reduces to the
  fidelity decision — #139, at whichever variant is the smallest one that clears 1.95x.

### Standing bar for anything proposed after this

Four columns or it does not get discussed: **factor on divisible card-seconds · effect on the floor ·
fidelity cost · `METHOD_ID`.** No execute-mode cycles quoted as proving cost. No lever quoted against a
profile it will itself invalidate.

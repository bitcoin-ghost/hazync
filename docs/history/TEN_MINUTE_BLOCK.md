# The ten-minute block — one denominator, and how the fleet question was actually answered

**Target, stated as a number instead of an aspiration: prove a near-tip block in 600 seconds.**

⚠ **This file was `SIXTEEN_CARD_PLAN.md` and was renamed on 2026-08-28, because its own §8.13 concluded
that sixteen cards cannot get there** — §8.5's chunk floor alone is 16.3 min. A filename asserting a
superseded conclusion is the same failure this project keeps hitting, except it sits where nobody edits
it. **600 s is the goal; the card count was always the derived quantity**, and it has moved 16 → 28 →
32 → 48 → 29 as the measurements landed. The title now names the thing that does not move.

## What this document is, and what it is not

**This is the evidence trail: how the fleet question was answered, and what was got wrong on the way.**
The measurements in §7 and §8 exist nowhere else — the first `ncu` profile of the prove kernels and the
spill test (§8.1-8.3), the fan-out N=4/N=8 run and its cross-box control (§8.12), the 78%
witness-read finding (§7.5), the flow map (§7), and the post-mortems on the stale `>3,300 s` aggregate
(§8.4) and the backwards `N^1.79` fit (§8.12).

⇒ **For "what should we actually run", read `TOPOLOGY_AND_SETTINGS.md`.** It is the reference sheet and
it is deliberately short. This file is why those numbers are believed, which is the part that stops
them circulating without their labels.

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
one card   chunks 14,367 s  +  aggregate 1,566 s  =  15,933 s
floor      ~83 s does not divide  (INFERRED from a two-worker run)
wall(N)    83 + (15,933 - 83) / N
```

⛔ **This model is SUPERSEDED by §8.12 and §8.13 — read them before using any number in §1 or §2.**
The 83 s floor was inferred from a two-worker run and is wrong in both size and shape: the aggregate
is **1,575 s** on block 962,000 and it is **constant in N**, not a small additive floor. The section
below is kept because the four-column denominator it defines is still the rule; its arithmetic is not.

✅ **The chunk term is now MEASURED, not modelled** (2026-08-27, §2.1). `FLEET_SIZING.md` §4 lists
"14.34 G is modelled" as reason #3 not to trust the card count; it measures **14.057 G**, so the model
was **+2.0%** high and that reason retires. Every figure below divides by a measured number now.

**Two different bars, and they are not the same number.**

| framing | what it means | bar at N=16 |
|---|---|---|
| **latency** | one block, start to finish, inside 600 s | **1.92x** on divisible work |
| **throughput** | keep up with the chain at a fixed lag | **1.66x** — `15,933 / (16 x 600)` |

Today: `wall(16) = 83 + 15,850/16 = 1,074 s = 17.9 min`.

⚠ ~~**The floor is the hidden multiplier on the difficulty.** At an 83 s floor the bar is 1.95x. If
coordination at N=16 pushes the floor to 150 s the bar becomes 2.24x; at 300 s it becomes 3.36x.
**Nothing above N=2 has ever been run**, so the floor at 16 workers is UNKNOWN and it is worth more
than any micro-optimisation on this page.~~

✅ **ANSWERED 2026-08-27 (§8.12), and the framing was wrong.** N=4 and N=8 have now both been run, on
one box, one card, one binary, with only N moving. The floor did not grow with N — it did not move at
all: **115.8 s → 116.6 s, +0.7% for double the chunks.** It is not a coordination floor that worsens
as the fleet grows; it is the aggregate, whose cost is a function of the **block** and is constant in
N. This was the single most load-bearing unknown on the page and it resolved in the favourable
direction: fan-out is free, and the question moved from "can we afford more cards" to "how many"
(§8.13).

### 1.1 The throughput framing is cheaper, and nothing in the design forbids it

FROM CODE: `prove_chunk` takes no previous receipt. The chain dependency is `add_assumption(prev)` in
`prove_step` / `prove_step_succinct` (`main.rs:396, 1024`) — i.e. **only the per-block fold is
sequential; chunk proving of block h+1 does not wait on block h's proof.**

So a fleet may run blocks overlapped, at a bounded lag, and the 83 s floors of consecutive blocks
overlap with the next block's chunk work. That converts the latency bar into the throughput bar and
**drops the requirement from 1.95x to 1.69x for free** — at the cost of always being a block or two
behind the tip, which for a proving system that tracks the chain is a design choice, not a defect.

#### PRICED 2026-08-28 — ~29 cards, and it makes the last blocker irrelevant

On the measured block-962,000 figures used throughout §8.13 (14,926 chunk card-seconds, 1.05x
straggler, 1,575 s aggregate):

```
per block, all in:  14,926 x 1.05  +  1,575  =  17,247 card-seconds
sustain one block per 600 s:        17,247 / 600  =  28.7  =>  29 cards
```

**Why this is not just "a bit cheaper than 32".** In the latency framing, the fleet size depends
entirely on how much of the aggregate distributes — §8.13's scenarios (a) 32 cards, (b) 48 cards, and
(c) **fails at any size**, 34 min. In the throughput framing that distinction **disappears**, because
the concurrency comes from running *different blocks* at once rather than from splitting one block:

- Chunk proving of block h+1 does not wait on block h (`prove_chunk` takes no previous receipt).
- Per-block aggregates are independent of each other — only the **chain fold** carries
  `add_assumption(prev)`.

⇒ **Scenario (c), the one that kills the latency framing, costs nothing here.** A fully serial
1,575 s aggregate is fine if several are in flight on different blocks. That matters because §8.13
ranks the distributed-aggregate check as the #1 remaining measurement and it needs >= 2 boxes: under
this framing **it stops being a purchasing blocker** and becomes an optimisation.

**What it costs: lag, and only lag.** Splitting 29 cards by workload share (chunks are 90.9% of
card-seconds) gives ~26 on chunks and ~3 on aggregates, so one block takes ~603 s of chunk time plus a
serial ~1,575 s aggregate ⇒ **≈36 min, about 3.5 blocks behind the tip**, in exchange for steady-state
throughput of one block per 10 min on **29 cards instead of 32-48**.

⚠ **The one term that is NOT priced here is the chain fold.** §1.1's own code reading says the fold is
the sequential step, and its per-block cost has never been measured. If it exceeds 600 s the whole
framing fails regardless of fleet size. It is small by construction — one join against the previous
receipt, not a re-proof of the block — but "small by construction" is exactly the kind of claim this
document exists to stop people quoting. **Measure it on the next box that is up; it is minutes of work
once one exists.**

⚠ Coordinator egress is not a new risk here: E5 measured ~18 Mbps against a ~320 Mbps assumption and
the risk was retired in #189, though that was measured for the latency framing's traffic pattern, not
for several blocks in flight.

⇒ **Recommendation: price the purchase at 29 cards + bounded lag, not 32-48 + tip latency, unless
being ~36 min behind the tip is unacceptable to the product.** That is a product decision, not an
engineering one, and it is now the cheapest question on the board — it needs no GPU and no code.

---

## 2. The scoreboard, and the arithmetic that follows from it

| lever | factor on divisible work | floor | fidelity | new `METHOD_ID`? | status |
|---|---|---|---|---|---|
| Tier 0 guest codegen (`-O3`, rust LTO+CGU=1, window 20) | **1.012x** | — | none | **yes** | MEASURED (`TIER0_RESULTS_2026-08-26.md`) |
| Worker processes per card (E6) | **≤1.09x** | — | none | no | see §3 — ceiling corrected downward |
| Preflight overlap (E9, risc0#3201 fork) | **≤1.09x** | — | none | no | see §3 — **should now close** |
| More cards | — (latency only) | — | none | no | not a *cost* lever — but see §8.12/§8.13, it is the live *latency* lever |
| **#139 middle path** | 5.25x | — | **~85% of Core** | yes | measured execute-derived |
| **#139 wholesale + type-aware packer** | **6.95x** | — | **~95% of Core** | yes | 7.18x MEASURED at proving time |
| Bounded-lag pipelining (§1.1) | 1.15x-equivalent (bar 1.92x → 1.66x) | **removes it** | none | no | UNPRICED — free to settle |
| **Witness read via `read_slice`** (§7.5-7.7) | **1.06x now, ~1.34x after #139** | — | **none** | **yes** — batch it | MEASURED 2026-08-27 |

**The conclusion is arithmetic, not judgement:**

```
all known zero-fidelity levers, multiplied:  1.012 x 1.09  =  1.106x
wall(16) after all of them:  83 + 14,331/16  =  979 s  =  16.3 min
target:                                          600 s  =  10.0 min
```

⛔ **16 cards is not reachable on the known zero-fidelity levers. They take 17.9 min to 16.3 min,
against a 10.0 min target.** Every remaining item on the board, added together and all landing
perfectly, closes about a fifth of the gap in seconds and none of it in fidelity terms.

That leaves exactly three routes, and they are not alternatives to each other — they are a decision,
a measurement, and a search:

1. **Take a fidelity trade (#139).** Even the *middle* path overshoots: `wall(16) = 350 s = 5.8 min`.
   Wholesale with the type-aware packer gives 305 s, and reaches 600 s on **~7 cards**.
2. **Take the free 1.15x-equivalent** by accepting bounded lag (§1.1) — real, but not sufficient alone.
3. **Find a lever class nobody has enumerated.** §4 argues there is one, and names where it is.

⚠ **The fidelity decision has more slack than the board presents.** The target needs 1.95x; the
smallest #139 variant delivers 5.25x. Nobody has asked *what the smallest concession worth 1.95x looks
like* — the board only prices the two variants that happen to exist. That question is open and it is
the right one to ask before spending 85% of Core.

---

### 2.1 The block's real cycle count, and two things it exposes

MEASURED 2026-08-27, `HAZYNC_PROFILE_EXEC=1 chunk-profile`, block 962,000, 8,006 inputs, 16 chunks,
laptop, no GPU, 604 s. Evidence: `~/hazync-l40s-evidence-2026-08-26/execmode-cycle-profile-962000-2026-08-27.log`.

| | count-packed (old) | **cost-packed (production)** |
|---|---|---|
| measured total | 14.048 G | **14.057 G** |
| predicted total | 14.341 G | 14.341 G |
| measured mean chunk | 878 M | 879 M |
| **measured straggler** (max / mean) | **1.251x** | **1.059x** |
| *predicted* straggler | 1.308x | **1.001x** |
| max / min chunk | 4.57x | 1.19x |

**Cost-packing is worth 1.18x on the slowest chunk — and a block's wall-clock IS its slowest chunk —
for a total-cycles cost of +0.06%.** That trade was previously argued from a model; it is now measured,
and `reproduce/METHOD_ID`'s note that cost-packing "costs slightly MORE" is quantified: 0.06%.

⚠ **The packer's own balance metric is computed from PREDICTIONS, and it reads perfect when it is not.**
`chunk-profile` prints `straggler: max 897454514 vs mean 896331744 = 1.00x` — both numbers are
*predicted*. Measured, the same partition is **1.059x**. A check that reports 1.00x whatever the guest
actually does is the failure mode this repo already has a name for.

⚠ **The byte term is still over-charging after #136/#137 — the fourth instance of that pattern.**
Per-chunk model error is 2.4-2.6% mean absolute, but it is not noise, it is signed and it tracks
payload size: the three highest-byte chunks (16.5 MB, 17.4 MB, 26.9 MB) measure **-12.3%, -12.7% and
-6.2%** against prediction, while compute-heavy chunks run over. So the packer under-fills chunks
carrying large transactions. The 32 measured points in the log above are the refit's input.

⚠ Neither of these is a lever in the §2 sense — together they are worth ~6% on the slowest chunk, not
2x. They are recorded because **#190 is already adding a curve dimension to this packer**, the refit is
a #139 *prerequisite* worth 2.36x, and this is the measurement that refit should be fitted against.

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
| ~~Measure the block's real cycle count~~ | ✅ **DONE 2026-08-27** — 14.057 G measured, model +2.0% high, `FLEET_SIZING.md` §4 reason #3 retires. See §2.1 |
| **Price bounded-lag pipelining** (§1.1) | drops the bar 1.95x → 1.69x, costs nothing, needs only a design decision |
| **Close E9 in the docs** | measured out at ≤1.09x (§3); it carries deadlock risk and a fork |
| **Correct §4.2's "65% idle"** in `PERF_INVESTIGATION_2026-08-26.md` | it contradicts §1 of its own file and the measurement (§3) |

⛔ **The aggregate profile is NOT free, and this document said it was.** `agg_chunks()` calls
`read_chunk_receipts()` *before* the execute-mode branch, and it verifies every receipt against
`METHOD_ID` (`main.rs:2030`). Execute mode therefore still needs sixteen real chunk receipts from the
**current** guest, and producing those needs a GPU. Checked rather than assumed:

| receipt set / binary | id | usable? |
|---|---|---|
| `hazync-artifacts/box1/` — all 16, block 962,000 | `b62d2a60…` | **superseded 2026-08-24** |
| `hazync-dist-v0.18.5/hazync-host` | `4722cec8…` | no |
| canonical at the time, since superseded (`main`, v0.19.0) | `1d6c3792…` | **no receipts exist locally** |

**`HAZYNC_AGG_EXECUTE` has never been run by anyone**, and it moves to the card day. Nor can
arithmetic stand in for it: `reproduce/METHOD_ID` records the aggregate at `~1,137 M` cycles, which at
the measured L40S rate is 1,162 s of the 1,566 s total — but the same composition on the
pre-re-baseline figure (`3,636.4 M`) predicts ~3,500 s against a measured total of **>3,300 s**, i.e.
it overshoots the entire run. Aggregate segments do not prove at the chunk rate, so the
validation/resolution split is UNKNOWN until the real run happens.

⛔ **SUPERSEDED — see §8.4.** The `>3,300 s` figure above predates four commits that change
exactly this path (#148, #153, #157, #161). MEASURED 2026-08-27: the N=16 **tip** aggregate is
**1,574.9 s**, and the GPU is never idle. Any reasoning below that rests on a 3,300 s aggregate,
or on "aggregation is the binding constraint", is reasoning from a stale number.

✅ **One risk to this document was checked and it holds.** `FLEET_SIZING.md`'s 1,565.9 s aggregate is
cited to #180 on "the v0.19.0 signed binary", and the re-baseline that made the aggregate **3.20x**
cheaper landed in `2d9a636` (2026-08-24 02:56). v0.19.0 was tagged 22:37 the same day and
`git merge-base --is-ancestor` confirms it **contains** that commit. The aggregate term is current,
not stale, and §1's arithmetic stands.

### One card, one day — the run that decides the project

**L2 + L3 + L4 on the same chunk-9 prove, reconciled to the same 399 s, plus the aggregate profile
above** — the sixteen chunk proves that session produces are exactly the receipts
`HAZYNC_AGG_EXECUTE` has been blocked on, so it costs nothing extra once the card is up. One L40S, a
few hours.
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

---

## 7. The flow map — stage by stage, what enters and what is done twice

Workstream B, started 2026-08-27. The premise is this project's own history: the three largest wins
ever found here (#136 `read_slice`, #137 group-by-tx, and the `1d6c3792` re-baseline at 3.62x) were
**all in the plumbing, none in consensus code**, and none was visible until somebody profiled the
data path rather than the compute.

### 7.1 What each chunk actually receives — grouping works, the model does not describe it

FROM CODE. `write_chunk_inputs` (`main.rs:1852`) walks the chunk's inputs, coalesces **consecutive
runs sharing a `tx_idx`**, and writes each group's transaction and prevout blob **once**:

```rust
b.write(&(groups.len() as u32));
for (tx_idx, gs, ge) in groups {           // one entry per RUN of inputs sharing a tx
    b.write_slice(&padded(tx));            // the transaction: ONCE per group
    b.write_slice(&padded(prevouts));      // the whole prevout blob: ONCE per group
    for inp in &w.inputs[gs..ge] { ... }   // then the per-input fields
}
```

`input_costs` (`main.rs:1681`) prices the same work **per input, at full transaction size**:

```rust
let bytes = (tx.len() + prevouts.len()) as u64;
COST_INPUT_BASE + COST_PER_EC_OP * ec + COST_PER_INPUT_BYTE * bytes   // charged for EVERY input
```

**The encoder is per-group; the cost model is per-input.** On block 962,000 that is a large gap:
summed over inputs, the per-input charge counts **53.40 MB** where each transaction counted once is
**1.53 MB** — a factor of **34.9x**. The measured consequence is already in §2.1: the three
highest-byte chunks come in at **-12.3%, -12.7% and -6.2%** against prediction, and the packer's
predicted straggler reads 1.001x where the measured one is 1.059x.

⚠ **This is NOT a stale constant, and calling it one would be wrong.** `git log -S` puts the last
change to `COST_PER_INPUT_BYTE = 6` in `c6e95ff` — *the same commit that introduced grouping*
("Group the chunk payload by transaction", 2026-08-21). The value was chosen with grouping in view.

**The defect is a missing dimension, not a wrong number.** The byte term is carrying two costs that
now scale differently, and one constant cannot express both:

| cost | scales with | after grouping |
|---|---|---|
| marshalling — shipping and reading the payload | **bytes per GROUP** | paid once per transaction per chunk |
| `input_bind` — re-hashing the whole transaction | **bytes per INPUT** | still paid per input |

The comment at `main.rs:1585` says as much: *"What remains in the byte term is mostly `input_bind`,
which still hashes the whole transaction once per input. That is ~2.5 cycles/byte of the 6."*
So ~3.5 of the 6 describes work that is now per-group, charged as though it were per-input.

**The fix is a second term, not a refit** — `COST_PER_GROUP_BYTE * grouped_bytes +
COST_PER_INPUT_BYTE * per_input_bytes`. That is the same shape of defect the board already records
against #139 (a missing *type* dimension) and that #190 is adding a curve dimension for. A packer
being taught one new dimension should be taught both at once.

### 7.2 The re-hash itself, priced

MEASURED from the witness: `input_bind` re-hashing costs `53.40 MB x ~2.5 cyc/byte` = **~133 M
cycles, 0.95% of the block**. Hoisting it to once-per-transaction would recover ~130 M (0.92%).

⚠ Small in total, but **87% of those bytes sit in 10% of the inputs**, so its effect on the
*straggler* — which is what a block's wall-clock actually is — is larger than 0.95% and lands on
whichever chunk holds the fat transactions. Worth having, nowhere near the 1.92x bar.

### 7.3 A simulator that does not match the code it simulates

`prover/tools/pack_after_139.py` builds its per-input cost with
`txb//max(1,len(t['prevouts']))` — the transaction's bytes **amortised across its inputs**. The real
`input_costs` charges `tx.len() + prevouts.len()` **in full, per input**. The script is explicit that
it reproduces the packer's *shape* rather than calling it, but this is a different byte model, not an
approximation of the same one.

That matters because this script is the source of the **2.36x** figure for the packer refit — the
number that makes the refit a #139 *prerequisite* rather than a follow-up. The finding may well
survive (it rests on the 13.8x ECDSA/Schnorr divergence, which is independent), but **the 2.36x
itself should be re-derived against the real cost function before it is quoted again.**

### 7.4 Still to map

`BlockWitness` construction · execute -> segment boundary · the wire · lift · the join tree · what
the aggregate re-derives that sixteen chunks already knew. ⚠ A refit must be fitted in **Rust**,
where `predicted_ec_ops` lives: a Python reconstruction of it disagreed with the binary on EC counts
for 5 of 16 chunks and on prevout framing by 30 bytes per input, so no fit taken outside the binary
is trustworthy. `chunk-profile` at several `HAZYNC_CHUNKS` values is the honest way to get more
measured points, and it is CPU-only — it can run beside GPU work rather than competing with it.

### 7.5 The aggregate never got #136's fix — 78% of block validation is deserialisation

MEASURED 2026-08-27, `host vb-stages`, current guest (`main`), execute mode, laptop, no GPU.
Evidence: `~/hazync-l40s-evidence-2026-08-26/vbstages-*.log`.

| phase | this phase | share of FULL |
|---|---|---|
| **read witness + header/version** | **2,519,517,266** | **78.2%** |
| + per-tx output leaves (`tx_out_leaves`) | 38,034 | 0.0% |
| + `created_at` in-block-coin map | 143,033,709 | 4.4% |
| + input loop: binds & per-tx checks | 146,219,635 | 4.5% |
| + utreexo deletes | 57,216,172 | 1.8% |
| + utreexo adds + root compare | 14,081,829 | 0.4% |
| + merkle root | 65,570,323 | 2.0% |
| + wtxids & witness commitment | 277,529,575 | 8.6% |
| **FULL** | **3,223,206,543** | |

**The witness is 7,256,592 B, so the read costs 347 cycles per byte.**

⛔ **This is #136's finding, unfixed, in the path #136 did not touch.** #136 measured `env::read` of
the *chunk* payload at ~147 cycles/byte — *"50.9% of a chunk's entire cycle count before any Bitcoin
logic ran"* — and replaced it with `write_slice`/`read_slice`. Both `write_aggregate_env`
(`main.rs:2016`) and `vb_stages_cmd` (`main.rs:3843`) still do a plain **`b.write(&w)`** over the
whole `BlockWitness`. Same disease, **2.4x worse per byte**, never profiled.

In absolute terms that single read is **17.9% of the entire block's measured chunk work** (2.52 G
against 14.06 G), paid once per block.

**And the per-byte cost is not constant — it worsens with block size.** Four blocks, same command:

| block | inputs | witness | stage-0 cycles | cyc/byte |
|---|---|---|---|---|
| 130,000 | 10 | 27,096 | 1,334,605 | **49.3** |
| 140,000 | 212 | 273,704 | 19,050,711 | **69.6** |
| 741,000 | 670 | 846,656 | 268,640,421 | **317.3** |
| **962,000** | **8,006** | **7,256,592** | **2,519,517,266** | **347.2** |

⚠ **Superlinear, but do not quote an exponent.** Overall it fits `bytes^1.35`, yet the local
exponents are **1.15, 2.34, 1.04** — nothing like a clean power law, and these blocks differ in era
and structure as well as size. What is safe to say: **cost per byte rises ~7x** from small historical
blocks to modern ones, so this gets worse in the direction of the tip, not better. The mechanism
needs a guest-side profile to pin down; serde's byte-at-a-time walk is the obvious suspect (it is
what #136 found) but it is not proven here.

### 7.6 Two things this contradicts

⚠ **`vb_stages_cmd`'s own comment says the input loop is "73% of the total".** Measured: **4.5%**.
The comment is describing a guest that no longer exists.

⛔ **`reproduce/METHOD_ID` records the aggregate at `~1,137 M` cycles.** The aggregate reads *this
same witness* plus sixteen chunk journals, so it cannot cost less than the 2,519 M this read
measures. One of the two is wrong. **`HAZYNC_AGG_EXECUTE` settles it**, and §6's sixteen chunk
proves are what unblock it — which is now the single highest-value item on the card day, because the
answer decides whether a ~2.5 G-cycle inefficiency is real and sitting in the term that becomes
**43% of cost after #139**.

⚠ Reading the table above: `vb-stages` carries one `prev` across both the phase ladder and the
`[loop]` breakdown, so the printed "this phase" for the `[loop]` rows subtracts the FULL run and is
meaningless. Read those rows as cumulative only.

### 7.7 Priced in the denominator — because 78% is not the same as a large lever

The §1 rule exists for exactly this case. A finding that reads as "78% of block validation" has to be
converted into **factor on divisible card-seconds** before it can be compared to anything.

The aggregate is **1,566 s of the 15,933 s one-card total — 9.8%**. Assume the read is 78.2% of the
aggregate's *validation* half and that assumption resolution is roughly fixed (300-500 s, the
uncertainty §6 flags):

| | saving | one-card | wall(16) | **factor** |
|---|---|---|---|---|
| today | 834-990 s | 14,943-15,099 s | **16.9-17.0 min** | **1.06x** |
| after #139 | same | 2,643-2,800 s vs 3,633 s | | **1.30-1.37x** |

⚠ **So it is worth ~1.06x today and ~1.34x after #139**, not the 4.5x its share suggests. It does not
close the 1.92x gap on its own, and nothing about it changes §2's conclusion.

**It is still the best-value item on the board by cost-to-fix**: the technique is already written and
shipped (#136's `write_slice`/`read_slice`) and it costs **no fidelity**.

⛔ **Correction — it DOES move `METHOD_ID`.** An earlier draft of this section reasoned that the
host→guest environment encoding is not guest source and might therefore land without a re-baseline.
That is wrong: the guest reads it with `let w: BlockWitness = env::read()` (guest `main.rs:1186` and
`:1291`), so changing the encoding changes guest source like any other guest edit. It must be
**batched** with the other re-baselining changes — Tier 0's codegen and #139 — exactly as
`ACCELERATION.md`'s "`METHOD_ID` constraint" section requires. That does not reduce its value; it
fixes where it sits in the order.

⚠ And it is worth **more** later than now, which is §5.2's rule working: the aggregate is 9.8% of
cost today and 43% after #139. A board ranked on today's profile would rank this eighth.

### 7.8 What the witness actually is — and `HAZYNC_WITNESS_SIZES` does not report it

MEASURED across four blocks. The tool's four categories (`proof_siblings`, `raw_tx`, `prevouts`,
`txids`) account for only **34.5%** of block 962,000's witness. The rest scales per input:

| block | inputs | total | accounted | **residual** | residual/input |
|---|---|---|---|---|---|
| 130,000 | 10 | 27,096 | 3,792 | 23,304 | 2,330 |
| 140,000 | 212 | 273,704 | 93,902 | 179,802 | 848 |
| 741,000 | 670 | 846,656 | 265,485 | 581,171 | 867 |
| **962,000** | **8,006** | **7,256,592** | 2,497,413 | **4,759,179** | **595** |

⇒ **the `inputs` vector is ~65% of the witness**, at roughly 600-870 bytes per input (130,000 is
fixed overhead dominating ten inputs, not a contradiction). Transaction bytes — the thing one would
assume dominates — are **21%**.

`BlockInput` (guest `main.rs:291`) is seven scalars plus **two `WireProof`s**, each carrying a
`[u8; 32]` leaf, a `u64` position and a siblings `Vec<[u8; 32]>`. Sibling counts here are ~1 per
proof, so the bulk is the struct encoding itself, which is consistent with 32-byte hashes and small
scalars each occupying ~4x their raw size in risc0's word stream.

⚠ **This redirects the fix.** The obvious reading of §7.5 is "pack the transaction bytes" — but those
are already only a fifth of it. **The target is the per-input structs.** Anyone acting on §7.7 should
confirm this by instrumenting `to_vec` per sub-structure before writing an encoder, because the
figures above are a residual, not a direct measurement of the `inputs` field.

⚠ Also: line 898 of the guest says the proofs clone *"~28x32 B of siblings per input"*. Measured here
it is **~66 B per input** — about one sibling per proof. That comment describes a different
accumulator state and should not be used to size anything.

## 8. The kernels, measured at last — and the figure that was stale

MEASURED 2026-08-27 on an L40S (46,068 MiB, 350 W), po2 22, `METHOD_ID 1d6c3792`. Evidence:
`~/hazync-l40s5-evidence-2026-08-27/`.

§4 called the CUDA layer "the loudest unexplained signal on the board" and §4.1 noted it is exempt
from `METHOD_ID` and fidelity. It has now been profiled. Two things came out of it: the kernels are
nowhere near roofline, and the aggregate figure this plan has been reasoning from is **stale**.

### 8.1 The first `ncu` profile of the prove kernels

| kernel | % GPU time | `sm__throughput` | ALU | DRAM | `warps_active` | regs/thread | blocks/SM |
|---|---|---|---|---|---|---|---|
| `eval_check` | **49.5%** | **15.24%** | 9.41% | **58.71%** | **16.66%** | **255** | **1** |
| `par_stepExec` | **19.8%** | 3.73% | 3.00% | 3.60% | 14.90% | **255** | **1** |
| `_poseidon2_rows` | 8.1% | **78.31%** | 65.50% | 8.60% | 65.34% | 60 | 4 |

⇒ **H4 (int32-roofline) is TRUE only for `_poseidon2_rows`, which is 8.1% of GPU time.** For the
69.3% the other two own it is false. Both sit at **255 registers/thread** — the hard maximum. An SM
has 65,536 registers, so 256 threads x 255 regs = 65,280: one block fills the register file, leaving
8 resident warps of a possible 64. `eval_check` has 461 waves/SM available, so this is not a
shortage of work; it is too few resident warps to hide latency.

⚠ Block size cannot fix this. 65,536 / 255 = 257 threads per SM however they are grouped. Registers
per thread is the only lever that exists.

⚠ Getting this measurement at all needs `--replay-mode application`. Default kernel replay saves and
restores device memory between passes, doubling the largest working set, and `eval_check` OOMs:
`Launch failed (error = 2)`. **The failed run still writes a full-length CSV whose every value is
`nan`.** Row count is a check that cannot fail; judge by `grep -c ',"nan"'`.

### 8.2 They already spill — which corrects §8.1's own roofline reading

| kernel | local ld+st (spill) | global ld+st (real data) | spill : data |
|---|---|---|---|
| `eval_check` | 23,358,078,976 sectors | 7,163,871,232 | **3.26 : 1** |
| `par_stepExec` | 7,928,700,826 | 1,899,264,744 | **4.17 : 1** |

**76-81% of all L1 sector traffic on both dominant kernels is register spill, not data.** 255 is the
compiler capping out and dumping the remainder to local memory — not a working set that genuinely
fits in 255 registers.

⛔ **This invalidates the obvious reading of the DRAM column above.** `eval_check` at 58.71% of DRAM
peak looks memory-bound, implying a ceiling of ~1.70x. Most of that traffic is spill — traffic that
should not exist. The bandwidth pressure and the occupancy cap have the **same cause**, so the
ceiling is higher than 1.70x, not lower.

⇒ It also reconciles the card-invariance tension §2 never resolved. Spill is served largely by
per-SM L1/L2, whose per-SM bandwidth is roughly flat across GPU generations — consistent with
L4/L40S/H100/B200 landing within ±9% despite 4-9x differences in DRAM bandwidth and large
differences in SM count.

⇒ And `_poseidon2_rows` is the control that settles the question of blame: **the same BabyBear
integer arithmetic, on the same card, at 78.3% of peak**, with 60 registers and 4 blocks/SM. The
field arithmetic is not the constraint. The register footprint is.

### 8.3 The power rail says the same thing, independently

| phase | power draw | of 350 W |
|---|---|---|
| chunk proving (`eval_check`-dominated) | **147 W** | 42% |
| tip aggregate (different kernel mix) | **276 W** | 79% |

At 46 °C, SM clock pinned 2520/2520 MHz, `clocks_event_reasons.active 0x0` — **no throttling of any
kind**. A card reporting 99-100% *residency* while drawing 42% of its power budget is full of warps
that cannot issue. Two independent instruments — `ncu` counters and the power telemetry — agree.

⇒ The 276 W figure kills the remaining escape hatch. "BabyBear integer work simply does not draw
power" cannot explain 42% in one phase and 79% in another on the same card in the same session.

### 8.4 ⛔ The `>3,300 s` aggregate figure is STALE

`report_agg_execute_only` prints *"the FULL aggregate cost beyond that (measured: >3,300 s)"*, and
this plan and the session notes have both treated **"aggregation is the binding constraint and does
not parallelise ⇒ a 10-minute block is impossible at any N"** as settled. It is not.

MEASURED, full prove, GPU sampled every 2 s throughout:

| | N=4, block 741,000 | **N=16, block 962,000 (tip)** |
|---|---|---|
| segments at po2 22 | 28 | **376** |
| segment proving | 101 s | **1,379 s** (3.67 s/seg) |
| assumption resolution | 16.3 s | **196 s** |
| **total wall** | **117.3 s** | **1,574.9 s** |
| GPU samples at 0% | 0 of 59 | **0 of 779** |

Both receipts VERIFIED. **The tip aggregate is 1,575 s, not 3,300 s — 2.1x cheaper than the stale
figure, at the harder of the two scales.** The GPU is never idle, so the "resolution runs off-GPU"
hypothesis (which hazync#147's `nvidia-smi` at 0% suggested) is REFUTED.

**Why it went stale.** The string was written in `c201f3f` (#150) at 2026-08-24 00:34. Four commits
that change exactly this path landed *after* it:

| | | |
|---|---|---|
| `2d9a636` | 08-24 02:56 | #148 segment distribution + **balanced join tree** |
| `78d42e9` | 08-24 13:22 | #153 aggregate distributes, **resolves as work** |
| `2facde4` | 08-24 18:21 | #157 distribute the last segment |
| `7701b86` | 08-25 03:16 | #161 distribute the last lift |

⚠ The figure is still hardcoded in the printed output, so **every execute-mode run reprints a number
measured before its own fix**. That is how it survived into the notes as fact. It should be deleted
or re-measured, not left to be re-quoted.

✅ **And this measurement independently confirms `FLEET_SIZING.md`.** That document records a
**1,565.9 s** aggregate; the tip run above measured **1,574.9 s** — **0.6% apart**. §6 had already
reasoned that the FLEET_SIZING term was current rather than stale, because v0.19.0 contains the
re-baseline commit `2d9a636`, and concluded "§1's arithmetic stands". It does. That reasoning was
right and is now confirmed by direct measurement.

⇒ **The correction is therefore narrower than it first appears, and it lands on the notes rather
than on this plan.** The project held two aggregate figures — 1,565.9 s and >3,300 s — and this
document had already worked out which one was live. What carried the stale number forward was the
session record, where "aggregation is the binding constraint ⇒ 10-minute block impossible at any N"
had hardened into a settled conclusion. §1's 16.6 min was never in doubt; §8.5 now explains where it
comes from.

### 8.5 The binding constraint is the CHUNK side

Block 962,000, 16 cost-packed chunks, each proved to a succinct receipt:

| | |
|---|---|
| total sequential | 14,926 s (4.15 h) |
| mean / max / min | 933 s / **980 s** / 828 s |
| **straggler, MEASURED** | **1.05x** (predicted 1.00x) |
| 16-card wall = slowest chunk | **980 s = 16.3 min** |

⇒ 16.3 min lands almost exactly on §1's 16.6 min. **The chunk side explains the plan's headline
number**, which the 55-minute aggregate claim never could — the two were always in contradiction and
§8.4 says which one was wrong.

⚠ The aggregate's 376 segments and its resolutions both distribute (#153/#157/#161). Across 16 cards
that is ~98 s. **The distributed path was NOT exercised today** — this is the code's claim, not a
measurement, and it is now the most load-bearing unmeasured thing on the board. If it does not hold,
the block is 16.3 + 26.3 = 42.6 min rather than ~17.9 min.

### 8.6 What a chunk actually costs: EC verifies — not bytes, not inputs

`chunk-profile` predicts cycles, and the prediction is honest: chunk 0 predicted 895,783,010 against
214 segments x 4,194,304 = 897,581,056, **0.2% apart**. Two falsifiable tests, both stated before
the data arrived:

**Bytes do not drive cost.** Predicted cost is flat while bytes vary 86x:

| chunk | bytes | vs chunk 0 | time |
|---|---|---|---|
| 0 | 313,835 | 1x | 923 s |
| 1 | 1,603,016 | 5.1x | 927 s |
| 2 | 16,459,628 | 52x | **830 s** |
| 4 | 26,886,050 | **86x** | **882 s** |

**Inputs do not drive cost either.** Chunk 6 carries 1,522 inputs — 3.6x chunk 0 — with *fewer* EC
verifies (432 vs 451), and took 962 s: 4% more, not 260% more.

Normalising by EC verifies gives ~2.06 s/EC across chunks 0-3, with chunk 4 (the 86x-bytes one) at
**2.38 s/EC**. ⇒ the witness read is real but **worth ~15% on the most byte-heavy chunk**, not the
~50% that #136's per-chunk figure implies at tip scale.

⛔ **This downgrades #139 on the chunk side to roughly 1.1x, not the 1.5x §7.7 would suggest.** §7.7
priced it against `validate_block`; `validate_block` is ~3% of the aggregate (110.9 M cycles of a
run whose block-validation share is ~107 s of 1,575 s), and a small share of a chunk. The fix is
still worth taking — it is cheap and it compounds — but it is not the lever.

⇒ Chunk cost is dominated by **EC verification — Core's real libsecp256k1 in the guest**. That is
the one thing fidelity forbids touching, and correctly so. It also means fan-out works: EC verifies
divide by N, so per-chunk time falls ~1/N.

### 8.7 The arithmetic, revised

Amdahl on the measured shares (`eval_check` 49.5%, `par_stepExec` 19.8%, `_poseidon2_rows` 8.1%
already at roofline, ~22.6% unprofiled NTT/bit-reverse assumed flat):

| | `eval_check` | `par_stepExec` | **kernel lever** |
|---|---|---|---|
| conservative | 1.2x | 1.5x | **1.17x** |
| mid | 1.4x | 2.5x | **1.35x** |
| optimistic | 1.7x | 4x | **1.54x** |

§8.2 argues the true ceiling is above the 1.70x roofline bound used for `eval_check` here, so these
are, if anything, conservative. **Every other known zero-fidelity lever combined is 1.106x.**

| | 16 cards |
|---|---|
| chunk floor (slowest chunk) | 16.3 min |
| aggregate, if distributed | ~1.6 min |
| **total today** | **~17.9 min** |
| **with the kernel lever at 1.75x** | **~10.2 min** |

⇒ At 32 cards with the kernel lever, ~6 min — **subject to §8.8's first caveat**.

### 8.8 ⚠ What is NOT established

**1. Whether resolution scales with N.** Per-assumption resolution was 4.1 s at N=4 and 12.2 s at
N=16 — but the **block changed too** (741,000 → 962,000, ~1,600 → 8,006 inputs). Two variables moved
between two points. A power law through two points fits perfectly by construction and is worth
nothing. If it *is* N-driven (~N^1.8), N=32 costs ~680 s of resolution and there is an **optimal
fan-out not far above 16** — which would contradict the whole "more cards" framing. The clean test
is same-block, different-N: **block 741,000 at N=8** against the N=4 run already measured, ~50 min.

**2. That the distributed aggregate works.** See §8.5. Claimed by #153/#157/#161, unexercised today.

**3. That the spill is removable.** §8.2 shows the headroom exists. Cutting spill means restructuring
generated kernels — less per-thread state, smaller tiles — which is real engineering. The
`-maxrregcount` sweep (§8.9) is the cheap probe, and it may well come back negative: capping
registers buys warps by spilling *more*, into kernels already spilling 3-4x.

### 8.9 What to do, in order

1. **`-maxrregcount` / `__launch_bounds__` sweep.** Neither kernel carries `__launch_bounds__`, so
   nvcc optimises per-thread latency with **no occupancy target** and settles on 255. This is not
   fighting a tuning decision; it is supplying information the compiler was never given. Vendored on
   `perf/kernel-occupancy` behind `HAZYNC_MAXRREG`; unset reproduces the stock build exactly.
   ⛔ **Judge on wall-clock.** An arm that doubles occupancy and loses wall-clock is a loss.
   ⛔ Receipt bytes are not a control — seal bytes carry ZK blinding. Use `METHOD_ID` + `verify`.
2. **Rematerialisation** — recompute intermediates instead of holding them, trading ALU (9.4% used)
   for registers (100% used). The trade runs in exactly the right direction.
3. **Split `eval_check` into passes** over constraint groups. Its body is one inlined `poly_fp` call
   generated across `eval_check_0..3.cu` (200-278 KB each), so the fix likely belongs in the
   generator, not in hand-written CUDA.
4. **Explicit shared memory** instead of compiler-chosen spill.

Then §8.8's two measurements, in that order — the N-scaling test decides how many cards to buy, and
the distributed-aggregate test decides whether 16 cards means 18 minutes or 43.

### 8.10 The sweep, RUN — §8.9's first item is refuted, and it yields a model

MEASURED 2026-08-27, L40S, `seg-prove-one` on a fixed 495,009-byte segment, median of 9 reps per
arm. `METHOD_ID` identical on every arm. Vendored control (`stock`, var unset) reproduced the
pre-vendoring binary to **0.2%** on wall-clock and to the *sector* on spill counters, so the flag is
the only thing that varied.

| arm | regs | blk/SM | warps% | `sm__throughput` | spill (G sectors) | wall (median) | vs stock |
|---|---|---|---|---|---|---|---|
| **stock** | 255 | 1 | 16.65 | **15.21%** | 23.36 | **4,115 ms** | — |
| 128 | 128 | 2 | 31.76 | 11.74% | 40.89 | 4,552 ms | **+10.6%** |
| 96 | 96 | 2 | 31.82 | 10.69% | 49.30 | 4,741 ms | **+15.2%** |
| 64 | 64 | **4** | **60.59** | 7.78% | 67.13 | 5,446 ms | **+32.3%** |

⛔ **Every arm is slower, monotonically.** Distributions do not overlap (stock 4,085-4,155 ms; 64
arm 5,407-5,516 ms). The 64 arm raised occupancy **3.6x** — precisely the mechanism §8.9 hoped for —
and **halved throughput**.

⇒ **Occupancy is not the constraint. Spill traffic is.** The cap delivers the resident warps it
promises and loses anyway, because it buys them by manufacturing more of the traffic that was
already the bottleneck. Note 128 and 96 have *identical* occupancy (2 blocks/SM); 96 bought no warps
at all and merely added 21% more spill, which is why it is purely worse.

⛔ **No compiler flag can fix this.** 255 registers/thread is the **architectural maximum** on NVIDIA
hardware. Stock is already at the ceiling — the compiler wants more than the hardware permits and
spills the remainder by force. There is no headroom upward, and every step downward is worse.

**The model this yields.** Fitting wall-clock against spill across the three capped arms:

| arm | spill | wall | implied *k* |
|---|---|---|---|
| 128 | x1.75 | x1.106 | 0.180 |
| 96 | x2.11 | x1.152 | 0.190 |
| 64 | x2.87 | x1.323 | 0.265 |

⇒ **wall ∝ spill^0.21** (mean). Extrapolated in the direction that matters — *cutting* spill:

| spill cut by | predicted speedup |
|---|---|
| 2x | 1.16x |
| 4x | 1.34x |
| 10x | 1.63x |
| **32x** | **2.08x** |

✅ **Independent corroboration.** `_poseidon2_rows` runs at 78.3% against `eval_check`'s 15.21% —
**5.1x** the throughput, with essentially no spill. Solving `spill^-0.47 = 5.1` on the same fit puts
its spill at ~3% of `eval_check`'s, i.e. a **~32x** cut. The extrapolation and the existence proof
land on the same number from opposite directions.

⚠ **Do not over-trust this.** The exponent is fitted over a 2.9x range in the direction of INCREASING
spill and extrapolated 32x in the other. It also curves (0.180 → 0.265 as the cap tightens), so it is
not a clean power law. Use it to size the difficulty, not to predict a result.

⇒ **This reframes §8.9's ordering.** The relationship is sublinear: halving spill buys 16%, not 50%.
A win of consequence needs spill **nearly eliminated**, which means only §8.9 item 3 — splitting
`eval_check` into passes over constraint groups — has the right shape. Item 1 is refuted; item 2
(rematerialisation) helps only if it removes nearly all spill, which is unlikely for a single
inlined `poly_fp`.

**And the traffic budget for item 3 is favourable.** `eval_check` currently moves 7.16 G sectors of
global traffic against 23.36 G of spill. Splitting into 4 passes costs ~4x the global reads (~29 G)
while removing the spill — roughly **flat on total sectors**, but converting scattered local traffic
into coalesced global reads. That is the trade worth prototyping, and it is measurable before it is
committed to.

### 8.11 The kernel lever is CLOSED at the compiler level — stock is already the best arrangement

Five arms, same segment, median of 9, `METHOD_ID` identical throughout:

| arm | regs | blk/SM | `sm__throughput` | local traffic (G sectors) | wall | vs stock |
|---|---|---|---|---|---|---|
| **stock** | 255 | 1 | 15.21% | **23.36** | **4,115 ms** | — |
| `__noinline__` | 255 | 1 | 14.85% | 23.85 | 4,154 ms | +0.9% |
| `-dlto` | 255 | 1 | **22.24%** | **45.72** | 4,260 ms | +3.5% |
| `-maxrregcount=128` | 128 | 2 | 11.74% | 40.89 | 4,552 ms | +10.6% |
| `-maxrregcount=64` | 64 | 4 | 7.78% | 67.13 | 5,446 ms | +32.3% |

⛔ **Every arm is slower than stock. nvcc's default is already the best available arrangement.**

The `-dlto` arm is the one that settles it. LTO genuinely worked — throughput rose **46%**, which is
only possible if it inlined across the `eval_check_*.cu` boundary — and local traffic still **doubled**.
It removed `poly_fp`'s 6,952-byte ABI frame and the twenty per-call frames, and the union live set
then exceeded 255 registers by enough to spill *more* than the frames had cost.

⇒ **The live set does not fit, under any arrangement.** Registers pin at 255 — the architectural
maximum — in all five arms. The three degrees of freedom available at the compiler level are
"call it" (stock: ABI frames), "inline it" (`-dlto`: register spill), and "cap it"
(`-maxrregcount`: more register spill). All three were measured; stock is the cheapest.

⚠ The `unity` arm (compiling the four generated files as one translation unit) was staged and
**deliberately not run**: it reaches the same mechanism as `-dlto` through the compiler rather than
the linker, and `-dlto` already demonstrated that inlining doubles local traffic. Running it would
have bought a second measurement of a settled question.

⚠ Note `-dlto` costs **1,422 s to build** (23.7 min, single-threaded `nvlink`, against a ~400 s
baseline). Even had it won, that build cost would need weighing. That it takes a compiler 24 minutes
to inline this is itself evidence about `poly_fp`'s shape: one enormous data-flow graph, not twenty
loosely-coupled functions.

⇒ **What this does to §8.7's arithmetic.** The 1.3-2.1x kernel lever is NOT reachable by any means
available to this project. It required removing spill; every mechanism that removes one kind of
local traffic adds more of another. The remaining path to it is restructuring the constraint
polynomial itself — generator-level work in RISC0's circuit DSL, upstream, in a repo that is dormant
on two filed issues. That is a materially different commitment from a compiler flag.

⇒ **The board therefore returns to:** ~16.3 min at 16 cards (§8.5), 1.106x of known zero-fidelity
levers, and **fan-out as the live lever** — chunk cost tracks EC verifies (§8.6), which divide by N,
so 32 cards is ~7.7 min of chunk time. §8.8's first caveat (does resolution scale with N?) is now
the highest-value open question on the board, because fan-out is what is left.

✅ **What the workstream bought.** Not a speedup, but a closed question: four hypotheses tested to
destruction, the mechanism identified (`ptxas`, at no GPU cost), and the reason recorded so nobody
re-runs a register sweep in six months. The kernels are not the missing 1.8x, and that is now
established rather than assumed.

### 8.12 ✅ Fan-out is FREE — the aggregate does not scale with N

MEASURED 2026-08-27 on a second L40S (`hazync-l40s6`, same model, same driver 595.58.03, same
`METHOD_ID 1d6c3792`). §8.8's first caveat is now closed.

**One block, one box, one card, one binary — only N moves:**

| | N=4 | N=8 | change |
|---|---|---|---|
| segments at po2 22 | 28 | **28** | none |
| segment proving | 100 s | **100 s** | none |
| **assumption resolution** | **16 s** | **17 s** | **+1 s** |
| total aggregate | 115.8 s | **116.6 s** | **+0.7%** |
| per assumption | 4.00 s | 2.12 s | halved |

Both receipts VERIFIED. **Doubling the chunk count changed the aggregate by 0.8 seconds.**

⇒ Resolution is not merely flat per-assumption — **total resolution is essentially constant in N**.
The aggregate's cost is a function of the BLOCK, not of how many pieces it is split into.

⛔ **§8.8's N^1.79 reading was an artefact of its confound, and acting on it would have been
expensive.** The pair it rested on — 4.1 s/assumption at N=4 (block 741,000) against 12.2 s at N=16
(block 962,000) — moved the block as well as N, from ~670 inputs to 8,006. The block was doing all
of the work. That fit argued for an optimal fleet size near 16 and against buying cards; it was
exactly backwards.

✅ **The cross-box control is what licenses believing this.** The N=4 arm was re-measured here rather
than compared against the previous box's number: 116 s against 117.3 s, resolution 16 s against
16.3 s, chunk times within 1.8% across all four. Two boxes, ~1% apart. That cost 25 extra minutes
and is the only reason N=8 can be read as a clean single-variable comparison.

### 8.14 ✅ The aggregate DOES distribute — measured 2026-08-28, and it saturates

§8.13 ranked "does the distributed aggregate work?" as the blocker and noted it had **never been
exercised**. It has now been, on two L40S in one datacentre (0.21 ms RTT, identical `METHOD_ID` on
both, verified before the run because the coordinator refuses foreign receipts by name).

**Block 741,000, 4 chunk receipts, aggregate only:**

| workers | po2 22 | po2 20 |
|---|---|---|
| 1 | **118 s** (single-box reference: 115.8 s ✅) | **163 s** |
| 2 | **65 s** — 1.81x | **90 s** — 1.81x |
| 4 | — | **89 s** — 1.83x, **no gain** |

⇒ **Scenario (c) is DEAD.** The aggregate is not serial; it distributes, and at N=2 it does so at 91%
of ideal.

⛔ **But it SATURATES at N=2, and this is not yet explained.** A two-point Amdahl fit on N=1,2
predicted 54 s at N=4; it measured **89 s**. ⚠ **That is the `N^1.79` mistake repeating** — a curve
extrapolated from two points that cannot constrain it (§8.12). The third point caught it, and it was
only run because someone asked for it.

**Two candidate causes, with opposite consequences:**

| hypothesis | mechanism | at 16 chunks |
|---|---|---|
| **join-tree width** | 4 chunks = 4 leaves -> 2 joins -> 1, so only ~2-way parallelism EXISTS | 8-way width ⇒ §8.13's numbers hold |
| **coordinator-bound** | single-threaded `seg-serve` cannot feed 4 workers | no improvement ⇒ the aggregate caps near 1.8x |

**Diagnostic — GPU utilisation, 4 workers, sampled on both boxes:**

```
box1 (2 workers, LOCAL) : mean 82% util,  7% idle
box2 (2 workers, REMOTE): mean 59% util, 35% idle
```

⇒ **Inconclusive, but it rules out the clean "no work exists" reading** — that would idle all four
roughly equally. The **remote** box starves while the local one does not, which points at work
*delivery* rather than work *availability*. Not decisive.

### ⛔⛔ RETRACTED 2026-08-28 — the N=4 arm ADDED NO COMPUTE, so it measured nothing

**Everything in this subsection is void. Read this before any of it.**

The N=4 arm ran **2 workers on box1 + 2 workers on box2 — still only TWO PHYSICAL CARDS.** Each
`seg-connect` is a separate process on the same GPU, and **GPU concurrency has been measured and
rejected three times at 0.95-1.03x** (`ACCELERATION.md`): two proves on one card do not go faster.

⇒ **N=2 and N=4 had IDENTICAL compute.** The flat result is exactly what that predicts and says
**nothing whatever** about the segment coordinator, the join tree, or any ceiling.

⇒ The utilisation data agrees and I read it backwards: box2 going from 26% to **36% idle** when its
second worker was added is **two processes contending for one card**, not a coordinator failing to
feed them.

✅ **What was actually measured: the aggregate scales 1.76-1.81x on TWO CARDS** — 88-91% efficiency,
which is *good*. **Scaling beyond two cards is UNMEASURED**, and needs a third box, not a fourth
worker process.

⚠ Method note, and it is the third instance in this document: §8.12's `N^1.79` artefact moved the
block as well as N; §8.14's Amdahl fit extrapolated from two points; this one **changed the worker
count without changing the hardware.** Every one produced a confident, wrong conclusion from a
variable that was not the one under test.

<details><summary>The superseded reasoning, kept as the record of the error</summary>

#### (VOID) join-tree width is REFUTED; the segment coordinator is the ceiling

The 16-chunk test was run. A 16-chunk aggregate has an **8-wide** join tree, so if width were the
limit it would have scaled. It did not move at all:

| workers | 4 chunks | **16 chunks** |
|---|---|---|
| 1 | 163 s | **165 s** |
| 2 | 90 s — 1.81x | **94 s — 1.76x** |
| 4 | 89 s — no gain | **95 s — no gain** |

**Both saturate at exactly N=2.** And GPU utilisation confirms it from a second, independent angle —
the **remote** box starves *worse* as workers are added while the local one stays busy:

| | N=2 | N=4 |
|---|---|---|
| box1 (local) | 13% idle | 11% idle |
| box2 (remote) | **26% idle** | **36% idle** |

⇒ **The parallel work exists and is not being delivered.** `seg_serve_cmd`
(`prover/host/src/main.rs`) is single-threaded — it runs `ExecutorImpl::run()` *and* dispatches
segments *and* collects receipts — and it caps the aggregate at **~1.76x regardless of fleet size**.

⚠ Note this is the **segment** coordinator (the ephemeral `seg-serve` process), not the **board**
coordinator (`coordinator/server.py`). Two different things share the name.

### What it costs, and the two ways out

At a 1.76x ceiling the aggregate floors at **1,575 / 1.76 = 897 s** — above the entire 600 s budget,
so **ten minutes is unreachable at any fleet size today.**

| fix | fleet |
|---|---|
| neither | **IMPOSSIBLE at any N** |
| `seg-serve` dispatch only | **7 cards** |
| aggregate witness read only (§7.5) | 8-15 cards |
| **both** | **5-6 cards** |

✅ **Neither is architectural — both are ordinary software.** And they ship very differently:

- **`seg-serve` is HOST-side.** It moves no `METHOD_ID`, needs **no re-baseline and no board reset**,
  and can land on its own. It alone takes the target from impossible to reachable.
- **The witness read is guest source**, so it rides the re-baseline already queued with #139.

⇒ **Sequence `seg-serve` first.** It banks the largest single improvement — impossible to 7 cards —
without waiting on the fidelity decision, and it de-risks #139 stalling again.

⚠ **A two-point Amdahl fit on N=1,2 predicted 54 s at N=4; it measured 89 s.** Two points cannot
constrain a curve.

</details>

### Where this actually leaves the aggregate

| | status |
|---|---|
| aggregate distributes at all | ✅ **yes** — 1.81x on 2 cards, scenario (c) is dead |
| scales past 2 cards | ⛔ **UNMEASURED** — needs a THIRD box |
| is `seg-serve` a bottleneck? | ⛔ **UNKNOWN** — nothing here tested it |

⇒ **The fleet arithmetic in `TOPOLOGY_AND_SETTINGS.md` §0.5 stands on the 2-card measurement**, which
supports scenario (a) as far as it goes. The 7-9 card figure is intact; what is not established is
whether it holds at the fleet sizes that matter.

⇒ **The witness read (§7.5) is unaffected by any of this** — it shrinks the aggregate rather than
distributing it, and is worth ~3x on its own.

### 8.13 What it costs to reach ten minutes

From measured block-962,000 figures: 14,926 chunk card-seconds, 1.05x straggler, 1,575 s aggregate
(1,379 s segment proving + 196 s resolution).

| | 32 cards | 48 cards |
|---|---|---|
| **(a)** aggregate fully distributes (#153/#157/#161) | **9.0 min** ✅ | 6.0 min ✅ |
| **(b)** only its segments distribute; resolution serial | 12.2 min | **9.2 min** ✅ |
| **(c)** nothing distributes | 34 min ❌ | 31 min ❌ |

⇒ **The ten-minute block is now a purchasing decision: ~32 cards under (a), ~48 under (b).** The
sixteen-card target is not reachable — §8.5's chunk floor alone is 16.3 min — but the goal is, at
roughly twice the fleet. And §1.1's throughput framing, which needs ~28 cards for the same result,
is still unpriced and cheaper than all of them.

⏰ **Under the latency framing, the one remaining blocker is scenario (c), and it is the only one that
fails.** Whether the distributed aggregate works is claimed by #153/#157/#161 and has never been
exercised. It needs >= 2 boxes rather than one, and it separates "buy 32 cards" from "buy 48" from
"the fleet cannot get there".

⚠ **But §1.1, priced 2026-08-28, dissolves scenario (c) rather than solving it.** Under bounded-lag
throughput the aggregate never needs to distribute — several run concurrently on different blocks —
and the fleet comes out at **29 cards**. So the distributed-aggregate measurement is decision-critical
only if the product requires tip latency. **Settle the framing before paying for the measurement.**

⇒ **Ranked, what is left (revised 2026-08-28, after §1.1 was priced):**

1. **Decide the framing — latency or bounded lag.** §1.1 is now priced at **29 cards + ~36 min lag**
   against 32-48 cards at tip latency. It needs no GPU, no code and no measurement; it is a product
   decision, and **it determines whether items 2 and 4 matter at all.** Do this first.
2. **The distributed-aggregate check** (>= 2 boxes). Decision-critical *only* under the latency
   framing, where it separates "buy 32" from "buy 48" from "the fleet cannot get there". Under
   bounded lag it is an optimisation, because per-block aggregates run concurrently on different
   blocks. See §1.1.
3. **The chain fold's per-block cost** — the one term §1.1 could not price, and the only thing that
   could invalidate the bounded-lag framing outright. Minutes of work on any box that is up; pair it
   with item 2.
4. **#139** for ~1.1x on chunks — a fidelity decision, and §2's note that nobody has asked what the
   *smallest* concession worth the bar looks like still stands.
5. **po2 23 on a B200**, never quantified — and note it takes **two changes, not one**: raising
   `DEFAULT_MAX_PO2` also changes the computed `ALLOWED_CONTROL_ROOT` away from the constant baked
   into `risc0-circuit-recursion`, which is why the 2026-08-25 B200 run produced invalid proofs. The
   lift programs for po2 23 exist (`lift_rv32im_v2_23.zkr`, `MAX_CYCLES_PO2 = 24`), so it is a missed
   second step rather than a wall — but shipping a recomputed root is a compatibility decision, since
   proofs would stop verifying against stock `risc0-zkvm`. It also OOMs on a 46 GB L40S (79 GB peak),
   so it is B200-only. Lowest priority: unquantified, paid, and gated on a decision nobody has taken.

The kernel lever is closed (§8.11) and chunk cost is EC verification (§8.6), which fidelity rightly
protects.

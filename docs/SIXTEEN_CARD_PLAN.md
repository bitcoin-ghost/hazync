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
one card   chunks 14,367 s  +  aggregate 1,566 s  =  15,933 s
floor      ~83 s does not divide  (INFERRED from a two-worker run)
wall(N)    83 + (15,933 - 83) / N
```

✅ **The chunk term is now MEASURED, not modelled** (2026-08-27, §2.1). `FLEET_SIZING.md` §4 lists
"14.34 G is modelled" as reason #3 not to trust the card count; it measures **14.057 G**, so the model
was **+2.0%** high and that reason retires. Every figure below divides by a measured number now.

**Two different bars, and they are not the same number.**

| framing | what it means | bar at N=16 |
|---|---|---|
| **latency** | one block, start to finish, inside 600 s | **1.92x** on divisible work |
| **throughput** | keep up with the chain at a fixed lag | **1.66x** — `15,933 / (16 x 600)` |

Today: `wall(16) = 83 + 15,850/16 = 1,074 s = 17.9 min`.

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
| current canonical (`main`, v0.19.0) | `1d6c3792…` | **no receipts exist locally** |

**`HAZYNC_AGG_EXECUTE` has never been run by anyone**, and it moves to the card day. Nor can
arithmetic stand in for it: `reproduce/METHOD_ID` records the aggregate at `~1,137 M` cycles, which at
the measured L40S rate is 1,162 s of the 1,566 s total — but the same composition on the
pre-re-baseline figure (`3,636.4 M`) predicts ~3,500 s against a measured total of **>3,300 s**, i.e.
it overshoots the entire run. Aggregate segments do not prove at the chunk rate, so the
validation/resolution split is UNKNOWN until the real run happens.

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

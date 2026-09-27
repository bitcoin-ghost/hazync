# Seven launches, no blocks: an evening spent in the gates — 2026-09-27

**~$46 of RTX PRO 6000 time. Zero blocks proved. The hour never started.** Every launch died before
the clock, and the useful part of the evening is *why*, because four of the seven were one bug and
two more were mine.

Kept because the failure taught the tooling more than a clean run would have: `--adopt`,
`--min-cards` and `--grow-to` all exist because of this evening, and two of the three were written
while the fleet was still billing.

## Capacity: what was actually available

⛔ **Capacity, not money, caps a fleet.** 45 requested of each type with SECURE stock, within minutes
of each other:

| card | granted of 45 | $/hr |
|---|---|---|
| RTX 4090 | **1** | 0.74 |
| RTX PRO 4500 SE | **4** | 0.72 |
| RTX PRO 6000 | **38** | 2.09 |

Two hours later PRO 6000 granted **17**; forty minutes after that, **21**. `stockStatus` read `Low`
for every one of those numbers. ⚠ One catalogue call listed 17 types with no 4090 at all; the next,
minutes later, listed 12 with the 4090 at the top. **A survey is worthless by the time you act on
it.**

⚠ The type id matters: `NVIDIA RTX PRO 4500 Blackwell Server Edition`, not `NVIDIA RTX PRO 4500`. A
wrong id returns "no capacity", which is indistinguishable from absent stock — it cost one probe and
nearly changed the plan.

## The seven attempts

| # | fleet | died of | cost |
|---|---|---|---|
| A | 30 | **I sent SIGUSR1** expecting a thread dump. Unhandled → `REAL_EXIT=138` | ~$8 |
| B | 30 adopted | `UnboundLocalError: want` — a bug in the `--adopt` code written minutes earlier | seconds |
| C | 30 | `hz-smoke-8` never started; 29 of a required 30 | $16.66 |
| D | 29 adopted | `hz-smoke-21` `GPU_BAD`; 28 of a required 29 | $38.09 |
| E | 28 adopted | 25 of a required 26 after the network gates | — |
| F | 25 adopted | stopped by hand; already short | — |
| G | 21 | **stopped by the operator**: the fleet had shrunk to 10 | $0.68 |

**C, D, E and F are one bug**: `--cards` was checked as a *floor* at the end of ~8 pre-clock gates,
so a run holding 29 healthy gated cards of a requested 30 exited and released all 29 — after paying
for every gate. Fixed by `--min-cards` (#539).

**G is the opposite bug, and it is mine too.** #539 passed the floor to three call sites whose
argument is a *target*:

```
21 rented (uniform PRO 6000)
 → 16   wait_for_ssh(need=floor) stopped 44 s in, having reached the floor, and released five
        still-booting cards as "never answered ssh" — with 420 s of its timeout left
 → 10   slow_worker_cut(need=floor) trims DOWN TO `need`; six more gone in seven seconds, each
        reported as a card "the aggregate could not push to fast enough"
```

The operator stopped it because ten cards cannot hold tip pace. They were right, and the fleet was
small because of the change — capacity had just granted 21. Fixed in #541.

⛔ **The tell is in each callee and the call site does not show it.** `need` means *wait for this
many*, *trim to this many*, *is this type big enough* — all targets. Only "proceed or refuse" is a
floor.

## What the gates caught, and were right to

Every card dropped was genuinely bad, which is the part worth defending:

- `hz-smoke-8` — RunPod accepted the pod and never published a port. It never started.
- `hz-smoke-21` — crawled the 411 MB prover fetch (78% when the other 28 had finished 12 minutes
  earlier), then failed the GPU gate outright. **Both** faults, one card.
- three more failed card-to-card reachability, in a pattern where every block was all-reached or
  all-failed — the network, not the pods.

⚠ And one near-miss in the other direction: `hz-smoke-21` looked wedged at 78% and I was minutes from
killing the run. Measured twice instead: **78% → 99.8% in 40 seconds.** It finished. Measure a
laggard before you judge it.

## Measurements worth keeping

- **411 MB prover fetch: ~14 min cold across 29 cards, ~4 s cached.** It caches *on the pod*, so a
  relaunch onto surviving pods skips the longest gate entirely. This is most of `--adopt`'s value.
- **Launch → live dashboard: 2 min 08 s** on the one attempt that cleared the gates cleanly. The
  page was never slow; we never reached it.
- Fleet attrition through the gates: **28 → 25** on one run, **21 → 20** on another, before the
  floor/target bug is counted.
- A 30-card PRO 6000 fleet bills **$62.70/hr**; 21 bills $43.89/hr.

## Process failures, separately from the code

1. **I changed one number and relaunched, four times**, instead of stopping to ask why cards were
   being lost. The operator called it — *"wasting money on whims"* — roughly twenty minutes before I
   would have. **After the second identical failure, fix the rule, not the number.**
2. **A stale background watcher merged #540 ahead of #541**, defeating an ordering I had explicitly
   designed. A second stale watcher had already killed a rebase earlier the same evening. **Stop the
   old one before starting its replacement.**
3. **A change-only monitor is silent through a stall** — the exact fault being watched for. 11½
   minutes of nothing went unreported until the operator asked. Alert on *silence*, with the phase.

## What shipped because of it

| | |
|---|---|
| #537 `--adopt` | a fleet outlives the run that rented it. **The recovery path**, not a convenience: it held 30 cards across four driver deaths when capacity could not have replaced them |
| #539 `--min-cards` | target vs floor |
| #540 `--grow-to` | absorb capacity mid-session; also unfroze `assignment` (built once before the loop, so a growing fleet reached nothing) and `rate_hr` (fixed, so recruits billed invisibly) |
| #541 | the three target/floor confusions #539 introduced |
| #538 | the GPU gate's phase line read as though 29 blocks were being proved |
| [`../TIP_RUN_OPERATIONS.md`](../TIP_RUN_OPERATIONS.md) | the operator's manual this evening showed was missing |

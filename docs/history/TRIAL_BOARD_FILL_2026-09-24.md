# The second tip hour: the gaps go to the board

**2026-09-24 · 15× RTX 4090 · $11.10/hr · $11.03 billed · 15 blocks proved, 0 failed**

A controlled re-run of [the 2026-09-23 flagship hour](FLAGSHIP_TIP_HOUR_2026-09-23.md) on the
**same card type at the same fleet rate**, testing two changes made in between:

| | |
|---|---|
| hazync#506 | the gaps between tip blocks go to the proof-party board, and the tip takes them back |
| hazync#503 | the prover-fetch gate is fleet-relative, so a laggard is dropped in seconds, not 40 minutes |

Driver on merged `main` @ `57b8d53`. Claiming as **G H O S T**. Board frontier 124,595 at launch;
bridge exactly level with the chain (968,338 = `getblockcount`).

---

## What it did

```
13 board blocks   124,622 → 124,673    median 37.9 s   (range 35.3–45.9 s)
 1 step-aside     124,674 abandoned after 56.3 s — "tip block 968339 is waiting"
 2 tip blocks     968,339 in  783.1 s   digest ac4796dc
                  968,340 in 1910.0 s   digest 1085db21   (9,600 segments)
```

**The preemption fired live**, 11 minutes in:

```
[03:15:03]  stepped aside from 124674 after 56.3s: tip block 968339 is waiting
            and the tip comes first
[03:15:04]  proving 968339 (attempt 1)
```

One tick. The board claim was kept, not wasted, and the block stayed re-provable.

## The comparison it was built for

Same card, same rate, so the difference is the change and not the hardware:

| | flagship 2026-09-23 | this trial |
|---|---|---|
| fleet | 15× RTX 4090, $11.10/hr | 15× RTX 4090, $11.10/hr |
| blocks proved | 2 | **15** |
| fleet busy | 48% | **~91%** |
| idle | **31.4 min — $5.81 of nothing** | filled with board work |
| billed | $15.20 | $11.03 |

⚠ **The idle figures are not like-for-like on duration.** The flagship's hour contained a 23-minute
gap between blocks and then 17 more minutes of nothing; this one contained a 29-minute block. Both
are one hour of a Poisson process, and neither is the average of anything.

## ⛔ And it fell behind the chain, which the flagship did not

This is the headline the utilisation number hides:

| block | appeared | accepted | lag | blocks behind when accepted |
|---|---|---|---|---|
| 968,339 | 02:14:44Z | 02:28:17Z | 813.1 s | **1** |
| 968,340 | 02:28:18Z | 03:00:14Z | 1916.7 s | **3** |

968,340 carried **9,600 segments** and took **31.8 minutes**. The chain produced three more blocks
while we proved it. The flagship kept `blocks_behind: 0` on both of its blocks — not because it was
faster, but because its blocks were smaller (4,897 and 2,939 segments) and its gaps longer.

**Filling the gaps did not make the fleet faster, and was never going to.** It converts idle into
board proofs; it does nothing for the size of a tip block. 15 cards is not enough for a 9,600-segment
block at the tip, and this run is the clearest evidence of that we have.

## Cost, per block rather than averaged

⛔ A single "cost per block" is meaningless here: the run mixed 38-second board blocks with a
32-minute tip block. `fleet_rate × wall_clock` per block:

| | wall | cost |
|---|---|---|
| board block (median) | 37.9 s | **$0.117** |
| 968,339 | 783.1 s | **$2.415** |
| 968,340 | 1910.0 s | **$5.889** |

968,340 normalises to **$0.000613 per segment** — inside, and slightly better than, the flagship's
measured $0.00064–0.00074 band.

⚠ Segment counts are only available for 968,340. `agg.log` is cleared at the start of each block, so
the earlier blocks' totals were overwritten before harvest. That is a gap in the evidence, not a
rounding choice.

## Why 968,340 took 32 minutes

The fleet was spread across **six data centres** (`2× EU-CZ-1, 21× EU-RO-1, 1× EUR-IS-1,
1× EUR-IS-2, 2× US-NC-1` before the gates). Join round-trips on that block, 7,986 samples:

| min | p50 | p90 | max | mean |
|---|---|---|---|---|
| 0.4 s | 17.1 s | 55.5 s | **107.4 s** | 27.1 s |

A 268× spread. The fold waits for the slowest peer at each level, so the **tail** sets the wall
clock, not the median. ⚠ This is an observation, not an attribution — nothing here separates
geography from card-to-card routing, and the fleet was not built to.

## ⛔ Four defects, found by running it

None of these were visible from the code. Each cost something.

**#508 — reachability is predicted by network block.** The first attempt lost 7 of 17 workers at the
reachability gate and aborted at 11 against a floor of 15. Grouped by /24 the same numbers are a
different fact:

| block | reached | failed |
|---|---|---|
| `194.68.245.x` | 10 | 0 |
| `69.30.85.x` (the aggregate's **own**) | 0 | 4 |
| `63.141.33.x` | 0 | 3 |

Every block all-reached or all-failed. ⛔ The intuitive fix — rent within one data centre — points
the wrong way: the block that failed completely was the aggregate's own. The gate now reports this
shape before the clock, and explicitly refuses to diagnose it.

**#509 — the fleet-relative fetch gate cut the aggregate loose.** On #503's first live outing it
dropped `hz-smoke-1` for needing "~2 more min at 2424 KB/s" — and `hz-smoke-1` was the aggregate.
Every worker's reachability had been tested against it, so nothing could be promoted and a healthy
30-card fleet died 34 seconds in. A worker is fungible; the aggregate is not. Now an identity, not a
threshold.

**#510 — a stale collector owned the live page for eight hours.** The trial's own collector died 72
seconds after starting, on `os.replace(tmp, out)` — the temp name was a fixed sibling, and a
collector left over from the previous night's flagship renamed it away. ⛔ The outage was invisible:
the page updated once a second for the whole session, with an eight-hour-old run in it. *"Is the page
publishing?"* had a reassuring answer throughout; *"which run is it publishing?"* was never asked.

**#513 — the budget on disk was one block behind.** `session.json` recorded **$4.54** where RunPod
billed **$11.03**. The in-memory accounting was correct ($10.45 in the session's own summary); what
was missing is that nothing wrote it after the loop ended. ⚠ That file is not a report — a resumed
session reads `spend_usd` from it and checks `--budget-usd` against it, so the gap silently refills a
budget already spent. The last block spent 1,910 s inside one `prove()` call, and its cost was
accrued and then dropped.

## What this run does not show

- **Two tip blocks is not a sample.** Both lags exceeded ten minutes; one block was 2.4× the other.
- **Board block cost is measured on early-chain heights** (~124,600), where the utreexo accumulator
  is shallow. It says nothing about what board work costs nearer the frontier.
- **Nothing here tests board-fill against a busy board.** Claims were available whenever asked; the
  4-per-key cap was never approached.
- **The first two attempts aborted** (~$1.60 total) before this one ran — on #508 and #509
  respectively. Both released every pod and the account was verified clean each time.

## The evidence

Committed beside this file:

| | |
|---|---|
| `trial-2026-09-24-tip_ledger.jsonl` | appeared/accepted per tip block, with lag and blocks-behind |
| `trial-2026-09-24-billing.json` | RunPod's per-pod uptime, price and data centre |
| `trial-2026-09-24-session.json` | every block the session recorded, and what it thought it spent |
| `fleet-economics.jsonl` | both tip blocks, with po2, gpus_per_pod and segment counts |

⛔ Not committed, retained on the operator's box: **15 receipts** (13 board + 2 tip, ~230 KB each),
16 bundles, per-card telemetry and the harvested per-card logs — including the aggregate log the
join-RTT figures above come from. As with the flagship, the receipts are the artifact a third party
would want and they are **not** published anywhere.

# One hour following the Bitcoin tip — 2026-09-23

**Two blocks proved and accepted while the chain was still on them. $15.20. Every figure below is measured.**

## What happened

A fleet of **15× RTX 4090** was rented, gated, and then deliberately left **idle** — it refused to touch any block that existed before it was ready. It waited for Bitcoin to mine something new, and proved what arrived.

| | 968,315 | 968,316 |
|---|---|---|
| bundle appeared | 19:35:26Z | 19:58:56Z |
| proof accepted | 19:53:03Z | 20:10:54Z |
| **lag** | **1,056.4 s** | **717.8 s** |
| chain tip at acceptance | 968,315 | 968,316 |
| **blocks behind** | **0** | **0** |
| **segments** | **4,897** | **2,939** |
| inputs | 7,749 | 7,081 |
| prove wall clock | 1,012.0 s | 705.4 s |
| receipt | 234,298 B | 233,786 B |
| digest | `c38429b0` | `c5b4e9d8` |

`blocks_behind: 0` on both: when each proof landed, the chain had not moved past it.

## Cost

RunPod's own billing, read from the API **before** the pods were released — not a computed estimate.

**Cost of proving each block** — fleet rate × that block's own wall clock:

| block | segments | wall | **cost** | $/segment |
|---|---|---|---|---|
| 968,315 | 4,897 | 1,012.0 s | **$3.12** | $0.000637 |
| 968,316 | 2,939 | 705.4 s | **$2.17** | $0.000740 |

⚠ **Do not average these.** 968,315 carries **1.67× the segments** of 968,316 but cost only **1.43×** as much — so the *smaller* block was **13.9% dearer per segment**. The ~121 s fixed head per block does not shrink with the block, so light blocks amortise it worse. A single "cost per block" figure hides both the size spread and that relationship.

The normalised figure is the one that travels: **$0.00064–0.00074 per segment**. That sits inside the $0.0007 band measured across four other card types today, on different blocks and fleet sizes.

**Cost of the hour**, RunPod's own billing read from the API before release:

| | |
|---|---|
| 15 pods, uptime 4,850–4,972 s | **$15.20** |
| session ÷ blocks (excludes boot, includes idle) | $5.55 |
| all-in ÷ blocks | $7.60 |

Inside the 60-minute session: **28.6 min proving ($5.30)**, **31.4 min idle awaiting a block ($5.81)**.

That idle is not waste to be optimised away — it is what following the tip *is*. You hold a fleet ready for a block nobody has mined yet. It is also the single clearest argument for the changes below.

## Verified by a machine that did no proving

```
[verify] image id 37987b85ec665970… matches canonical
  968315  VERIFIED  sha256 7f45c276b5141a5c…
  968316  VERIFIED  sha256 9c06adb55600b00c…
  2 VERIFIED, 0 FAILED
```

Re-verified on the bridge host using the canonical `37987b85` binary — **not** the bridge's own `fb4d7352` guest, which a guard refuses. A receipt verified against the wrong image id proves nothing.

## What the hour taught us — 4 issues raised

An hour of real operation surfaced four concrete defects. None were known this morning; all are now filed with measurements attached.

**#503 — one slow card can hold an entire fleet for 40 minutes.**
A single pod fetched the 410 MB prover at **93 KB/s** while the other 17 finished in seconds. The stall detector could not see it: the card *was* progressing, just barely. The per-card ceiling is 40 minutes, so 17 idle GPUs billed while one crawled. Killing the download did not help — the fetcher restarts it by design. Terminating the pod was the only way through, and the gate cleared within seconds.
→ *The check must be fleet-relative: a card 20× slower than the median is a bad card, knowable in 30 seconds.*

**#504 — the fleet is fixed at boot, but capacity is not.**
Two capacity snapshots **two minutes apart** disagreed on 7 of 22 GPU types. Across the evening the same card went 26 available → 0 → 18. This run took 15 cards because that is what existed at 20:05, then held 15 for an hour while capacity returned around it. The transport already supports joining mid-block — workers dial in, reconnect, and work is assigned dynamically (measured: 16 cards finishing within **2 seconds** of each other). Only the driver assumes a constant fleet.
→ *Absorb cards as they appear.*

**#502 — two banks, leapfrogging.**
A single fleet pays a ~121 s head per block — fetching that block's bundle, arming cards, executing to the first segment. It cannot be prefetched, because the block does not exist yet, and the cards are busy. Only *different hardware* can hide it. Two banks alternate: one proves, the other is ready. It also absorbs burst arrivals — Bitcoin gaps are Poisson, and **39% are under 300 s**.
→ *A single fleet sized for the mean sits at 97.5% utilisation, which is not a system that keeps up.*

**#505 — the log said the wrong thing.**
While waiting for the chain, the run printed `idle: the board has nothing free right now` every 30 seconds. It was working perfectly. The message belongs to a different code path and was read, live, as the run having given up.
→ *Say what is actually being waited for.*

Those four sit on top of eleven fixed during the day, several found by watching this run's own dashboard lie: released spares rendered as failed cards, a block's cost billed to one card instead of the fleet, a "behind the chain" figure computed from an unfinished block, and a completion animation that re-fired forever.

## What this does not show

**Two blocks because the chain produced two.** The gap 968,315 → 968,316 was **23 minutes**; 968,317 had not appeared 17 minutes after that. The session correctly declined to start a third it could not finish. This is not "we managed two" — it is "we took both that existed".

**We were slower than ten minutes.** 1,056 s and 718 s. We stayed at the tip because the gaps were long, not because 15 cards is sufficient. On a median gap we would have fallen behind. 15 cards was what capacity allowed; the job wants roughly 28–36.

**Boot was inflated by a bug.** $3.98 of the $15.20, of which ~19 minutes was #503. A clean boot is about 2.5 minutes, roughly $0.46.

**Geography is recorded but not controlled.** Four data centres across two continents. Per-card timings mix card and network, and the frame says so.

## Evidence

`receipt_968315.bin`, `receipt_968316.bin`, `verification.json`, `billing.json`, `tip_ledger.jsonl`, 45 harvested card logs, 2,751 deduplicated frames, and the bundles both proofs were made from.

---

## Where the evidence lives

Committed beside this file:

| | |
|---|---|
| `flagship-2026-09-23-tip_ledger.jsonl` | appeared/accepted per block, with lag and blocks-behind |
| `flagship-2026-09-23-billing.json` | RunPod's per-pod uptime and charge (pod ids stripped) |
| `flagship-2026-09-23-verification.json` | independent re-verification, with each receipt's sha256 |
| `fleet-economics.jsonl` | both blocks, with po2, gpus_per_pod and segment counts |

⛔ **Not committed — too large for the repo, retained on the operator's box** at
`/home/defenwycke/hazync-flagship-20260923T190410Z`:

| | |
|---|---|
| `receipt_968315.bin`, `receipt_968316.bin` | 234,298 B / 233,786 B — the proofs themselves |
| `bundle_968315.json`, `bundle_968316.json` | 27 MB / 26 MB — the inputs they were made from |
| `frames/` | 308 MB, 2,751 deduplicated frames |
| `stream/` | 7.7 MB of 1 Hz per-card telemetry |
| `logs/` | 1.4 MB harvested from all 15 cards before release |

⚠ The receipts are the artifact anyone else would want to check. They are small enough to publish
and are **not** in git — if this run is cited publicly, they need somewhere to live first.

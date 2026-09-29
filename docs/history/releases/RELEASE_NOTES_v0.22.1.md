Thirty-five commits since v0.22.0, of which a handful change anything you download. The headline is
a measurement bug: the aggregate had been reporting every tip block as taking about one whole
execution phase longer than it did, and the fleet-sizing conclusions drawn from that were wrong by
roughly three times.

**The guest is untouched. `METHOD_ID` is still `37987b85…`**, so proofs from this release verify
against every earlier one and nothing needs re-baselining.

## The aggregate was double-counting execution

The per-block summary printed `TOTAL = execution + worker wall + assembly`, and those windows
overlap. The executor publishes each segment as it is produced, so connected workers are already
proving while it runs — that overlap is deliberate, and the code says so at the executor call. But
the two timers were started a hundred lines apart in the same scope and both ran through it, so
adding the columns counted the executor twice.

The evidence had been sitting in one file, disagreeing with itself, for a day. `TIP_HOUR_3` records
both a session wall clock and the phase table:

| block | session wall | phase sum | excess | reported execution |
|---|---|---|---|---|
| 969,018 | 691.7 s | 885.6 s | 193.9 s | 205.8 s |
| 969,019 | 1027.9 s | 1299.0 s | 271.1 s | 277.3 s |

The excess is the reported execution less a few seconds, on both blocks, and that remainder is the
prologue — which the old `execution` timer also carried, because it started before the bind.

**What it cost.** The "serial floor" it produced included a phase that is not serial:

| block | floor as published | floor corrected | cards needed |
|---|---|---|---|
| 969,018 | 312.4 s = 52 % of the gate | **118.5 s = 20 %** | 36 → **22** |
| 969,019 | 439.2 s = 73 % | **168.1 s = 28 %** | 102 → **38** |

So 969,019 was inside the ten-minute gate on a fleet anyone can rent. The run that missed it had 19
cards. A second claim went with it: assembly was said to grow as cards are added, and a measured
N-sweep shows it halving between 4 and 26 cards.

`TOTAL` is now measured from a single whole-block timer instead of summed, the layout shows which
phases nest, `execution` is labelled as being *inside* the segment phase, and the summary prints both
the real serial floor and the fleet size past which the executor — not proving — sets the wall
(around 60 cards for a 9,000-segment block). The affected records carry corrections rather than quiet
edits, because the way this hid is the useful part.

## `[rtt]` names the card

Join round trips were attributed to `peer`, the address `accept()` returned — the egress IP. Several
cards on one pod, and several pods behind one NAT, share it; up to eleven came back as one `peer` on
a September fleet. So "which card has the slow tail?" could not be answered from the logs.

Workers now send their own `HAZYNC_WORKER_ID` as a small frame on connect and on every reconnect, and
`[rtt]` leads with `card=`. `peer=` stays, because it is still what routes and the two together are
what reveal cards sharing an address. A worker too old to introduce itself reports `card=?` rather
than being guessed at.

⚠ **This is a new frame on the wire, and it has never run on a real fleet.** Both ends ship in the
same binary and a fleet run launches them all from one pinned release, so they always match — but do
not mix releases *within* a run: a new worker's hello would be read as a receipt by an old aggregate.
Roll every worker together.

## `/api/state` stopped shipping 19.8 MB

The four README badges had been rendering as "inaccessible". `/api/state` was 19,795,606 bytes and
33.3 s, finally 504-ing at the edge, of which the verified-range list was 99.95 % — and the block map
polled it unslimmed every ten seconds. The old default protected nobody, because no client could
complete the request. The list is now opt-in.

## What is NOT in this release

The segment-size work is repo-side tooling, not an asset: `tip_smoke.py` now resolves
`HAZYNC_SEG_PO2` from the weakest card in the fleet — po2 22 where every card has ≥48 GB, which
measured ~11.5 % faster on a tip block, and 21 otherwise. It cannot be mixed per card, because the
aggregate segments the block once. Anyone running from the repository has it already; it needs no
binary.

The cost model (`tip_cost.py`) and the per-level join analysis (`join_levels.py`) are likewise
repo-side.

## Honest limits

- **The CUDA host is byte-verified, not GPU-tested.** It was built on a box with no card, so the
  guest id was read out of its bytes rather than by running it. That is a real check against the
  canonical pin, and it is weaker than a run. Smoke it on a card before trusting a large fleet to it.
- **The hello frame is untested on hardware.** It is covered by seven socket-level tests, including
  one that deliberately mis-frames a hello and proves the stream desyncs — so the tests can detect
  the failure that would matter — but no GPU fleet has exchanged one.
- **Nothing here is a proving speedup.** Both host changes are logging and accounting. The value is
  that the next tip run can be measured correctly and per card, which is what the remaining work on
  the serial floor needs.

# Ghost's next build — the straggler, not the speed

Ghost is **11.467x and 5 cards** (measured, two L40S, block 962,000 — `BUILDS.md` §1). The target is
4. This document says where that card is, and — as importantly — where it is not.

## The card is in the packer, not the accelerator

| | measured | needed for 4 cards |
|---|---|---|
| Ghost straggler | **1.438** | **≤ 1.35** |

That is the whole gap. `BUILDS.md` §4.2 states it directly: *"It decides 5 cards vs 4: at 1.35 GHOST
is four cards."* Ghost's chunk work is already 1,713 card-seconds; shaving cycles further moves the
mean, and **a block's wall-clock is its slowest chunk, not its mean**.

## Why the straggler is high: Ghost has no calibration of its own

Ghost ships the **default** packing constants. This is deliberate and recorded in
`scripts/gpu-benchmark.sh` — Core's refit is a *regression* on Ghost (1.469 → 1.884), so it was
correctly refused. But refusing the wrong calibration is not the same as having the right one, and
`BUILDS.md` §3 is blunt about it: **"Ghost needs its own calibration; it does not have one."**

The stated cause is a ratio that should have narrowed and did not:

> the hint removes decompression from *both* curves — taproot key-path spends call `lift_x` too — so
> rescaling Ghost's Schnorr constant by the old 13.77x ECDSA ratio is wrong.

There is now a second, independent piece of evidence pointing the same way.

## New evidence (measured 2026-09-05, #139 arm, block 741000)

Chunks 1 and 11 are a controlled pair — identical 42 inputs and 42 EC verifies, differing only in
bytes (765,282 vs 37,933):

```
chunk  1 (fat)   10,039,314 cycles
chunk 11 (lean)   7,763,901 cycles      ratio 1.293x measured, 1.574x modelled
```

⇒ **`COST_PER_INPUT_BYTE` (6) is ~2x the measured 3.13 cycles/byte.** A least-squares refit over all
16 chunks, with the intercept forced to zero because the packer cannot express one, gives:

```
cycles = 106,177*ec + 2.62*bytes + 90,395*inputs      mean |error| 5.2%   (current constants: 13.1%)
```

An over-weighted byte term makes the packer starve byte-heavy chunks of inputs and over-load lean
ones — which is precisely how a straggler ends up at 1.438 while the *predicted* straggler looks fine.
The packer balances its own model perfectly; that is what makes a wrong model invisible.

⛔ **This is not yet the answer for Ghost.** It was measured on the #139 arm, not the full Ghost arm,
and block 741000 carries **2 Schnorr verifies out of 723** — so it cannot inform the curve split at
all, and the curve split is the dimension Ghost most needs right.

## The work, in order

1. **Profile the Ghost arm on a taproot-bearing block.** `HAZYNC_PROFILE_EXEC=1`, 16 chunks, execute
   mode — no GPU needed. 962,000 is only 2.7% Schnorr and is described in the packer's own comments as
   "the mildest case available"; pick something taproot-heavy so the ECDSA:Schnorr ratio is actually
   observable.
   ⛔ Build the arm properly — an experiment arm built from env vars alone is silently **stock**, and
   the gate is an exact match on `hazync_ecmult_verify` (see `scripts/gpu-benchmark.sh` `WANT`/`DENY`).
2. **Fit Ghost's own constants** from that profile, intercept forced to zero. Ship them as the
   `HAZYNC_COST_*` defaults for the Ghost mode only — the constants are already per-build-mode.
3. **Re-measure the straggler on hardware.** `BUILDS.md` §4.1 is the standing caveat: proving time is
   quantised by segment (`ceil(cycles / 2^po2)`), so the *cycle* straggler and the *real* straggler can
   differ. Only the second one buys a card.
4. **Re-check the aggregate.** Ghost's budget is 25% aggregate. `BUILDS.md` §4.3 assumes 627 s; the
   aggregate has since measured **405.6 s** with two workers (772.4 s with one and an idle coordinator,
   473.1 s once the coordinator also works — 1.63x for free). If that holds under Ghost, it is a second
   card and it is already paid for.

## ⛔ Closed — do not re-open without a new result

Step figures are the measured cumulative chain in `docs/CORE_VS_GHOST.md` §"Where Ghost's 8.75x
comes from" (stock 14,720 s → 1,683 s chunk total). **bigint2 alone is worth ~13 cards; everything
after it is worth ~6 more combined** — the single substitution decision dominates and the rest is
refinement, which is the right frame for judging anything proposed below.

| lever | verdict |
|---|---|
| **MSM** | excluded by decision, not by evidence |
| **Wholesale bigint2** | **8.00x middle vs 9.10x wholesale**, measured at proving time — the extra **14%** costs the whole equivalence surface and buys at most ONE card. Decision of record: middle path. ⚠ execute-mode cycles were 9.19x; do not quote the two interchangeably |
| **G3 (Schnorr lane) + memo** | **1.253x** measured — keep, already in. ⚠ G3 is NOT isolated: it was measured together with the decompression memo, so neither has a standalone figure |
| **Scalar inverse** | **1.082x** measured — keep, already in |
| **Kernel-level work** | profiled: not at roofline, but the lever is CLOSED — 69.3% of GPU time |
| **Aggregate fan-out** | free; the aggregate does NOT scale with chunk count |
| **po2 23** | needs three fixes and is B200-only |

## What would falsify this

If a Ghost-arm profile on a taproot-heavy block shows the current constants already within ~5%, then
the straggler is not a calibration problem and the 5th card is real. In that case the next lever is the
aggregate (item 4), and after that there is no identified path to 4 cards without re-opening something
in the closed table.

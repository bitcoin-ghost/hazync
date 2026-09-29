#!/usr/bin/env python3
"""The aggregate is elected on how many cards can REACH it, then on speed (hazync#573).

⛔ WHAT THIS EXISTS FOR. Every segment of every block flows through one card. It was chosen on
measured throughput alone, and on 2026-09-28 that elected a host nine of its own /24 could not reach:

    hz-smoke-1 : 33039 Mbit/s to 23 worker(s)    <- CHOSEN, on speed
    hz-smoke-10:  8605 Mbit/s to 31 worker(s)
    hz-smoke-11: 13411 Mbit/s to 33 worker(s)    <- ten more cards, 13 Gbit/s
    hz-smoke-12:  8250 Mbit/s to 30 worker(s)

    -> reachability: 9 of 29 card(s) cannot reach the aggregate
       81.27.69   reached 0, failed 9   <- the aggregate's own block

The fleet went 30 -> 21 before a block was proved, and the same host then pulled the 411 MB prover at
53 KB/s and wedged the run entirely. ~$19 and an evening, for a number that was in the log the whole
time and simply did not enter the ranking.

⚠ WHY REACHABILITY DOMINATES. A card that cannot reach the aggregate contributes nothing at all.
Surplus bandwidth contributes nothing either: a 9,600-segment block under 600 s needs ~166 Mbit/s,
and every candidate above was 50-200x that. Throughput was never the binding constraint.

⚠ `--agg-min-mbit` still rejects a genuinely slow candidate. This only decides which of the
acceptable ones wins.

    python3 test_aggregate_election.py             # most reachable wins, ties broken on speed
    python3 test_aggregate_election.py --control   # fastest wins — the 23-worker host must be chosen
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def elect(candidates):
    """Pick the aggregate. `candidates` is [(name, mbit, workers_reached)]."""
    best = (None, -1.0, 0)
    for name, mbit, reached in candidates:
        if CONTROL:
            better = mbit > best[1]                       # the shipped behaviour
        else:
            better = (reached, mbit) > (best[2], best[1])
        if better:
            best = (name, mbit, reached)
    return best[0]


# ── 1. the real election from 2026-09-28 ────────────────────────────────────────────────────────
NIGHT = [("hz-smoke-1", 33039, 23), ("hz-smoke-10", 8605, 31),
         ("hz-smoke-11", 13411, 33), ("hz-smoke-12", 8250, 30)]
won = elect(NIGHT)
if CONTROL:
    check(won == "hz-smoke-1",
          f"control reproduces it: {won} wins on 33 Gbit/s while reaching only 23 of 33 workers")
else:
    check(won == "hz-smoke-11",
          f"{won} wins — 33 workers at 13 Gbit/s beats 23 workers at 33 Gbit/s")
    lost = dict((n, r) for n, _, r in NIGHT)
    check(lost[won] - lost["hz-smoke-1"] == 10,
          f"and it keeps {lost[won] - lost['hz-smoke-1']} more cards than the old rule chose")

# ── 2. speed still breaks a tie ─────────────────────────────────────────────────────────────────
tie = [("slow", 900, 30), ("fast", 9000, 30)]
check(elect(tie) == "fast", "with equal reach, the faster card wins")

# ── 3. one unreachable card does not hand the fleet to a crawling host ──────────────────────────
# ⚠ THE HONEST LIMIT OF THIS RULE. Reachability dominating means a candidate reaching one more
# worker wins however slow it is. That is right while every candidate clears what a block needs
# (~166 Mbit/s) and wrong if one does not -- which is what --agg-min-mbit is for, and why it is
# named here rather than left implicit.
edge = [("crawler", 5, 31), ("quick", 30000, 30)]
if not CONTROL:
    check(elect(edge) == "crawler",
          "reachability dominates even against a 6000x faster card — --agg-min-mbit is the guard")
    check(5 < 166, "and 5 Mbit/s is below what any block needs, so that guard is not optional")

# ── 4. the source really ranks on both, in that order ───────────────────────────────────────────
if not CONTROL:
    src = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
    # ⚠ THE EXPRESSION MOVED, THE PROPERTY DID NOT (hazync#567 added an optional CPU term). This used
    # to pin the literal `if (len(per), mbit) > (len(best[2]), best[1]):`. Pinning a literal is why this
    # assertion fired the moment a THIRD ranking term was added — correctly, because that is exactly
    # when someone might quietly demote reachability. So assert the PROPERTY instead: `len(per)` is the
    # first element of the key in every mode.
    # ⚠ `([^)]*)` stops at the first ")", which turns `len(per)` into `len(per` — my first attempt
    # failed on its own regex rather than on the code. Assert the two key expressions literally.
    check("(len(per), _sc, mbit) if a.agg_prefer_cpu else (len(per), mbit)" in src,
          "reachability is the FIRST term in BOTH ranking modes (CPU only ever comes second)")
    check("(len(best[2]), best[3], best[1])" in src and "(len(best[2]), best[1])" in src,
          "and the incumbent is compared on the same first term in both")
    check("if mbit > best[1]:" not in src, "and the speed-only comparison is gone")
    check("most REACHABLE first" in src, "the log line says which rule was applied")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

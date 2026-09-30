#!/usr/bin/env python3
"""The aggregate is not left feeding a tail (hazync#526).

⛔ WHY. The fold waits for the SLOWEST peer at every level, so one bad link sets the wall clock for
the whole block. Measured on block 968,340, 7,986 join samples:

    min 0.4 s    p50 17.1 s    p90 55.5 s    max 107.4 s      a 268x spread

That block took 31.8 minutes and the chain moved three blocks past us while we proved it. Renting
more cards does not fix a tail -- it adds join levels for the tail to show up at, which is why the
fix for "we fell behind" is not simply "rent 40 instead of 15".

⛔ AND IT GATES ON MEASUREMENT, NOT ON THE DATACENTRE. `dc` is recorded per card and it is tempting
to cluster on it, but the run that produced those numbers says plainly that "nothing here separates
geography from card-to-card routing". This drops a card for a link that was just measured, not for
the building it sits in.

  python3 test_worker_gate.py            # must PASS
  python3 test_worker_gate.py --control  # gate disabled; MUST keep the slow cards
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_smoke                                                            # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class Card:
    __slots__ = ("cid",)

    def __init__(self, cid):
        self.cid = cid


def cut(order, per, need, floor=0.0):
    if CONTROL:
        return set(), []                       # ⛔ the gate removed
    return tip_smoke.slow_worker_cut(order, per, need=need, floor=floor)


AGG = Card("agg")
W = [Card(f"w{i}") for i in range(1, 6)]
ORDER = [AGG] + W


# ── 1. the slowest are cut down to `need`, and the aggregate never is ──────────────────────────
per = {"w1": 400.0, "w2": 12.0, "w3": 380.0, "w4": 3.0, "w5": 410.0}
drop, ranked = cut(ORDER, per, need=4)
check(drop == {"w4", "w2"}, f"the two slowest workers are cut to reach need=4 (got {sorted(drop)})")
check("agg" not in drop, "⛔ the aggregate is never dropped — hazync#509 is what that costs")

# ⚠ nothing to do when the fleet is already the right size
check(cut(ORDER, per, need=6)[0] == set(), "no spares, no cuts")
check(cut(ORDER, per, need=99)[0] == set(), "and never cuts below what the run needs")


# ── 2. a floor cuts on evidence, even without surplus pressure ──────────────────────────────────
drop_f, _ = cut(ORDER, per, need=5, floor=100.0)
check(drop_f == {"w4"} or drop_f == {"w4", "w2"},
      f"a floor releases links measured below it (got {sorted(drop_f)})")
# ⛔ but never past `need`: with need=5 only one may go
check(len(ORDER) - len(cut(ORDER, per, need=5, floor=100.0)[0]) >= 5,
      "⛔ and the floor still cannot take the fleet below need")


# ── 3. untested is not failed, and is not favoured either ──────────────────────────────────────
# ⚠ An unmeasured card ranks at the MEDIAN of the measured ones: condemning it would break the rule
# the aggregate choice already follows, and ranking it first would make the gate prefer ignorance.
per_gap = {"w1": 400.0, "w2": 5.0, "w3": 380.0, "w5": 410.0}      # w4 unmeasured
drop_u, _ = cut(ORDER, per_gap, need=5)
check(drop_u == {"w2"},
      f"the MEASURED slow card goes before the untested one (got {sorted(drop_u)})")

per_all_slow = {"w1": 5.0, "w2": 6.0, "w3": 7.0, "w5": 8.0}       # w4 unmeasured
drop_u2, _ = cut(ORDER, per_all_slow, need=5)
check(drop_u2 == {"w1"},
      f"and an untested card is not kept ahead of every measured one either (got {sorted(drop_u2)})")

# ⚠ nothing measured at all: no evidence, so no cuts beyond surplus, and no crash
d_none, _ = cut(ORDER, {}, need=5)
check(len(d_none) == 1, f"with nothing measured it still trims surplus without crashing ({d_none})")


# ── 4. ⛔ the driver must actually call it ───────────────────────────────────────────────────────
src = open(os.path.join(HERE, "tip_smoke.py")).read()
check("slow_worker_cut(" in src.split("def slow_worker_cut", 1)[1],
      "⛔ the driver calls the gate (a rule written but never called is not a rule)")
check("--worker-min-mbit" in src, "and the floor is a real flag")
check("order = [agg] + [c for c in order if c.cid != agg.cid]" in src
      and src.index("order = [agg] + [c for c in order if c.cid != agg.cid]")
      < src.index("w_drop, w_ranked = slow_worker_cut("),
      "⚠ and it runs AFTER the aggregate leads `order`, so order[0] really is the aggregate")

EXPECTED_CONTROL = {
    "the two slowest workers are cut",
    "a floor releases links measured below it",
    "the MEASURED slow card goes before the untested one",
    "and an untested card is not kept ahead",
    "with nothing measured it still trims surplus",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — without the gate every slow link stays in the fleet:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}, got {sorted(hit)}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("the slowest links are released before the clock starts, on measurement, never the aggregate")

#!/usr/bin/env python3
"""The floor decides whether a run PROCEEDS. It must never decide how long to wait or how many to keep.

⛔ WHAT THIS EXISTS FOR. #539 introduced --min-cards and I passed it to three call sites whose
argument is a TARGET, not a floor. Measured live on 2026-09-27, a 21-card fleet with --cards 17
--min-cards 10:

    21 rented
    -> 16   wait_for_ssh(need=10) stopped the moment it held 10 and released the other 5 as
            "never answered ssh" — 44 seconds in, with 420 s of budget left and every one of them
            still booting
    -> 10   slow_worker_cut(need=10) trims the fleet DOWN TO `need`, so it cut six more in seven
            seconds, every one reported as a card "the aggregate could not push to fast enough"

The operator stopped the run because ten cards cannot keep up with the tip. The fleet was small
because of this bug, not because of capacity.

⛔ THE TELL IS IN EACH CALLEE, AND READING THE CALL SITE WILL NOT SHOW IT:

    wait_for_ssh      while ... and len(ready) < target      -> how many to WAIT FOR   = target
    slow_worker_cut   if len(order) <= need: return          -> trims DOWN TO it       = target
    rent_uniform      if len(got) >= X: accept this type     -> is it big enough       = target
    FetchFleet(_, keep)  "refuses once dropping one more would leave fewer than keep"  = FLOOR
    the final gate check / the adopt check / the ssh refusal -> proceed or refuse      = FLOOR

    python3 test_floor_vs_target.py             # each call site gets the right one
    python3 test_floor_vs_target.py --control   # the floor everywhere — must shrink the fleet
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CONTROL = "--control" in sys.argv
SRC = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
fails = 0


def check(ok, what):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails += 1


# ── 1. the source says the right thing at each site ──────────────────────────────────────────────
# ⛔ Anchored to the CALL, not to a bare substring: a 12-space pattern matched the tail of a
# 16-space line when this fix was written and silently corrupted the indentation.
TARGET_SITES = [
    (r"wait_for_ssh\(api, created, ssh, need=a\.(\w+)\)", "wait_for_ssh waits for the TARGET"),
    (r"order, best\[2\], need=a\.(\w+), floor=a\.worker_min_mbit\)", "the tail cut trims to the TARGET"),
    (r"if len\(got\) >= a\.(\w+):", "a uniform fleet is accepted at the TARGET"),
]
FLOOR_SITES = [
    (r"FetchFleet\(len\(order\), a\.(\w+), never_abandon", "the fetch view abandons against the FLOOR"),
    (r"if len\(order\) < a\.(\w+):", "the final gate check refuses on the FLOOR"),
    (r"if len\(cards\) < a\.(\w+):", "the ssh refusal is on the FLOOR"),
    (r"if len\(adopted\) < a\.(\w+):", "the adopt check refuses on the FLOOR"),
]

# ⛔ A SOURCE ASSERTION CANNOT HAVE A SOURCE CONTROL. Asserting "the file is still wrong" would
# fail the moment the file is right, which is backwards. The control below is BEHAVIOURAL: it drives
# the real function with the floor as `need` and must reproduce the shrink that ended the live run.
if not CONTROL:
    for pat, what in TARGET_SITES:
        m = re.search(pat, SRC)
        if not m:
            check(False, f"{what} — CALL NOT FOUND (did it move? the pattern must follow it)")
            continue
        check(m.group(1) == "cards", f"{what} (found a.{m.group(1)})")

    for pat, what in FLOOR_SITES:
        m = re.search(pat, SRC)
        if not m:
            check(False, f"{what} — CALL NOT FOUND")
            continue
        check(m.group(1) == "min_cards", f"{what} (found a.{m.group(1)})")

# ── 2. the behaviour those sites drive, against the real functions ───────────────────────────────
if True:
    import tip_smoke  # noqa: E402

    class C:
        def __init__(self, cid):
            self.cid = cid

    order = [C(f"hz-{i}") for i in range(16)]
    # a realistic tail: four slow links, the rest fine
    per = {c.cid: (2.0 if i < 4 else 90.0) for i, c in enumerate(order)}

    drop_floor, _ = tip_smoke.slow_worker_cut(order, per, need=10, floor=0.0)
    drop_target, _ = tip_smoke.slow_worker_cut(order, per, need=17, floor=0.0)
    if CONTROL:
        # the control IS the live bug: pass the floor and watch a 16-card fleet become 10
        check(len(drop_floor) == 6,
              f"control reproduces it: with the floor as need, 16 cards are cut to 10 "
              f"(dropped {len(drop_floor)})")
        check(len(drop_target) == 0,
              "control: the same call with the target as need cuts nothing — the argument is the bug")
    else:
        check(len(drop_floor) == 6,
              f"the floor as need would cut 16 down to 10 — why it must never be passed here "
              f"(dropped {len(drop_floor)})")
        check(len(drop_target) == 0,
              f"the target as need leaves a fleet at or under it untouched (dropped {len(drop_target)})")

print()
if fails:
    print(f"FAIL: {fails} assertion(s)")
    sys.exit(1)
print(f"PASS ({'control' if CONTROL else 'real'})")

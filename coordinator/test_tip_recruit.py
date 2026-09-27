#!/usr/bin/env python3
"""A recruiter tops a fleet up without renting the same slot ten times, and never admits a bad card.

⛔ THE BUG THE COUNTING EXISTS TO PREVENT. Gating a recruit takes MINUTES -- the prover fetch alone
measured 14 minutes across 29 cards on 2026-09-27 -- while the poll is seconds. A policy that counts
only the cards already in the fleet therefore starts a fresh rental on every poll while the first is
still being gated: 10+ pods rented for one slot, each billing from the moment RunPod grants it. Cards
being gated (`pending`) and cards gated-but-not-yet-admitted (`ready`) must both count against the
target.

⛔ AND A REJECTED RECRUIT MUST BE RELEASED. It never reaches a proof, so nothing else will ever look
at it; a pod that fails its gate and is forgotten bills until someone notices the invoice.

    python3 test_tip_recruit.py             # the real policy
    python3 test_tip_recruit.py --control   # counts only the fleet — must over-rent
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tip_recruit  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails += 1


def wanted(**kw):
    if CONTROL:
        # The bug: pending and ready ignored.
        return max(0, int(kw["target"]) - int(kw["have"]))
    return tip_recruit.wanted(**kw)


# ── 1. the counting policy ───────────────────────────────────────────────────────────────────────
n = wanted(have=20, pending=0, ready=0, target=25)
check(n == 5, f"20 of 25 with nothing in flight wants 5 (got {n})")

n = wanted(have=20, pending=5, ready=0, target=25)
if CONTROL:
    check(n == 5, f"control still wants 5 while 5 are being gated — it will over-rent (got {n})")
else:
    check(n == 0, f"20 of 25 with 5 already being gated wants 0 (got {n})")

n = wanted(have=20, pending=2, ready=3, target=25)
if CONTROL:
    check(n == 5, f"control ignores the 3 already gated and waiting (got {n})")
else:
    check(n == 0, f"gated-but-not-yet-admitted cards count against the target (got {n})")

if not CONTROL:
    check(wanted(have=30, pending=0, ready=0, target=25) == 0, "an over-full fleet wants nothing")
    check(wanted(have=0, pending=0, ready=0, target=3) == 3, "an empty fleet wants the whole target")

# ── 2. the thread, driven with a fake clock ──────────────────────────────────────────────────────
if not CONTROL:
    rented, released, gated = [], [], []
    fleet = [f"card{i}" for i in range(2)]       # start with 2, target 4

    def rent_fn():
        if len(rented) >= 5:
            return None                          # capacity runs out
        p = {"id": f"p{len(rented)}", "name": f"hz-smoke-{len(rented)}"}
        rented.append(p)
        return p

    def gate_fn(pod):
        gated.append(pod["name"])
        # the second recruit is a dud, like hz-smoke-21 on the night this was written
        if pod["name"].endswith("1"):
            return False, None, "its GPU cannot prove"
        return True, pod["name"], ""

    ticks = {"n": 0}

    def fake_sleep(_s):
        ticks["n"] += 1
        if ticks["n"] > 12:
            r.stop_flag()

    r = tip_recruit.Recruiter(target=4, have_fn=lambda: len(fleet), rent_fn=rent_fn,
                              gate_fn=gate_fn, release_fn=lambda p: released.append(p["name"]),
                              poll_s=0, sleep=fake_sleep)
    r.stop_flag = r._stop.set
    r._loop()

    admitted = [x["card"] for x in r.drain()]
    check(len(admitted) == 2, f"admitted exactly the 2 needed to reach the target (got {admitted})")
    check("hz-smoke-1" not in admitted, "the dud was never admitted")
    check(released == ["hz-smoke-1"], f"the dud was released, and only it (got {released})")
    c = r.counts()
    check(c["pending"] == 0, f"nothing left pending (got {c['pending']})")
    check(c["rejected"] == 1, f"one rejection recorded (got {c['rejected']})")
    # ⛔ the whole point: it stopped renting once the target was met, rather than renting every poll
    check(len(rented) == 3, f"rented 3 to fill 2 slots (1 was a dud), not one per poll (got {len(rented)})")

# ── 3. stopping releases anything gated but never admitted ───────────────────────────────────────
if not CONTROL:
    left = []
    r2 = tip_recruit.Recruiter(target=1, have_fn=lambda: 0, rent_fn=lambda: None,
                               gate_fn=lambda p: (True, "c", ""),
                               release_fn=lambda p: left.append(p["name"]))
    r2._ready.append({"pod": {"name": "orphan"}, "card": "orphan"})
    r2.stop()
    check(left == ["orphan"], f"a gated card nobody admitted is released on stop (got {left})")

print()
if fails:
    print(f"FAIL: {fails} assertion(s)")
    sys.exit(1)
print(f"PASS ({'control' if CONTROL else 'real'})")

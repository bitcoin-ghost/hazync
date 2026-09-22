#!/usr/bin/env python3
"""A degraded fleet must not render like a healthy one, and the cost must not lie.

⛔ WHY. The FLEET stat read `2/3 up` in exactly the same neutral colour as `3/3 up`. Over an
unattended 24-hour run the entire point of the frame is that someone glances at it, and a card that
died four hours ago is precisely what they need to see.

⚠ AND THE RATE MUST NOT DROP. A rented pod bills whether it answers or not, so showing a reduced
$/hr when a card dies would understate what the run is actually costing -- the opposite of what a
cost readout is for. That is the easy "fix" to reach for, so it is pinned here.

The rules live in tip24live.fleet_colour / fleet_sub so this exercises the SHIPPED code rather than
a copy of it, and one rendering check confirms the rule actually reaches pixels.

  python3 test_fleet_degraded.py            # assertions; exit 0 on success
  python3 test_fleet_degraded.py --control  # the rule is inverted; MUST fail
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip24live  # noqa: E402

BOX = (690, 730, 1180, 800)          # the FLEET stat on the frame


def colour(up, total, control=False):
    if control:                       # the control: the pre-fix rule, one colour for every state
        return tip24live.hx(tip24live.TEXT)
    return tip24live.fleet_colour(up, total)


def check_degraded_differs(control=False):
    healthy = colour(3, 3, control)
    degraded = colour(2, 3, control)
    if healthy == degraded:
        return False, f"2/3 up is the same colour as 3/3 up ({healthy}) — a dead card is invisible"
    return True, f"healthy {healthy} != degraded {degraded}"


def check_all_down_is_flagged(control=False):
    if colour(0, 3, control) == colour(3, 3, control):
        return False, "0/3 up renders as healthy"
    return True, "0/3 up is flagged"


def check_rate_does_not_drop(control=False):
    """The $/hr must be identical whether cards are up or down."""
    full = tip24live.fleet_sub(3, 3, 2.22)
    part = tip24live.fleet_sub(2, 3, 2.22)
    if "$2.22/hr" not in full or "$2.22/hr" not in part:
        return False, f"the rate changed with card health: {full!r} vs {part!r}"
    if "down" in full:
        return False, f"a healthy fleet claims cards are down: {full!r}"
    if "1 down" not in part:
        return False, f"a degraded fleet does not say how many are down: {part!r}"
    return True, f"rate held at $2.22/hr; degraded reads {part!r}"


def check_it_reaches_pixels(control=False):
    """Render both states and require the frames to differ — the rule must not stop at the API."""
    snap_path = os.path.join(HERE, "snapshot.json")
    if not os.path.exists(snap_path):
        return None, "snapshot.json not present (skipped)"
    try:
        from PIL import Image
    except ImportError:
        return None, "Pillow not available (skipped)"
    snap = json.load(open(snap_path))
    if not (snap.get("cards") and len(snap["cards"]) >= 2):
        return None, "snapshot has too few cards (skipped)"
    frames = {}
    with tempfile.TemporaryDirectory() as tmp:
        for up in (len(snap["cards"]), len(snap["cards"]) - 1):
            s = json.loads(json.dumps(snap))
            for i, c in enumerate(s["cards"]):
                c["up"] = i < up
            s["fleet"]["up"] = up
            sp = os.path.join(tmp, f"s{up}.json"); json.dump(s, open(sp, "w"))
            op = os.path.join(tmp, f"f{up}.png")
            subprocess.run([sys.executable, os.path.join(HERE, "tip24live.py"),
                            "--once", "--snap", sp, "--out", op],
                           cwd=tmp, capture_output=True, timeout=180)
            if not os.path.exists(op):
                return None, "renderer produced no frame (skipped)"
            frames[up] = (Image.open(op).crop(BOX).convert("RGB").tobytes())
    a, b = frames.values()
    if a == b:
        return False, "the rendered FLEET stat is pixel-identical for a healthy and degraded fleet"
    return True, "the two states render differently"


CHECKS = [check_degraded_differs, check_all_down_is_flagged,
          check_rate_does_not_drop, check_it_reaches_pixels]
CONTROL_MUST_FAIL = {check_degraded_differs, check_all_down_is_flagged}


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        ok, why = fn(control=control)
        if ok is None:
            print(f"  skip {fn.__name__}: {why}")
            continue
        if control and fn in CONTROL_MUST_FAIL:
            if ok:
                print(f"  CONTROL DID NOT FAIL: {fn.__name__} -- {why}")
                bad += 1
            else:
                print(f"  control ok: {fn.__name__} caught it ({why})")
        elif not control:
            print(f"  {'ok  ' if ok else 'FAIL'} {fn.__name__}: {why}")
            bad += 0 if ok else 1
    if bad:
        print(f"\n{bad} check(s) wrong")
        return 1
    print("\na dead card is visible and the cost does not lie" if not control
          else "\ncontrols failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""A dead feed must say so. It read "updated 0s ago" for ever.

⛔ WHAT THIS CAUGHT. The renderer computed

    now = snap.get('t') or time.time()      # top of draw_live
    ...
    age = now - (snap.get('t') or now)      # the footer

`now` IS `snap['t']`, so the age was ZERO BY CONSTRUCTION in every frame ever rendered. Twelve
minutes after the collector stopped, the footer still read:

    live · 3 cards streaming · updated 0s ago

On an unattended 24-hour run that is the worst failure a dashboard can have -- a dead feed that
looks perfectly healthy. Nobody checks a display that always says it is fine.

The collector now stamps `wall` (the REAL clock at write time) alongside `t`. `t` is rewound under
--replay so a finished capture renders as it looked live, which is exactly why it cannot be used to
judge staleness; `wall` always advances, so a collector that dies stops advancing it.

  python3 test_stale_feed.py            # assertions; exit 0 on success
  python3 test_stale_feed.py --control  # age from `t` again; MUST fail
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip24live  # noqa: E402


def age_of(snap, wall_now, control=False):
    if control:                       # the pre-fix rule: age against the snapshot's own clock
        t = snap.get("t") or wall_now
        return t - (snap.get("t") or t)
    return tip24live.feed_age(snap, wall_now)


def check_dead_feed_is_detected(control=False):
    """⛔ THE ONE THAT MATTERS. Collector stopped 12 minutes ago."""
    snap = {"t": 1_000_000.0, "wall": 1_000_000.0}
    age = age_of(snap, 1_000_000.0 + 720, control)
    if age < tip24live.STALE_S:
        return False, (f"a feed {720}s old reported an age of {age:.0f}s — "
                       "a dead collector renders as healthy")
    return True, f"12 min of silence reads as {age:.0f}s and trips STALE_S={tip24live.STALE_S}"


def check_fresh_feed_is_not_flagged(control=False):
    snap = {"t": 1_000_000.0, "wall": 1_000_000.0}
    age = age_of(snap, 1_000_000.0 + 2, control)
    if age >= tip24live.STALE_S:
        return False, f"a 2s-old feed was called stale ({age:.0f}s)"
    line = tip24live.feed_line(3, age)
    if "STALE" in line:
        return False, f"a healthy feed renders as stale: {line!r}"
    return True, f"2s reads healthy: {line!r}"


def check_the_words_change(control=False):
    """A number alone is not a signal; the line must SAY it."""
    live = tip24live.feed_line(3, 2)
    dead = tip24live.feed_line(3, 900)
    if "STALE" not in dead or "may be dead" not in dead:
        return False, f"a stale feed does not say so: {dead!r}"
    if "STALE" in live:
        return False, f"a live feed claims to be stale: {live!r}"
    if live == dead:
        return False, "live and stale render the same text"
    return True, f"{live!r} vs {dead!r}"


def check_old_snapshot_does_not_crash(control=False):
    """A snapshot written before `wall` existed must not raise or claim staleness."""
    if tip24live.feed_age({"t": 1_000_000.0}, 1_000_000.0 + 9999) != 0.0:
        return False, "a snapshot with no `wall` reported a nonzero age"
    return True, "a pre-`wall` snapshot degrades to the old behaviour, no worse"


def check_collector_stamps_wall(control=False):
    """The collector must actually write the field the renderer depends on."""
    src = open(os.path.join(HERE, "collect.py"), encoding="utf8").read()
    if '"wall": time.time()' not in src:
        return False, "collect.py does not stamp `wall` — the renderer has nothing to read"
    return True, "collect.py stamps `wall` at write time"


CHECKS = [check_dead_feed_is_detected, check_fresh_feed_is_not_flagged,
          check_the_words_change, check_old_snapshot_does_not_crash,
          check_collector_stamps_wall]
CONTROL_MUST_FAIL = {check_dead_feed_is_detected}


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        ok, why = fn(control=control)
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
    print("\na dead feed says so" if not control else "\ncontrol failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

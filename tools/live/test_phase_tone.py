#!/usr/bin/env python3
"""A failed run must not look like a healthy one.

⛔ WHAT THIS CAUGHT. The phase banner was hardcoded to ACCENT, so

    FAILED on block 741000      (235, 140, 25)
    PROVING block 741000        (235, 140, 25)
    VERIFIED block 741000 ...   (235, 140, 25)

rendered PIXEL-IDENTICAL. A status readout with one colour cannot report status. On an unattended
24-hour run the banner gets one glance, and that glance could not tell a failure from progress.

⚠ Trouble is checked BEFORE success: a phase line can contain both words ("FAILED on block N after
verifying 3"), and it must read as a failure.

  python3 test_phase_tone.py            # assertions; exit 0 on success
  python3 test_phase_tone.py --control  # one colour for every phase; MUST fail
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip24live as T  # noqa: E402


def tone(txt, control=False):
    return T.ACCENT if control else T.phase_tone(txt)


def check_failure_differs_from_progress(control=False):
    """⛔ THE ONE THAT MATTERS."""
    if tone("FAILED on block 741000", control) == tone("PROVING block 741000 on 3 cards", control):
        return False, "FAILED renders in the same colour as PROVING — a failure looks like progress"
    return True, "FAILED != PROVING"


def check_success_differs_from_progress(control=False):
    if tone("VERIFIED block 741000 in 267.9s", control) == tone("PROVING block 741000", control):
        return False, "VERIFIED renders in the same colour as PROVING"
    return True, "VERIFIED != PROVING"


def check_trouble_wins_over_success(control=False):
    """A line with both words is a failure."""
    mixed = tone("FAILED on block 741000 after verifying 3 others", control)
    if mixed != tone("FAILED on block 1", control):
        return False, "a line containing both 'FAILED' and 'verified' did not read as trouble"
    return True, "trouble is matched first"


def check_real_phase_strings(control=False):
    """The strings tip_smoke actually writes must land in the right bucket."""
    cases = [
        ("PREPARING · renting 3 cards (+3 spare)", T.ACCENT),
        ("PREPARING · staging the block onto 3 cards", T.ACCENT),
        ("PROVING block 741000 on 3 cards", T.ACCENT),
        ("SESSION · 0.2 h on 3 cards", T.ACCENT),
        ("VERIFIED block 741000 in 267.9s on 3 cards", T.OK),
        ("SUBMITTED block 741000 as GHOST", T.OK),
        ("FAILED on block 741000", T.RED),
        ("no capacity for hz-smoke-1", T.RED),
        ("refusing to start the aggregate: loopback bind", T.RED),
        ("stopping: 3 blocks failed in a row", T.RED),
    ]
    for txt, want in cases:
        got = T.phase_tone(txt)
        if got != want:
            return False, f"{txt!r} -> {got}, expected {want}"
    return True, f"all {len(cases)} real phase strings map correctly"


def check_empty_is_safe(control=False):
    for v in (None, "", "   "):
        if T.phase_tone(v) != T.ACCENT:
            return False, f"{v!r} did not fall back to the neutral tone"
    return True, "None/empty fall back to in-progress"


CHECKS = [check_failure_differs_from_progress, check_success_differs_from_progress,
          check_trouble_wins_over_success, check_real_phase_strings, check_empty_is_safe]
CONTROL_MUST_FAIL = {check_failure_differs_from_progress, check_success_differs_from_progress}


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
    print("\na failure does not look like progress" if not control else "\ncontrol failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

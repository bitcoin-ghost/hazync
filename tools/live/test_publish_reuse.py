#!/usr/bin/env python3
"""The publish loop must reuse its ssh connection, or it cannot keep up with itself.

⛔ MEASURED against the live web box, 2026-09-22, publishing real 108 KB frames:

    fresh connection per rsync   1.460 s per tick
    one reused connection        0.645 s per tick

Each tick makes TWO rsync calls. A cold handshake to that box costs 0.48-0.71 s, so two of them is
~1.0 s of setup inside a `sleep 1` loop -- before a single byte of the frame moves. The public page
would fall to roughly one update every 2.5 s, and a 24-hour run would make ~172,800 handshakes for
no benefit.

⚠ THIS IS A SHAPE TEST. It asserts the options are present and the socket is torn down; it cannot
measure latency without a network, and the numbers above came from timing the real thing.

  python3 test_publish_reuse.py            # assertions; exit 0 on success
  python3 test_publish_reuse.py --control  # options stripped; MUST fail
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "publish.sh"), encoding="utf8").read()


def body(control=False):
    if not control:
        return SRC
    # the control removes the reuse options, as they were before the fix
    out = re.sub(r"\s*-o ControlMaster=auto.*?ControlPersist=60", "", SRC, flags=re.S)
    return out.replace("-O exit", "-O nope")


def check_reuse_options(control=False):
    s = body(control)
    for opt in ("ControlMaster=auto", "ControlPath=", "ControlPersist="):
        if opt not in s:
            return False, f"{opt} missing — every rsync opens a fresh ssh (~0.5 s each, twice a tick)"
    return True, "ControlMaster / ControlPath / ControlPersist all set"


def check_socket_is_private(control=False):
    s = body(control)
    if "chmod 700" not in s:
        return False, "the control socket directory is not mode 700"
    if "%r@%h:%p" not in s:
        return False, "the socket path is not per-user/host/port — two publishers could collide"
    return True, "socket dir is 700 and the path is per-destination"


def check_master_is_closed(control=False):
    s = body(control)
    if "-O exit" not in s:
        return False, ("the shared connection is never closed — a ControlPersist socket outliving "
                       "the script is a live authenticated channel to the web box")
    if "trap" not in s:
        return False, "no trap: a killed publisher would leave the socket behind"
    return True, "the master is closed on EXIT/INT/TERM"


def check_still_only_what_changed(control=False):
    """The pre-existing mtime guard must survive the change."""
    if "PUBLISH ONLY WHAT CHANGED" not in SRC or 'stat -c %Y' not in SRC:
        return False, "the mtime guard was lost — identical frames would be re-uploaded"
    return True, "the mtime guard is intact"


def check_sleeps_the_remainder(control=False):
    """⛔ `sleep 1` AFTER the work makes the period `work + 1s`, not 1s.

    Measured: 1.65 s per update with connection reuse, 2.46 s without. The renderer writes a frame
    every second, so roughly every other frame was skipped and the page ran up to 1.65 s behind
    what had already been drawn.
    """
    s = body(control)
    if control:
        # the control restores the pre-fix loop: a flat sleep after the work
        s = re.sub(r'rest=\$\(awk.*?sleep "\$rest"', "sleep 1", s, flags=re.S)
    if re.search(r"^\s*sleep 1\s*$", s, re.M):
        return False, "a flat `sleep 1` remains — the period is work + 1s, not 1s"
    if 'sleep "$rest"' not in s:
        return False, "the loop does not sleep a computed remainder"
    if "d>0?d:0" not in s:
        return False, "the remainder is not clamped — a negative sleep would error or spin"
    return True, "sleeps the remainder, clamped at zero when a tick overruns"


CHECKS = [check_reuse_options, check_socket_is_private,
          check_master_is_closed, check_still_only_what_changed,
          check_sleeps_the_remainder]
CONTROL_MUST_FAIL = {check_reuse_options, check_master_is_closed,
                     check_sleeps_the_remainder}


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
    print("\nthe publish loop reuses its connection" if not control
          else "\ncontrols failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

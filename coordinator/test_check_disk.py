#!/usr/bin/env python3
"""Tests for hazync-check-disk.sh (hazync#397).

WHY THIS EXISTS. Nothing on the coordinator watched free space until this check. It was added because
the bridge was allowed to become tip-following, which writes ~9 GB/day, and the failure it guards
against is the quiet kind: the disk fills over weeks and the first symptom is the coordinator, bitcoind
and the proof store failing together.

A disk check has one dangerous way to be wrong, and it is the SAFE-LOOKING direction: reporting "ok"
when it cannot actually see the filesystem. Then it never fires, and a check that never fires is
indistinguishable from a healthy box until the day it is not. So the cases below pin BOTH directions --
low space must exit 1, and an unreadable path must exit 2 rather than a cheerful 0.

No real filesystem: `df` is stubbed on PATH to report chosen values.

  python3 test_check_disk.py            # assertions; exit 0 on success
  python3 test_check_disk.py --control  # the floor comparison is removed; MUST fail
"""
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deploy", "hazync-check-disk.sh")

fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)

work = tempfile.mkdtemp(prefix="ckdisk_")
script = os.path.join(work, "check.sh")
src = open(SCRIPT).read()

GUARD = 'if [ "$avail" -lt "$FLOOR_GB" ]; then'
if GUARD not in src:
    print(f"CANNOT TEST: the floor comparison is not in {SCRIPT}.")
    print("These tests are pinned to it; if it was rewritten, update them deliberately.")
    sys.exit(2)
if CONTROL:
    # Removes ONLY the floor comparison, so exactly the low-space cases should trip.
    src = src.replace(GUARD, 'if false; then')
open(script, "w").write(src)
os.chmod(script, 0o755)

binv = os.path.join(work, "bin")
os.makedirs(binv)
MAP = os.path.join(work, "avail.map")
with open(os.path.join(binv, "df"), "w") as f:
    f.write(
        '#!/bin/sh\n'
        'for a in "$@"; do p="$a"; done\n'          # last argument is the path
        'v=$(grep "^$p:" "$AVAIL_MAP" 2>/dev/null | cut -d: -f2)\n'
        '[ -n "$v" ] || exit 1\n'                   # no entry = df failing for that path
        'printf "Avail\\n%sG\\n" "$v"\n')
os.chmod(os.path.join(binv, "df"), 0o755)

def run(avail, paths, floor=500):
    """avail: {path: gigabytes or None for 'df fails'}; returns (exit code, stdout)."""
    with open(MAP, "w") as f:
        for p, v in avail.items():
            if v is not None:
                f.write(f"{p}:{v}\n")
    env = dict(os.environ)
    env.update({"PATH": binv + os.pathsep + env["PATH"], "AVAIL_MAP": MAP,
                "HAZYNC_DISK_PATHS": " ".join(paths), "HAZYNC_DISK_FLOOR_GB": str(floor)})
    p = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout

print(f"  (stubbed df{'; CONTROL: floor comparison removed' if CONTROL else ''})")

# 1. Plenty of room on every path.
rc, o = run({"/srv/bulk": 3900, "/": 1800}, ["/srv/bulk", "/"])
check(rc == 0 and "above the" in o, f"every path above the floor exits 0 (rc={rc})")

# 2. Below the floor. ⛔ THE CENTRAL CASE — this is the whole point of the check.
rc, o = run({"/srv/bulk": 120}, ["/srv/bulk"])
check(rc == 1 and "LOW" in o and "120G" in o and "500G" in o,
      f"below the floor exits 1 and names the path and both numbers (rc={rc})")

# 3. df says nothing: that is "could not check", NOT "fine".
rc, o = run({"/srv/bulk": None}, ["/srv/bulk"])
check(rc == 2 and "COULD NOT CHECK" in o and "NOTHING about free space" in o,
      f"an unreadable path exits 2, not a cheerful 0 (rc={rc})")

# 4. One low among several — the healthy one must not mask it.
rc, o = run({"/srv/bulk": 3900, "/": 40}, ["/srv/bulk", "/"])
check(rc == 1 and "LOW: /" in o, f"one low path among healthy ones still exits 1 (rc={rc})")

# 5. Low AND unreadable: the definite finding outranks the uncertain one.
rc, o = run({"/srv/bulk": 10, "/": None}, ["/srv/bulk", "/"])
check(rc == 1 and "could not be read" in o,
      f"low beats unreadable: exit 1, and it says a path was unreadable too (rc={rc})")

# 6. Exactly at the floor is NOT low (the comparison is -lt).
rc, o = run({"/srv/bulk": 500}, ["/srv/bulk"])
check(rc == 0, f"exactly at the floor is not low (rc={rc})")

EXPECTED_CONTROL_FAILURES = {
    "below the floor exits 1 and names the path and both numbers (rc=0)",
    "one low path among healthy ones still exits 1 (rc=0)",
    "low beats unreadable: exit 1, and it says a path was unreadable too (rc=2)",
}

print()
if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — the floor comparison was removed and exactly the {len(got)} assertion(s) "
              "that depend on it failed:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the floor comparison did not produce the expected failures.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES):
        print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

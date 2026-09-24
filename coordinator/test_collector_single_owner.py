#!/usr/bin/env python3
"""One collector owns the snapshot, and it cannot be killed by another one (hazync#510).

⛔ WHY THIS EXISTS — measured 2026-09-24, and it cost a whole session's visibility.

A collector left running from the previous night's flagship run was still writing
`tools/live/snapshot.json` eight hours later. When the next run started its own collector, the new
one died 72 seconds in:

    [03:03:19] 29/29 up · 0 blocks · $21.46/hr · spent $2.0651 · chain ok
    Traceback (most recent call last):
      File ".../collect.py", line 708, in main
        os.replace(tmp, a.out)
    FileNotFoundError: [Errno 2] .../snapshot.json.tmp -> .../snapshot.json

`tmp = a.out + ".tmp"` is a FIXED sibling name. It is atomic against the RENDERER -- which is what
it was written for -- and lethal against another COLLECTOR: both write the same temp path, and
whichever renames first deletes the other's file out from under it.

⛔⛔ AND THE OUTAGE WAS INVISIBLE. The page was not blank and not frozen; it updated once a second
for the whole session, with an eight-hour-old run's blocks. "Is it publishing?" was the wrong
question, and it had a reassuring answer.

Two independent defects, so two independent guards:

  1. the temp name is per-process, so two collectors cannot destroy each other's file
  2. a collector REFUSES to start when a live one already owns that output, so they cannot both
     write it and leave the renderer alternating between two runs

  python3 test_collector_single_owner.py            # must PASS
  python3 test_collector_single_owner.py --control  # the fixed temp name is restored; MUST show
"""
import json
import os
import subprocess
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(os.path.dirname(HERE), "tools", "live")

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── 1. two writers, one fixed temp name: the loser dies on the rename ───────────────────────────
# ⛔ THE REAL RACE, RUN. Not a description of it -- two processes, the same output path, the exact
# write-then-rename that collect.py does, repeated until one of them loses.
RACER = r'''
import json, os, sys, time
out, mode, deadline = sys.argv[1], sys.argv[2], time.time() + float(sys.argv[3])
tmp = out + ".tmp" if mode == "fixed" else f"{out}.{os.getpid()}.tmp"
n = 0
while time.time() < deadline:
    try:
        with open(tmp, "w") as fh:
            json.dump({"pid": os.getpid(), "n": n}, fh)
        os.replace(tmp, out)
    except FileNotFoundError as exc:
        print(f"DIED {exc}")
        raise SystemExit(3)
    n += 1
print(f"SURVIVED {n}")
'''

with tempfile.TemporaryDirectory() as d:
    out = os.path.join(d, "snapshot.json")
    mode = "fixed" if CONTROL else "perpid"
    procs = [subprocess.Popen([sys.executable, "-c", RACER, out, mode, "3"],
                              stdout=subprocess.PIPE, text=True) for _ in range(2)]
    outs = [p.communicate()[0].strip() for p in procs]
    died = [o for o in outs if o.startswith("DIED")]
    check(not died,
          f"two collectors write the same snapshot and BOTH survive "
          f"({'; '.join(o.split(chr(10))[0][:60] for o in outs)})")
    # whatever happened, the file must still be readable JSON for the renderer
    try:
        json.load(open(out))
        ok_json = True
    except Exception:
        ok_json = False
    check(ok_json, "and the renderer never sees a half-written snapshot")

# ── 2. ⛔ BUT NOT DYING IS NOT THE POINT. They must not both own it ─────────────────────────────
# A per-pid temp name makes the crash go away and leaves the worse bug: two collectors alternating
# two runs' data into one file, which is exactly what looks healthy from outside.
with tempfile.TemporaryDirectory() as d:
    out = os.path.join(d, "snapshot.json")
    rundir = os.path.join(d, "run")
    os.makedirs(rundir)
    first = subprocess.Popen([sys.executable, os.path.join(LIVE, "collect.py"),
                              "--rundir", rundir, "--out", out, "--interval", "1"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    second = subprocess.run([sys.executable, os.path.join(LIVE, "collect.py"),
                             "--rundir", rundir, "--out", out, "--interval", "1"],
                            capture_output=True, text=True, timeout=60)
    blocked = second.returncode != 0 and "already collecting" in (second.stderr + second.stdout)
    check(blocked,
          f"a second collector on the same snapshot REFUSES to start (rc={second.returncode}, "
          f"{(second.stderr or second.stdout).strip().splitlines()[0][:70] if (second.stderr or second.stdout).strip() else 'no message'})")

    stolen = subprocess.run([sys.executable, os.path.join(LIVE, "collect.py"),
                             "--rundir", rundir, "--out", out, "--interval", "1", "--once",
                             "--steal"], capture_output=True, text=True, timeout=60)
    check(stolen.returncode == 0, "and --steal is there for when the takeover is deliberate")
    first.kill()
    first.wait()

    # ⚠ A collector killed with -9 leaves its lock behind. The next run must not be wedged by it.
    dead_pid_lock = os.path.abspath(out) + ".owner"
    with open(dead_pid_lock, "w") as fh:
        fh.write("999999")                      # a pid that cannot be alive
    after = subprocess.run([sys.executable, os.path.join(LIVE, "collect.py"),
                            "--rundir", rundir, "--out", out, "--once"],
                           capture_output=True, text=True, timeout=60)
    check(after.returncode == 0,
          f"a lock left by a -9'd collector does not wedge the next run (rc={after.returncode})")

EXPECTED_CONTROL = {"two collectors write the same snapshot and BOTH survive"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the fixed .tmp name restored, one collector kills the other:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected only the shared-temp assertion to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("one collector owns the snapshot, and a stale one cannot quietly keep the page")

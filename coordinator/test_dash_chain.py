#!/usr/bin/env python3
"""The run starts everything that turns telemetry into a KEPT frame — including the archiver.

⛔ WHAT THIS EXISTS FOR. `publish.sh` rewrites a single `frame.png` and pushes it; the previous frame
is gone. That is right for a live page and useless afterwards — the most legible artifact a tip run
can produce is the hour compressed into a few seconds, and after the run there is nothing left to
compress.

`frame-archive.sh` has existed for this the whole time and was in nobody's start-up path. Measured
2026-09-28: tip hour 2 ran from 12:45, nothing archived a single frame, and the archiver was started
BY HAND at 13:34 — 49 minutes in. The frames from the run's entire gate phase, its first tip block
and the growth from 17 to 24 cards do not exist and cannot be recovered.

⚠ The run that needs the archiver least is the one somebody is watching. Started from the chain, no
one has to remember.

    python3 test_dash_chain.py             # the archiver is in the chain and points at the rundir
    python3 test_dash_chain.py --control   # archiver removed — the frames must be unrecoverable
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_smoke  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


A = types.SimpleNamespace(live_rig="/rig", rundir="/run", publish_dest="box:")


def chain(a):
    out = tip_smoke.dash_chain(a)
    if CONTROL:
        out = [(n, v) for n, v in out if n != "archiver"]      # the shipped behaviour
    return out


names = [n for n, _ in chain(A)]
check("collector" in names and "renderer" in names, f"the feed and the frame are started ({names})")

if CONTROL:
    check("archiver" not in names,
          "control reproduces it: nothing archives frames, so only the LAST one survives the run")
else:
    check("archiver" in names, f"the archiver is started with the run ({names})")
    argv = dict(chain(A))["archiver"]
    # ⛔ INTO THE RUNDIR, NOT THE RIG. The frames belong to the run; the rig is reused by the next
    # one, and a shared directory would interleave two runs' timelapses.
    check(any(p == "/run/frames" for p in argv),
          f"and writes into the RUNDIR, not the rig: {argv}")
    check(any(p == "/rig/frame.png" for p in argv), "reading the frame the renderer writes")
    check(argv[0].endswith("bash") and argv[1].endswith("frame-archive.sh"),
          f"via frame-archive.sh, which dedupes by content: {argv[1]}")

# ⚠ The publisher is the only OPTIONAL member — a run with no --publish-dest still keeps its frames.
B = types.SimpleNamespace(live_rig="/rig", rundir="/run", publish_dest=None)
names_b = [n for n, _ in chain(B)]
check("publisher" not in names_b, "no --publish-dest means no publisher")
if not CONTROL:
    check("archiver" in names_b,
          "but the frames are still archived — a private run is still worth a timelapse")

# ⛔ Every member is torn down by pid, so each must be a real spawnable argv.
for n, argv in chain(A):
    check(isinstance(argv, list) and len(argv) >= 2 and argv[0].startswith("/"),
          f"{n}: argv is absolute and spawnable ({argv[0]})")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

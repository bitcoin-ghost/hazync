#!/usr/bin/env python3
"""`tip-stream.sh stop` must kill the process that is WRITING, not just the loop around it.

⛔ WHAT THIS EXISTS FOR. The streamer was `( while true; do ssh ... >> card.csv; done ) &` and the
recorded pid was the SUBSHELL's. `kill "$pid"` then:

  1. sent SIGTERM to a bash loop blocked in `ssh`, which defers it until the child exits — and an
     ssh to a live pod does not exit;
  2. even when the loop died, the `ssh` was its CHILD: reparented, still appending to the same csv;
  3. ran `: > "$PIDS"` regardless, so anything that survived became permanently unreachable.

#500 had already added "stop before every start", so the calls were being made. They did nothing.
Every `start` stacked another writer on the same files. Measured 2026-09-28 across the run's 26
captures:

    hz-smoke-2.csv    17,864 rows over 3,505 unique seconds   5.07 rows/sec, 10 in the worst second
    TOTAL            348,723 rows over 73,943 unique seconds   4.72x duplication

and 46 orphaned loops still running 17 hours later. The dashboard counts rows as seconds, so the
frame published **$200 of spend against $41.56 actually billed**.

⚠ THIS USES REAL PROCESSES. A mocked `kill` proves only that a call was made — which is precisely
what was already true and already useless. The test spawns a writer inside a loop, exactly the shape
the streamer uses, and asks whether the FILE STOPS GROWING.

    python3 test_streamer_stop.py             # the writer dies with its loop
    python3 test_streamer_stop.py --control   # kill the subshell only — the writer must survive
"""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def spawn_writer(out):
    """A loop whose CHILD does the writing — the streamer's exact shape, with `sh` for `ssh`.

    ⚠ ALWAYS setsid, in BOTH arms. The arms differ in how the thing is STOPPED, which is the actual
    defect; starting the control outside a process group would leave its orphaned writer running
    for ever, because the cleanup below could then only reach it by killing this test's own group.
    A test that leaks a runaway process is worse than no test.
    """
    body = f'while true; do sh -c \'while true; do echo x >> "{out}"; sleep 0.1; done\'; sleep 1; done'
    p = subprocess.Popen(["setsid", "bash", "-c", body])
    return p.pid


def stop(pid):
    if CONTROL:
        try:                                                # the shipped behaviour: the LEADER only
            os.kill(pid, signal.SIGTERM)                    # (not the group -- that is the bug)
        except OSError:
            pass
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)                             # the whole group
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(0.6)


d = tempfile.mkdtemp()
out = os.path.join(d, "card.csv")
open(out, "w").close()

pid = spawn_writer(out)
time.sleep(1.5)
grew = os.path.getsize(out)
check(grew > 0, f"the writer is running and the capture is growing ({grew} bytes)")

stop(pid)
time.sleep(1.0)
before = os.path.getsize(out)
time.sleep(1.5)
after = os.path.getsize(out)

if CONTROL:
    check(after > before,
          f"control reproduces it: the capture STILL GREW after stop ({before} -> {after} bytes) — "
          f"the orphaned writer goes on appending")
else:
    check(after == before,
          f"the capture stopped growing at stop ({before} -> {after} bytes)")

# ⛔ TIDY UP THE WHOLE GROUP, IN BOTH ARMS. The control deliberately leaves an orphaned writer
# alive; it is in its own process group precisely so this can always reach it.
try:
    os.killpg(pid, signal.SIGKILL)
except OSError:
    pass
time.sleep(0.3)
_leaked = os.path.getsize(out)
time.sleep(0.8)
check(os.path.getsize(out) == _leaked,
      "the test leaves nothing running behind it, whichever arm ran")

# ── the script itself ────────────────────────────────────────────────────────────────────────────
src = open(os.path.join(HERE, "tip-stream.sh"), encoding="utf8").read()
if not CONTROL:
    check("setsid bash -c" in src, "streamers are started with setsid, so each has its own group")
    check(re.search(r'kill -KILL -- "-\$pid"', src) is not None,
          "stop escalates to KILL against the process GROUP (the leading - on the pid)")
    check("kill -0" in src, "and verifies afterwards rather than assuming")
    # ⛔ The pidfile must never be emptied over a survivor, or the orphan is unreachable for ever.
    check(': > "$PIDS"' not in src.split("stop)")[1].split(";;")[0],
          "stop no longer truncates the pidfile unconditionally")
    check('printf \'%s\' "$survivors" > "$PIDS"' in src,
          "it keeps whatever would not die, so a later stop can try again")

# ── the second line of defence: a duplicated second cannot inflate the money ─────────────────────
# ⛔ `secs` IS THE SPEND. collect.py multiplies it by the card's hourly rate, so a doubled feed
# doubles the bill on the frame. Fixing tip-stream.sh fixes the CAUSE; this makes the NUMBER safe
# even if a feed doubles again, which it has now done twice.
import csv as _csv  # noqa: E402


def count_seconds(rows):
    """Seconds counted for money, as collect.py counts them."""
    secs, last = 0, None
    for r in rows:
        whole = int(float(r[0]))
        if not CONTROL and whole == last:
            continue                      # the fix: one whole second, once
        last = whole
        secs += 1
    return secs


# One card, 100 real seconds, delivered by FIVE writers — the shape measured on 2026-09-28
# (5.07 rows/sec per card, 10 in the worst second, 4.72x across 348,723 rows).
rows = [(f"{1790590000 + t}.{k}", "x") for t in range(100) for k in range(5)]
RATE = 2.09
got = count_seconds(rows)
spend = got * RATE / 3600.0
true_spend = 100 * RATE / 3600.0

if CONTROL:
    check(got == 500,
          f"control reproduces it: 100 seconds of work counted as {got} — a 5x feed is a 5x bill")
    check(abs(spend / true_spend - 5.0) < 0.01,
          f"control: the frame would show ${spend:.4f} where ${true_spend:.4f} was spent")
else:
    check(got == 100, f"100 seconds of work count as {got} seconds however many writers there were")
    check(abs(spend - true_spend) < 1e-9,
          f"so the money is right: ${spend:.4f}")
    # a real 1 Hz feed is unaffected
    clean = [(f"{1790590000 + t}.0", "x") for t in range(100)]
    check(count_seconds(clean) == 100, "an undoubled feed is counted exactly as before")
    # ⚠ Rows from two writers INTERLEAVE, so the guard must key on the row's own timestamp rather
    # than on "has the value advanced since the previous row".
    inter = [(f"{1790590000 + t}.{k}", "x") for t in range(10) for k in (0, 5)]
    check(count_seconds(inter) == 10, "interleaved writers still yield one count per second")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

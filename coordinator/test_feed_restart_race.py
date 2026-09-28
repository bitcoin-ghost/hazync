#!/usr/bin/env python3
"""Stopping the old streamers must FINISH before the new ones start (hazync#564).

⛔ WHAT THIS EXISTS FOR. `DashboardFeed.run` is `os.spawnve(os.P_NOWAIT, ...)`, so `start()` spawned
`tip-stream.sh stop` and `tip-stream.sh start` and awaited neither. They raced.

That was harmless for as long as the stop did not work: it TERMed a bash subshell blocked in `ssh`,
which defers the signal, and the `ssh` doing the writing was a child that survived anyway. The old
streamers lived through every restart — which is precisely what stacked 4.72x duplicate rows.

#562 made the stop effective. The race then inverted: stop began winning and killing the generation
that `start` had just launched. Measured live on 2026-09-28 at the 18->19 growth event:

    [17:54:45] fleet grew by 1 to 19 cards (hz-grow-4)
    streaming 19 pods -> /root/tiphour3b-2026-09-28/stream      <- start ran
               0 streamer processes, 0 pidfile entries, captures stale 97s

The feed was dead for six minutes; the frame showed `0/19 up` and the spend froze at $9.13 while
nineteen cards went on billing. With `--grow-to` that is one outage per growth event.

⚠ A CALL-ORDER ASSERTION WOULD HAVE PASSED THE WHOLE TIME. `stop()` was always called before
`start()` in the source — #500 added it, and it was doing nothing. What matters is whether the stop
has TAKEN EFFECT when start begins, so this test models both as asynchronous and asks what is
running at the end.

    python3 test_feed_restart_race.py             # the stop completes first; the new feed survives
    python3 test_feed_restart_race.py --control   # fire-and-forget stop — it must kill the new feed
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_dashboard  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


class FakeRig:
    """A streamer whose stop takes real time to land, as the shell script does.

    `generation` stands in for the set of processes currently streaming: `start` bumps it and writes
    the pidfile; `stop` clears whatever generation was current WHEN IT EVENTUALLY RUNS.
    """

    def __init__(self, rundir, stop_delay=0.6):
        self.rundir = rundir
        self.pids = os.path.join(rundir, "stream.pids")
        self.generation = 0
        self.stop_delay = stop_delay
        self.lock = threading.Lock()
        self.threads = []

    def run(self, argv, env):
        action = argv[-1] if isinstance(argv, (list, tuple)) else str(argv)
        if "stop" in action:
            t = threading.Thread(target=self._stop_later)
        else:
            t = threading.Thread(target=self._start_now)
        # ⚠ DAEMON. The bounded-timeout case deliberately leaves a stop that never lands, and a
        # non-daemon thread sleeping through it keeps the interpreter alive at exit -- the test then
        # prints PASS and hangs until something kills it (exit 124). A test that does not terminate
        # is a test nobody will keep running.
        t.daemon = True
        t.start()
        self.threads.append(t)
        return 12345                      # spawnve returns a pid nobody waits on

    def _stop_later(self):
        time.sleep(self.stop_delay)       # the shell script takes TERM -> sleep -> KILL -> verify
        with self.lock:
            self.generation = 0
            open(self.pids, "w").close()  # emptied: nothing is streaming

    def _start_now(self):
        with self.lock:
            self.generation += 1
            with open(self.pids, "w") as fh:
                for i in range(19):
                    fh.write("%d hz-card-%d\n" % (9000 + i, i))

    def join(self, timeout=10.0):
        for t in self.threads:
            t.join(timeout)


CARDS = [{"cid": "hz-card-%d" % i, "ip": "10.0.0.%d" % (i + 1), "port": 22,
          "price": 2.09, "gpu": "RTX PRO 6000"} for i in range(19)]

d = tempfile.mkdtemp()
os.makedirs(os.path.join(d, "stream"), exist_ok=True)
rig = FakeRig(d)

feed = tip_dashboard.DashboardFeed(d, script="/rig/tip-stream.sh", run=rig.run, key="/k")

# An already-running generation, exactly as a growth event finds things.
rig._start_now()
check(rig.generation == 1, "a generation is streaming before the restart")


def restart():
    """What start() does about the old streamers."""
    if CONTROL:
        feed.stop()                       # fire and forget: the shipped behaviour
    else:
        feed.stop_and_wait(timeout_s=5.0, poll_s=0.05)
    rig._start_now()                      # then the new generation goes up


restart()
rig.join()                                # let every spawned action land

if CONTROL:
    check(rig.generation == 0,
          "control reproduces it: the late stop killed the NEW generation — the feed is dead")
    check(os.path.getsize(rig.pids) == 0,
          "control: the pidfile is empty, so nothing can even be found to restart")
else:
    check(rig.generation == 1,
          f"the new generation is still streaming after the restart (generation={rig.generation})")
    check(os.path.getsize(rig.pids) > 0,
          "and the pidfile records it, so a later stop can reach it")

# ── the wait is bounded, and reports rather than hangs ───────────────────────────────────────────
if not CONTROL:
    d2 = tempfile.mkdtemp()
    stuck = FakeRig(d2, stop_delay=60.0)          # a stop that never lands
    stuck._start_now()
    f2 = tip_dashboard.DashboardFeed(d2, script="/rig/tip-stream.sh", run=stuck.run, key="/k")
    t0 = time.monotonic()
    ok = f2.stop_and_wait(timeout_s=1.0, poll_s=0.05)
    waited = time.monotonic() - t0
    check(ok is False, "a stop that never lands returns False rather than claiming success")
    check(waited < 3.0, f"and it gives up in {waited:.1f}s — a hung stop must not hold up a run")

    # ⚠ No pidfile at all is "nothing was streaming", the normal first call of a run.
    d3 = tempfile.mkdtemp()
    empty = FakeRig(d3)
    f3 = tip_dashboard.DashboardFeed(d3, script="/rig/tip-stream.sh", run=empty.run, key="/k")
    check(f3.stop_and_wait(timeout_s=1.0, poll_s=0.05) is True,
          "no pidfile means nothing was streaming, and that is an immediate success")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

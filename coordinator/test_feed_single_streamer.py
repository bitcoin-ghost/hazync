#!/usr/bin/env python3
"""Starting the feed twice must not leave two streamers on the same files (hazync#500).

⛔ WHY THIS EXISTS. A run calls `Feed.start` more than once: once when the fleet is rented, and
again after the gates pick the final cards ("feed rewritten for N cards"). `start` wrote pods.txt
and launched `tip-stream.sh start` without stopping whatever was already running, so both sets
appended to the same `stream/<card>.csv` for the rest of the run.

Measured on the 968,243 run: the session log shows `streaming 18 pods` and then `streaming 16
pods`, and the coordinator's capture held 5,356 rows across 2,860 s -- 1.87 rows/sec against a
1 Hz design. Nothing on the frame said so, and anything counting rows as seconds inherited the
factor: behind-the-chain reported 4,845s for a block that had been running 2,560s.

  python3 test_feed_single_streamer.py            # must PASS
  python3 test_feed_single_streamer.py --control  # the stop is removed again; MUST FAIL
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_dashboard                                                            # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


CARDS = [{"cid": f"hz-smoke-{i}", "ip": f"10.0.0.{i}", "port": 22000 + i,
          "pod": f"pod{i}", "price": 0.49, "gpu": "NVIDIA_A40"} for i in range(1, 4)]


class RecordingFeed(tip_dashboard.DashboardFeed):
    """The real DashboardFeed, with only the subprocess runner replaced — the logic is untouched."""

    def __init__(self, rundir, calls):
        super().__init__(rundir, script="/nonexistent/tip-stream.sh",
                         run=lambda argv, env: (calls.append(argv[1] if len(argv) > 1 else "?"), 0)[1])
        self.calls = calls

    def stop(self):
        if CONTROL:
            # ⛔ THE CONTROL: start() no longer stops what is already running.
            raise RuntimeError("control: stop suppressed")
        return super().stop()


d = tempfile.mkdtemp(prefix="feed_")
os.makedirs(os.path.join(d, "stream"), exist_ok=True)
calls = []
f = RecordingFeed(d, calls)

f.start(CARDS)
first = list(f.calls)
f.start(CARDS)                      # the "feed rewritten" call the run really makes

starts = f.calls.count("start")
stops = f.calls.count("stop")

check(first.count("start") == 1, f"the first start launches the streamer once (got {first})")
check(stops >= 1,
      f"the SECOND start stops the first streamer before launching (stop calls: {stops})")
check(starts == 2 and stops >= starts - 1,
      f"so each start owns the feed alone — {starts} starts, {stops} stops, never two sets at once")

EXPECTED_CONTROL = {
    "the SECOND start stops the first streamer",
    "so each start owns the feed alone",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in x for x in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — without the stop, a second start left two streamers on the same files:")
        for x in fails:
            print(f"  - {x}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)} failures; got {len(fails)}:")
    for x in fails:
        print(f"  {x}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("one start, one streamer — a rewritten feed replaces the old one instead of joining it")

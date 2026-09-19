#!/usr/bin/env python3
"""The sponsor bot's regeneration queue: one replay at a time, never beside a running bridge (hazync#347).

WHY THIS EXISTS. regen_advice() reports; the queue ACTS. Acting spends coordinator CPU, which the operator
allows only for blocks someone has paid for — held sponsorships — and only one replay at a time.

⛔ THE TWO GUARDS UNDER TEST are the ones whose absence is expensive, not merely untidy:

  1. ONE AT A TIME. A replay is ~10 GB of accumulator on a 62.8 GiB box that also serves the board. Two at
     once is two bridges on one machine — the exact shape that OOM-killed the live bridge 23 times on
     2026-09-18, with systemd reporting `active (running)` throughout.
  2. NOT BESIDE A BRIDGE. The same arithmetic, except the other process is the tip bridge or the backfill
     walk, which is worse: the backfill has NO resume, so killing it at 76% costs a walk from genesis.

Both controls below disable one guard and the exact set of assertions that depend on it must then fail.

⛔ PINNED LITERALS, not values derived from the thing under test — deriving them moves the scenarios with
the control and the control then passes for an unrelated reason (test_regen_advice.py records the same).

⛔ bridge_running() IS TESTED AGAINST A FAKE /proc, never the host's. Reading the real one makes the result
depend on what happens to be running, so the test would be green or red for reasons unrelated to its label.

Usage:
  python3 test_regen_queue.py            # assertions; exit 0 on success
  python3 test_regen_queue.py --control  # both guards disabled; MUST fail
"""

import os
import sqlite3
import sys
import tempfile

CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sponsor_bot  # noqa: E402

if CONTROL:
    sponsor_bot._CONTROL_IGNORE_ONE_AT_A_TIME = True
    sponsor_bot._CONTROL_IGNORE_BRIDGE_BUSY = True

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    sponsor_bot.ensure_tables(c)
    return c


class _Spy:
    """Stands in for sponsor_bot.subprocess. Records every Popen and starts nothing."""

    PIPE = STDOUT = -1

    def __init__(self):
        self.calls = []

    def Popen(self, argv, **kw):                       # noqa: N802 — matches subprocess.Popen
        self.calls.append(argv)
        return _FakeProc()


class _FakeProc:
    """A replay that never finishes, so one spawn does not cascade into the polling path."""

    pid = 31337
    stdout = None

    def poll(self):
        return None


def bot_with(conn):
    """A Bot cheap enough to build for this: _regen touches no pod, no runner and no coordinator.

    api and runner are stored unvalidated; only the three money flags must be positive, and
    pod_price_ceiling must not exceed max_usd_per_hour."""
    b = sponsor_bot.Bot(":memory:", None, None, max_pods=1, max_usd=1.0, max_usd_per_hour=1.0,
                        pod_price_ceiling=1.0, clock=lambda: 1000.0, log=lambda m: None)
    return b, conn


def spawned(bot_conn, bridge):
    """Run ONE _regen tick with a spying subprocess and a fixed bridge_running; return processes started.

    ⛔ Patch the MODULE ATTRIBUTES and restore them. Setting an environment variable instead would change
    nothing (CKPT_ARCHIVE is read at import) and the test would pass against whatever the host happens to
    have — the trap test_regen_advice.py:98 records.
    """
    bot, conn = bot_conn
    spy = _Spy()
    saved_sub, saved_br = sponsor_bot.subprocess, sponsor_bot.bridge_running
    saved_arch = sponsor_bot.CKPT_ARCHIVE
    sponsor_bot.subprocess = spy
    sponsor_bot.bridge_running = lambda *a, **k: list(bridge)
    # ⛔ A FAKE ARCHIVE WITH A REAL SEED, or the guards pass for the wrong reason. With the guards
    # DISABLED the drain runs on to regen_bundles.plan(), which reads this directory. Left pointing at the
    # host's /srv/bulk/hazync/checkpoints it finds nothing on any machine that is not the coordinator,
    # plan()["missing"] is truthy, and _regen takes the REFUSE branch and returns before Popen — so the spy
    # records nothing and both guard assertions pass without the guards doing anything. The control caught
    # exactly that. A rung strictly below every height used here makes the disabled path reach Popen.
    arch = tempfile.TemporaryDirectory()
    open(os.path.join(arch.name, "state_580000.bin"), "wb").close()
    sponsor_bot.CKPT_ARCHIVE = arch.name
    try:
        bot._regen(conn, now=1000.0)                   # never raises by contract; a spawn is recorded by the spy
    finally:
        sponsor_bot.subprocess, sponsor_bot.bridge_running = saved_sub, saved_br
        sponsor_bot.CKPT_ARCHIVE = saved_arch
        arch.cleanup()
    return len(spy.calls)


def fake_proc(entries):
    """A /proc-shaped directory. entries: {pid: argv0}. Returns its path (caller keeps the TemporaryDirectory)."""
    d = tempfile.TemporaryDirectory()
    for pid, argv0 in entries.items():
        os.mkdir(os.path.join(d.name, str(pid)))
        with open(os.path.join(d.name, str(pid), "cmdline"), "wb") as f:
            f.write(argv0.encode() + b"\0bridge\0")
    os.mkdir(os.path.join(d.name, "self"))          # a non-numeric entry must be skipped, not crash
    return d


# ── the table ─────────────────────────────────────────────────────────────────────────────────────────

c = db()
check({"height", "sponsorship_id", "queued_at", "started_at", "finished_at", "pid", "rung", "outcome", "detail"}
      <= {r[1] for r in c.execute("PRAGMA table_info(regen_queue)")},
      "ensure_tables creates regen_queue with the columns the drain needs")

added = sponsor_bot.regen_enqueue(c, [(7, 600000), (7, 600001)], now=100.0)
check(added == 2, "queueing two heights adds two rows")
check(sponsor_bot.regen_enqueue(c, [(7, 600000)], now=101.0) == 0,
      "re-queueing a height already queued adds nothing, so a tick loop cannot fill the table")

# ── one at a time ─────────────────────────────────────────────────────────────────────────────────────

check(sponsor_bot.regen_inflight(c) is None, "nothing is in flight before a replay is started")
check(sponsor_bot.regen_next(c)["height"] == 600000, "the oldest queued height is taken first")

sponsor_bot.regen_start(c, 600000, pid=4242, rung=580000, now=102.0)
row = sponsor_bot.regen_inflight(c)
check(row is not None and row["height"] == 600000 and row["pid"] == 4242,
      "a started replay is in flight, with its pid recorded so it can be reaped")

# 1. ⛔ THE CONTROL CASE. With one replay in flight, the drain must not start another.
#
# ⛔ THIS DRIVES THE REAL Bot._regen AND COUNTS PROCESSES SPAWNED. An earlier version of this test
# recomputed the guard here — `regen_inflight(c) is not None and not _CONTROL_IGNORE_...` — and asserted
# its own arithmetic. That passes whatever _regen does, including spawning a replay unconditionally: green
# for a reason unrelated to its label, which is the failure test_regen_advice.py's own notes warn about.
# The spy below CAN fail, which is the only reason to keep the assertion.
#
# ⛔ ITS OWN DATABASE. Under --control this call really does start a replay, which changes rows. Sharing a
# connection with the later reaping and queue-order cases let that bleed into them, and the control then
# reported failures that had nothing to do with the guard. An EXPECTED_CONTROL_FAILURES list grown to
# absorb collateral stops distinguishing "the guard broke" from "something else moved" — which is the only
# thing the control is for. Each guard case gets a fresh db and heights nothing else uses.
c_one = db()
sponsor_bot.regen_enqueue(c_one, [(7, 610000), (7, 610001)], now=1.0)
sponsor_bot.regen_start(c_one, 610000, pid=4242, rung=580000, now=2.0)
check(spawned(bot_with(c_one), bridge=[]) == 0,
      "with a replay in flight the one-at-a-time guard holds, so a second is not started")

# ── not beside a bridge ───────────────────────────────────────────────────────────────────────────────

d = fake_proc({4242: "/usr/local/bin/hazync-host-bridge"})
check(sponsor_bot.bridge_running(d.name) == [4242],
      "a process whose argv[0] IS the bridge binary is found")
d.cleanup()

d = fake_proc({99: "/usr/bin/python3", 100: "/bin/bash"})
check(sponsor_bot.bridge_running(d.name) == [],
      "processes that are not the bridge are not reported")

# ⛔ The self-match case that has cost hours three times: a process that only MENTIONS the binary.
d.cleanup()
d = fake_proc({77: "/usr/bin/python3 /opt/hazync/coordinator/deploy/regen_bundles.py --bridge hazync-host-bridge"})
check(sponsor_bot.bridge_running(d.name) == [],
      "a process that merely names the bridge in its arguments is NOT counted as a running bridge")
d.cleanup()

# 2. ⛔ THE SECOND CONTROL CASE. Nothing in flight, one height queued, but a bridge is walking.
c3 = db()
sponsor_bot.regen_enqueue(c3, [(9, 800000)], now=1.0)
check(spawned(bot_with(c3), bridge=[4242]) == 0,
      "while a bridge is walking the drain waits rather than starting a second one")
# ⛔ Its own database again, and NOT the one spawned() just ran against. Under --control that call starts
# the replay, so the row stops being queued — a true consequence of disabling the guard, which would show
# up here as a third "unexpected" failure and blunt the control. What this asserts is narrower and always
# true: a bridge-blocked drain leaves the row untouched, so the next tick retries it.
c4 = db()
sponsor_bot.regen_enqueue(c4, [(9, 810000)], now=1.0)
spawned(bot_with(c4), bridge=[4242])
still = sponsor_bot.regen_next(c4)
check(sponsor_bot._CONTROL_IGNORE_BRIDGE_BUSY or (still is not None and still["height"] == 810000),
      "a height held back by a running bridge stays QUEUED, so it is retried rather than lost")

# ── reaping ───────────────────────────────────────────────────────────────────────────────────────────

# The bot died: pid 4242 is gone, so the row must not block the queue for ever.
reaped = sponsor_bot.regen_reap(c, now=200.0, alive=[])
check(reaped == [600000], "a replay left in flight by a stopped bot is reaped")
check(sponsor_bot.regen_inflight(c) is None, "after reaping, nothing is in flight and the queue moves again")
r = c.execute("SELECT outcome, detail FROM regen_queue WHERE height=600000").fetchone()
check(r["outcome"] == "failed" and "stopped while this replay was running" in r["detail"],
      "the reaped row says plainly why it failed, rather than looking like a replay that ran")

# A live pid is NOT reaped.
sponsor_bot.regen_start(c, 600001, pid=555, rung=580000, now=201.0)
check(sponsor_bot.regen_reap(c, now=202.0, alive=[555]) == [],
      "a replay whose bridge is still running is left alone")

# ── exit codes map to outcomes ────────────────────────────────────────────────────────────────────────

# Pinned to regen_bundles.main()'s own docstring: 0 ran, 1 wrong, 2 could not check.
check(sponsor_bot.regen_finish(c, 600001, 0, "installed 1 bundle(s)", 300.0) == "done",
      "exit 0 is done")
c2 = db()
sponsor_bot.regen_enqueue(c2, [(1, 700000), (1, 700001)], now=1.0)
check(sponsor_bot.regen_finish(c2, 700000, 2, "no rung below", 2.0) == "refused",
      "exit 2 is refused, not failed — nothing was replayed")
check(sponsor_bot.regen_finish(c2, 700001, 1, "replay failed", 3.0) == "failed",
      "exit 1 is failed")

print()
EXPECTED_CONTROL_FAILURES = {
    "with a replay in flight the one-at-a-time guard holds, so a second is not started",
    "while a bridge is walking the drain waits rather than starting a second one",
}

if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — both guards were disabled and exactly the {len(got)} assertion(s) that "
              "depend on them failed:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — disabling the guards did not produce the expected failures.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES):
        print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

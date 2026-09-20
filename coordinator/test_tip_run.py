#!/usr/bin/env python3
"""Tests for tip_run.py — one block from launch to receipt (Phase 5 ⑧).

The phase order is `tools/milestone/run_continuous.sh`, the only sequence that has produced a near-tip
block under ten minutes. The gates below are the ones whose absence killed a run:

  phase 0  a pod carrying a stale chunk_N.bin reports DONE in 63 s, its receipt is collected, and the
           aggregate fails — or worse, succeeds against work nobody asked for
  phase 5  a silent scp drop once staged 21 of 22 and seg-serve panicked with no useful message

A fake runner drives the whole sequence instantly, so the loop costs $0 to test instead of $1.39 and ten
minutes.

  python3 test_tip_run.py            # assertions; exit 0 on success
  python3 test_tip_run.py --control  # the phase-0 gate is removed; MUST fail
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_run as tr  # noqa: E402

if CONTROL:
    # THE GATE REMOVED: start whatever the fleet's state. This is the run that collects a stale receipt.
    tr.verify_fleet_empty = lambda runner, cards: {"clean": list(cards), "dirty": [], "silent": [],
                                                   "ok": True}

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeRunner:
    """A fleet that behaves however the test asks. Records what it was told to do."""

    def __init__(self, cards, *, clear="LEFT:0 REDIRS:0", finish_after=1, wedge=(),
                 stage_ok=True, staged_count=None, verify_after=1, agg_alive=True):
        self.cards, self.clear_reply = cards, clear
        self.finish_after, self.wedge = finish_after, set(wedge)
        self.stage_ok, self._staged_count = stage_ok, staged_count
        self.verify_after, self.agg_alive = verify_after, agg_alive
        self.ticks = 0
        self.staged, self.log = set(), []

    def clear_and_check(self, cards):
        self.log.append(("clear", len(cards)))
        if isinstance(self.clear_reply, dict):
            return self.clear_reply
        return {c: self.clear_reply for c in cards}

    def launch_all(self, cards, **kw):
        self.log.append(("launch", len(cards)))

    def arm_auto_attach(self, cards):
        self.log.append(("attach", len(cards)))

    def probe_all(self, cards, reassigned=()):
        self.ticks += 1
        out = {}
        for chunk, card in cards.items():
            if chunk in self.wedge:
                out[card] = "104857:0:12:0"          # idle: no process, no VRAM
            elif self.ticks >= self.finish_after:
                out[card] = "DONE"
            else:
                out[card] = "500:98:22460:1"          # proving
        return out

    def stage_receipt(self, card, chunk):
        if self.stage_ok:
            self.staged.add(chunk)
        return self.stage_ok

    def staged_count(self):
        return self._staged_count if self._staged_count is not None else len(self.staged)

    def kill_provers(self, card):
        self.log.append(("kill", card))
        return True

    def relaunch(self, card, chunk, **kw):
        self.log.append(("relaunch", card, chunk))
        self.wedge.discard(chunk)                     # the restart fixes it
        return True

    def start_aggregate(self, **kw):
        self.log.append(("aggregate",))
        self.agg_ticks = 0

    def aggregate_status(self):
        self.agg_ticks = getattr(self, "agg_ticks", 0) + 1
        return {"alive": self.agg_alive,
                "verified": self.agg_alive and self.agg_ticks >= self.verify_after,
                "digest": "84e6643e" if self.agg_alive else None}


CARDS = {0: "a", 1: "b", 2: "c"}


def fresh(**kw):
    return dict(CARDS), FakeRunner(dict(CARDS), **kw)


# ── 1. the happy path ──────────────────────────────────────────────────────────────────────────────
cards, r = fresh()
clk = Clock()
res = tr.run_block(block="966256", cards=cards, runner=r, now=clk.now, sleep=clk.sleep)
check(res["ok"] and res["cards"] == 3, f"a clean run returns a verified result ({res['ok']})")
check(res["digest"] == "84e6643e", "the digest is carried out of the aggregate")
check(("launch", 3) in r.log and ("attach", 3) in r.log, "all cards are launched and armed to attach")

# ⛔ THE CLOCK STARTS AT LAUNCH, NOT AT CLEARING. Clearing is setup; counting it would flatter every
# figure this produces. Asserted by ORDER -- the clear must be logged before the launch -- and by the
# wall time excluding a deliberately slow clear.
class SlowClearRunner(FakeRunner):
    def __init__(self, cards, clock, **kw):
        super().__init__(cards, **kw)
        self.clock = clock

    def clear_and_check(self, cards):
        self.clock.t += 500.0          # clearing takes 500 s of setup
        return super().clear_and_check(cards)


clk = Clock()
cards = dict(CARDS)
r = SlowClearRunner(dict(CARDS), clk)
res2 = tr.run_block(block="966256", cards=cards, runner=r, now=clk.now, sleep=clk.sleep)
order = [e[0] for e in r.log]
# Membership first: under --control the clear never happens at all, and an .index() on a missing entry
# would raise instead of failing the assertion -- a test that crashes reports nothing.
check("clear" in order and "launch" in order and order.index("clear") < order.index("launch"),
      f"the fleet is cleared BEFORE the clock starts ({order[:3]})")
check(res2["wall_s"] < 500.0,
      f"a 500 s clear is NOT counted in the block's wall time ({res2['wall_s']} s)")

# ── 2. phase 0: a dirty or unreachable pod stops the run ──────────────────────────────────────────
cards, r = fresh(clear="LEFT:3 REDIRS:0")
try:
    tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None)
    check(False, "a pod with leftovers must stop the run")
except tr.RunRefused as e:
    check("not verified empty" in str(e), f"a pod with leftovers stops the run ({str(e)[:60]}…)")

cards, r = fresh(clear={"a": "LEFT:0 REDIRS:0", "b": None, "c": "LEFT:0 REDIRS:0"})
try:
    tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None)
    check(False, "an unreachable pod must stop the run")
except tr.RunRefused as e:
    check("unreachable=['b']" in str(e),
          f"⛔ an UNREACHABLE pod is not a clean pod — it stops the run too ({str(e)[:70]}…)")

# ── 3. phase 5: the silent scp drop ────────────────────────────────────────────────────────────────
cards, r = fresh(staged_count=2)          # 2 of 3 arrived
try:
    tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None)
    check(False, "staging 2 of 3 must refuse the aggregate")
except tr.RunRefused as e:
    check("2/3" in str(e) and "refusing to aggregate" in str(e),
          f"a short stage refuses the aggregate rather than panicking inside it ({str(e)[:60]}…)")

# ── 4. a wedged card is recovered and the run still completes ─────────────────────────────────────
cards, r = fresh(finish_after=2, wedge={1})
clk = Clock()
res = tr.run_block(block="966256", cards=cards, runner=r, now=clk.now, sleep=clk.sleep,
                   stall_s=0.0)
check(res["ok"], "a run with one wedged card still completes")
check(any(e[0] == "kill" for e in r.log), "the wedged card's provers are killed before relaunch")
check(any(e[0] == "relaunch" for e in r.log), "and its chunk is relaunched")
check(res["events"], f"the recovery is reported, not silent ({res['events'][:1]})")

# ── 5. a dead aggregate is not waited on for ever ─────────────────────────────────────────────────
cards, r = fresh(agg_alive=False)
try:
    tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None)
    check(False, "a dead aggregate must raise")
except tr.RunRefused as e:
    check("died before verifying" in str(e), f"a dead aggregate is noticed, not waited on ({str(e)[:50]}…)")

# ── 5b. ⛔ AN AGGREGATOR THAT VANISHES IS NOT "STILL WORKING" ─────────────────────────────────────
# Measured 2026-09-20 on the 23-card run: the pods were terminated while run_block was still polling.
# Every subsequent poll correctly reported `unreachable` -- which is NOT `dead`, because one failed ssh
# must never abort a healthy run -- so the loop kept asking a fleet that no longer existed, printing
# nothing, until an outer `timeout` killed it at 45 minutes. The run reported EXIT=124 and not one word
# about why. `unreachable` is the right verdict for ONE poll and the wrong one to repeat 1200 times.
class VanishingRunner(FakeRunner):
    """Answers normally, then goes unreachable for ever once the aggregate starts."""

    def aggregate_status(self):
        return {"alive": True, "verified": False, "unreachable": True, "joins": None}


cards, r = fresh()
r.__class__ = VanishingRunner
try:
    tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None,
                 unreachable_limit=20, tick_s=3.0)
    check(False, "a vanished aggregator must stop the run")
except tr.RunRefused as e:
    check("unreachable for 20 consecutive polls" in str(e),
          f"a vanished aggregator stops the run instead of polling in silence ({str(e)[:56]}…)")
    check("probably gone" in str(e) and "terminated" in str(e),
          "and the message says what actually happened, so the next reader is not left with EXIT=124")

# ⛔ AND ONE BAD ssh MUST STILL NOT ABORT ANYTHING. The counter has to reset on any answer, or a fleet
# with an occasional slow probe dies at poll 20 of a run that was working perfectly.
class FlakyRunner(FakeRunner):
    def aggregate_status(self):
        self.agg_ticks = getattr(self, "agg_ticks", 0) + 1
        if self.agg_ticks % 3:                        # unreachable 2 polls in every 3
            return {"alive": True, "verified": False, "unreachable": True}
        return {"alive": True, "verified": self.agg_ticks >= 60, "digest": "84e6643e"}


cards, r = fresh()
r.__class__ = FlakyRunner
res = tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None,
                   unreachable_limit=20)
check(res["ok"], "a flaky probe does NOT abort the run — the counter resets on every answer")

# ── 5c. the dashboard feed is marked at the CLOCK, and never fails a run ─────────────────────────
class FakeFeed:
    """Records WHERE in the phase order it was called, not just that it was."""

    def __init__(self, st, runner=None):
        self.st, self.runner = st, runner
        self.marked, self.log_at_mark = None, None

    def mark_t0(self, t):
        self.marked = t
        self.log_at_mark = list(self.runner.log) if self.runner else None

    def staleness(self, now, limit_s=15.0):
        return self.st


cards, r = fresh()
clk = Clock()
feed = FakeFeed({"ok": True, "live": ["a", "b", "c"], "stale": [], "never": []}, runner=r)
res = tr.run_block(block="966256", cards=cards, runner=r, now=clk.now, sleep=clk.sleep, feed=feed)
check(res["ok"], "a run with a healthy feed completes")
check(feed.marked is not None, "t0 is declared to the dashboard")

# ⛔ t0 SITS BETWEEN THE CLEAR AND THE LAUNCH, and the position is what is asserted -- a timestamp
# comparison would pass just as well if t0 were written during the clear. The clear is SETUP: a t0
# written there back-dates the run by the whole of phase 0 and flatters every per-block figure on the
# frame. Marking it after the launch would lose the launch itself from the graph.
phases = [e[0] for e in (feed.log_at_mark or [])]
check(phases == ["clear"],
      f"t0 is marked AFTER the clear and BEFORE the launch -- the log at that moment is {phases}")

# ⛔ A BLANK SCREEN MUST NOT THROW AWAY A WORKING FLEET. The receipt is the product.
cards, r = fresh()
feed = FakeFeed({"ok": False, "live": [], "stale": ["b"], "never": ["c"]})
res = tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now, sleep=lambda s: None,
                   feed=feed)
check(res["ok"], "⛔ a DEAD telemetry feed does not fail the run — the receipt is the product")
check(any("telemetry not live" in e for e in res["events"]),
      f"but it is RECORDED, not swallowed ({[e[:40] for e in res['events']]})")
check(any("CANNOT be reconstructed" in e for e in res["events"]),
      "and the event says it is unrecoverable after the run, which is the actionable part")

# A run with no feed at all behaves exactly as before.
cards, r = fresh()
check(tr.run_block(block="966256", cards=cards, runner=r, now=Clock().now,
                   sleep=lambda s: None)["ok"],
      "feed=None is still a complete run — the dashboard is optional")

# ── 6. an empty fleet is refused rather than dividing by zero ─────────────────────────────────────
try:
    tr.run_block(block="966256", cards={}, runner=FakeRunner({}), now=Clock().now, sleep=lambda s: None)
    check(False, "an empty fleet must be refused")
except tr.RunRefused as e:
    check("no cards" in str(e), "an empty fleet is refused")

# Under --control the gate never raises, so it is the `must` branch of each pair that records the
# failure -- not the branch that inspects the exception message.
EXPECTED_CONTROL_FAILURES = {
    "a pod with leftovers must stop the run",
    "an unreachable pod must stop the run",
}

print()
if CONTROL:
    got = {f.split(" (")[0] for f in fails}
    want = {f.split(" (")[0] for f in EXPECTED_CONTROL_FAILURES}
    if want <= got:
        print(f"CONTROL OK — the phase-0 gate was removed and the {len(want)} assertion(s) that depend "
              "on it failed, as they must:")
        for f in sorted(fails):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the phase-0 gate went undetected.")
    for f in sorted(want - got):
        print(f"  should have failed and did not: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

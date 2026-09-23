#!/usr/bin/env python3
"""Board work fills the gaps, and gives way the instant a tip lands (hazync#367, #506).

Filling idle time with board work is only safe if the tip still comes first. Three things have to
hold, and each one broke something real when it did not:

  1. a board block ABANDONS itself when a tip bundle appears, and tears the aggregate down on the
     way out -- `seg-serve` holds port 9110, and this codebase has already relaunched one on top of
     a healthy one, killing the new process on bind() while the original ran on with its log gone
  2. an abandoned block is NEITHER a success NOR a failure -- counted as a failure, three tip
     arrivals in a row would trip the fleet-fault guard and release a perfectly healthy fleet
  3. "a tip is waiting" means a COMPLETE bundle. The bridge writes `bundle_<h>.json.tmp` into the
     same directory and renames it, and the old filter (`sed 's/[^0-9]//g'`) turned that into a
     height -- so the fleet would abandon real work for a bundle that does not exist yet

  python3 test_board_fill.py            # must PASS
  python3 test_board_fill.py --control  # preemption removed; MUST show as the gap
"""
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_board                                                             # noqa: E402
import tip_run                                                               # noqa: E402
import tip_session                                                           # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── 3 first, because the other two depend on it being right ─────────────────────────────────────
# ⛔ RUN THE REAL PIPELINE. The filter is a shell command executed on the bridge, so a Python fake
# runner would only prove that whatever the fake returns comes back. `highest_bundle_cmd` hands out
# the exact string the driver sends, and here it runs against a real directory.
with tempfile.TemporaryDirectory() as d:
    for name in ("bundle_968315.json", "bundle_968316.json", "state.bin", "state.head"):
        open(os.path.join(d, name), "w").close()
    os.mkdir(os.path.join(d, "undo"))

    def highest(where):
        r = subprocess.run(["bash", "-c", tip_board.highest_bundle_cmd(where)],
                           capture_output=True, text=True)
        return r.stdout.strip()

    check(highest(d) == "968316",
          f"a settled directory reports its highest complete bundle (got {highest(d)!r})")

    # the bridge starts writing the next one
    open(os.path.join(d, "bundle_968317.json.tmp"), "w").close()
    got = highest(d)
    if CONTROL:
        # ⛔ THE OLD FILTER, restored verbatim.
        r = subprocess.run(["bash", "-c", f"ls -U {d} 2>/dev/null | sed 's/[^0-9]//g' "
                                          f"| grep -v '^$' | sort -n | tail -1"],
                           capture_output=True, text=True)
        got = r.stdout.strip()
    check(got == "968316",
          f"⛔ a half-written bundle_968317.json.tmp is NOT a waiting tip (got {got!r})")

    os.rename(os.path.join(d, "bundle_968317.json.tmp"), os.path.join(d, "bundle_968317.json"))
    check(highest(d) == "968317", "and the moment it is renamed, it is")


# ── 1. a board block abandons itself, and takes the aggregate down with it ──────────────────────
class FakeRunner:
    """Enough of FleetRunner to drive run_range. Records what the teardown actually did."""

    def __init__(self, verify_at_tick=None):
        self.verify_at_tick = verify_at_tick
        self.tick = 0
        self.stopped = 0
        self.disarmed = 0
        self.armed = 0

    def start_range_aggregate(self, *, height, bundle_path):
        return True, ""

    def arm_auto_attach(self, cards):
        self.armed += 1

    def stop_auto_attach(self, cards):
        self.disarmed += 1

    def stop_range_aggregate(self):
        self.stopped += 1
        return True, "no hazync-host-cud left"

    def aggregate_status(self):
        self.tick += 1
        done = self.verify_at_tick is not None and self.tick >= self.verify_at_tick
        return {"verified": done, "alive": True, "digest": "deadbeef" if done else None,
                "joins": "joins 4/4" if done else "joins 1/4"}


clock = {"t": 0.0}


def now():
    return clock["t"]


def sleep(s):
    clock["t"] += s


tip_at = {"tick": 3}


def tip_waiting_after(runner):
    def abort():
        if runner.tick >= tip_at["tick"]:
            return "tip block 968317 is waiting and the tip comes first"
        return None
    return abort


runner = FakeRunner(verify_at_tick=100)          # a long board block
aborted, res, err = None, None, None
try:
    res = tip_run.run_range(height=123538, cards=["a", "b"], runner=runner, bundle_path="/tmp/x",
                            now=now, sleep=sleep, on_event=None,
                            abort=(None if CONTROL else tip_waiting_after(runner)))
except tip_run.RunAborted as exc:
    aborted = exc
except tip_run.RunRefused as exc:
    err = exc

check(aborted is not None,
      f"a board block steps aside when a tip lands (got {'RunAborted' if aborted else res or err})")
check(runner.stopped == 1,
      f"⛔ and the aggregate is STOPPED on the way out, not left holding port 9110 "
      f"(stop_range_aggregate called {runner.stopped}x)")
check(runner.disarmed == 1,
      f"and the workers are disarmed, so they stop dialling it ({runner.disarmed}x)")
check(runner.tick <= tip_at["tick"] + 1,
      f"promptly — within a tick of the tip appearing (gave up on tick {runner.tick})")

# ⛔ WORK THAT IS ALREADY DONE IS NOT THROWN AWAY. A receipt six seconds from being collected must
# not be discarded to start the tip six seconds sooner.
clock["t"] = 0.0
r2 = FakeRunner(verify_at_tick=1)
out = tip_run.run_range(height=123539, cards=["a"], runner=r2, bundle_path="/tmp/x",
                        now=now, sleep=sleep, abort=lambda: "a tip is waiting")
check(out.get("ok") and r2.stopped == 0,
      f"a block that has ALREADY verified is kept, not abandoned (ok={out.get('ok')}, "
      f"stopped={r2.stopped}x)")

# ⚠ A BROKEN ABORT MUST NOT KILL A HEALTHY RUN. The predicate reaches over ssh; a blip is not a
# reason to throw away a block that is proving.
clock["t"] = 0.0
r3 = FakeRunner(verify_at_tick=3)


def exploding():
    raise OSError("ssh: connect to host bridge port 22: Connection timed out")


out3 = tip_run.run_range(height=123540, cards=["a"], runner=r3, bundle_path="/tmp/x",
                         now=now, sleep=sleep, abort=exploding)
check(out3.get("ok") and r3.stopped == 0,
      f"an abort check that RAISES is survived, not obeyed (ok={out3.get('ok')})")


# ── 2. an abandoned block is neither a success nor a failure ────────────────────────────────────
state = tip_session.new_state(started_at=0.0, duration_s=3600)

n1 = tip_session.record_attempt(state, "123538")
n2 = tip_session.undo_attempt(state, "123538")
check(n1 == 1 and n2 == 0,
      f"an abandoned block gives its attempt back ({n1} -> {n2}), so three tips do not retire it")
check("123538" not in state["attempts"],
      "and leaves no trace that would count against MAX_ATTEMPTS later")

# it must survive the round trip several times, which is the real pattern: claim, step aside, repeat
for _ in range(5):
    tip_session.record_attempt(state, "123538")
    tip_session.undo_attempt(state, "123538")
check(state["attempts"].get("123538", 0) == 0,
      f"five tip arrivals in a row still leave the block un-retired "
      f"(attempts={state['attempts'].get('123538', 0)}, MAX={tip_session.MAX_ATTEMPTS})")

# ⛔ AND THE SESSION LOOP MUST NOT COUNT IT AS A FLEET FAILURE. Pinned to the source: the abort
# branch has to come BEFORE record_block and the consecutive_fails counter, or the fleet releases
# itself after three tip arrivals.
_src = open(os.path.join(HERE, "tip_session.py")).read()
_abort_at = _src.find('if result.get("aborted")')
# ⚠ THE CALL SITE, NOT THE DEFINITION. `_src.find("record_block(state, rng, result")` matches
# `def record_block(...)` first -- which sits near the top of the file, so the ordering assertion
# compared the abort branch against a function definition and failed while the code was correct.
_record_at = _src.find("        record_block(state, rng, result, at=now())")
_fail_at = _src.find("consecutive_fails += 1")
check(_abort_at != -1 and _record_at != -1 and _abort_at < _record_at,
      "the session handles an abort BEFORE recording the block")
check(_abort_at != -1 and _fail_at != -1 and _abort_at < _fail_at,
      "⛔ and before the failure counter, or three tips release a healthy fleet")

EXPECTED_CONTROL = {"a board block steps aside when a tip lands",
                    "⛔ and the aggregate is STOPPED on the way out",
                    "and the workers are disarmed",
                    "promptly",
                    "⛔ a half-written bundle_968317.json.tmp is NOT a waiting tip"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — without preemption the fleet finishes its board block first, and the "
              "old filter calls a half-written bundle a waiting tip:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly {len(EXPECTED_CONTROL)} assertions to fail; "
          f"got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("the gaps are the board's, and the tip takes them back within one tick")

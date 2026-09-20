#!/usr/bin/env python3
"""Tests for tip_session.py — the 24-hour session layer (Phase 5 ⑩).

The failures here are expensive rather than loud: a session that re-proves work it already did, one
that terminates a pod belonging to something else, one that starts a block it cannot finish, one that
loses its own spend. None of them raise; all of them cost GPU hours or destroy other people's work.

  python3 test_tip_session.py            # assertions; exit 0 on success
  python3 test_tip_session.py --control  # the fleet-ownership gate is removed; MUST fail
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_session as ts   # noqa: E402

if CONTROL:
    # ⛔ THE OWNERSHIP GATE REMOVED: terminate anything asked for that is not on the name denylist.
    # The denylist knows two pods; the fleet check is what protects every pod it has never heard of.
    def _terminable(pod_ids, session_fleet):
        out = {"terminate": [], "protected": [], "not_ours": []}
        for pid in pod_ids:
            (out["protected"] if ts.is_protected(pid) else out["terminate"]).append(str(pid))
        for k in out:
            out[k].sort()
        return out
    ts.terminable = _terminable

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


T0 = 1789900000.0
DAY = 24 * 3600


def fresh(**kw):
    kw.setdefault("started_at", T0)
    kw.setdefault("duration_s", DAY)
    kw.setdefault("fleet_ids", ["hz-01", "hz-02", "hz-03"])
    return ts.new_state(**kw)


# ── 1. ⛔ A SESSION MUST NEVER TERMINATE WHAT IT DID NOT CREATE ───────────────────────────────────
st = fresh()
out = ts.terminable(["hz-01", "hz-02", "hz370-a", "hz-board-5", "someone-elses-pod"], st["fleet"])
check(out["terminate"] == ["hz-01", "hz-02"], f"only the session's own pods are terminable ({out['terminate']})")
check(out["protected"] == ["hz-board-5", "hz370-a"],
      "⛔ the anchoring spine worker and the GHOST board card are PROTECTED BY NAME, in code")
check(out["not_ours"] == ["someone-elses-pod"],
      "⛔ a pod this session did not create is refused — the denylist only knows two names, the "
      "ownership check protects every pod it has never heard of")
check(ts.is_protected("hz370-a") and ts.is_protected("hz-board-5"),
      "both protected pods are named")

# The refusals are RETURNED, not dropped — a pod we declined to kill is a pod still being billed.
check(set(out) == {"terminate", "protected", "not_ours"},
      "the refusals are reported, because a pod left running is still costing money")

# ── 2. resume: the whole point ────────────────────────────────────────────────────────────────────
d = tempfile.mkdtemp(prefix="sess_")
p = os.path.join(d, "session.json")
st = fresh()
ts.record_block(st, "965500", {"ok": True, "wall_s": 540.0, "digest": "abc123", "cards": 27}, at=T0 + 600)
ts.record_block(st, "965501", {"ok": True, "wall_s": 520.0, "digest": "def456", "cards": 27}, at=T0 + 1200)
ts.add_spend(st, 1.39)
ts.save(p, st)

back = ts.load(p)
check(back is not None and len(ts.done_blocks(back)) == 2,
      "⛔ a saved session reloads with its completed blocks — without this a driver that dies at "
      "hour 19 re-proves the whole day")
check(back["spend_usd"] == 1.39, "and its spend")
check(not os.path.exists(p + ".tmp"), "the save left no temp file")

# ⛔ A CORRUPT FILE IS None, NOT AN EMPTY SESSION. Returning new_state() would resume having forgotten
# every block proved and re-prove the lot at full price.
open(p + ".bad", "w").write("{not json")
check(ts.load(p + ".bad") is None, "a corrupt session file reads as None, never as a fresh session")
open(p + ".bad2", "w").write('{"unrelated": 1}')
check(ts.load(p + ".bad2") is None, "and so does a JSON file that is not a session")
check(ts.load(os.path.join(d, "nope.json")) is None, "a missing file is None")

# ── 3. ⛔ NEVER RE-PROVE A BLOCK THIS SESSION ALREADY PROVED ──────────────────────────────────────
# On a resume the coordinator can hand back the same block — an unexpired claim, or a tip that has
# not moved. Proving it again costs a full block of GPU for a receipt that already exists.
plan = ts.plan_next(back, T0 + 1800, work={"source": "tip", "range": "965500"})
check(plan["action"] == "idle" and "already proved" in plan["why"],
      f"a block already proved this session is not proved again ({plan['action']})")
plan = ts.plan_next(back, T0 + 1800, work={"source": "tip", "range": "965502"})
check(plan["action"] == "prove" and plan["range"] == "965502", "a new block is proved")

# ── 4. a block that keeps failing must not consume the session ───────────────────────────────────
st = fresh()
for i in range(ts.MAX_ATTEMPTS):
    n = ts.record_attempt(st, "965999")
check(n == ts.MAX_ATTEMPTS, f"attempts are counted ({n})")
plan = ts.plan_next(st, T0 + 60, work={"source": "tip", "range": "965999"})
check(plan["action"] == "idle" and "not being retried" in plan["why"],
      "⛔ a block that has failed MAX_ATTEMPTS times is abandoned — a 24-hour run must not spend "
      "itself proving one block zero times")

# ── 5. ⛔ DO NOT START A BLOCK THE SESSION CANNOT FINISH ──────────────────────────────────────────
st = fresh()
plan = ts.plan_next(st, T0 + DAY - 240, work={"source": "tip", "range": "966000"},
                    block_estimate_s=540)
check(plan["action"] == "stop" and "discard" in plan["why"],
      f"with 4 min left and a ~9 min block, the session stops rather than paying for a block it "
      f"will throw away ({plan['action']})")
plan = ts.plan_next(st, T0 + DAY - 1200, work={"source": "tip", "range": "966000"},
                    block_estimate_s=540)
check(plan["action"] == "prove", "with 20 min left it still proves")
plan = ts.plan_next(st, T0 + DAY + 1, work={"source": "tip", "range": "966000"})
check(plan["action"] == "stop" and "over" in plan["why"], "past the deadline the session stops")

# ── 6. an idle board is not a failure ─────────────────────────────────────────────────────────────
plan = ts.plan_next(fresh(), T0 + 60, work={"source": "idle", "range": None})
check(plan["action"] == "idle" and "nothing free" in plan["why"],
      "a board with nothing free is idle, not an error — a busy board is EX_TEMPFAIL, not a fault")

# ── 7. ⛔ SPEND ACCUMULATES, IT IS NEVER RECOMPUTED ───────────────────────────────────────────────
# The fleet changes size mid-session, so cards x rate x elapsed is wrong the moment one is released —
# quietly, and in whichever direction the last change went.
st = fresh()
ts.add_spend(st, 1.39)
ts.add_spend(st, 0.74)
check(abs(st["spend_usd"] - 2.13) < 1e-9, f"spend accumulates across blocks ({st['spend_usd']})")

# ── 8. the summary is what the operator reads ────────────────────────────────────────────────────
st = fresh()
for i, w in enumerate([540.0, 485.0, 610.0]):
    ts.record_block(st, str(965500 + i), {"ok": True, "wall_s": w, "digest": "d", "cards": 27},
                    at=T0 + 600 * (i + 1))
ts.record_block(st, "965600", {"ok": False, "wall_s": None}, at=T0 + 3000)
ts.add_spend(st, 4.17)
s = ts.summary(st, T0 + 3600)
check(s["blocks_ok"] == 3 and s["blocks_failed"] == 1, f"ok and failed are counted separately ({s['blocks_ok']}/{s['blocks_failed']})")
check(s["failed_ranges"] == ["965600"], "and the failed block is named, not just counted")
check(s["median_wall_s"] == 540.0 and s["fastest_wall_s"] == 485.0,
      f"median and fastest are reported ({s['median_wall_s']}, {s['fastest_wall_s']})")
check(abs(s["usd_per_block"] - 1.39) < 1e-9,
      f"⛔ cost per block is over PROVED blocks only ({s['usd_per_block']}) — dividing by attempts "
      f"would flatter a session that failed half of them")
check(s["remaining_s"] == round(DAY - 3600, 1), "the remaining window is reported")

# ── 9. resume_verdict: the fleet may not have survived ───────────────────────────────────────────
st = fresh()
v = ts.resume_verdict(st, ["hz-01", "hz-02", "hz-03"], T0 + 3600)
check(v["ok"] and v["gone"] == [] and v["extra"] == [], "an intact fleet resumes cleanly")

v = ts.resume_verdict(st, ["hz-01", "hz-99"], T0 + 3600)
check(v["ok"] and v["gone"] == ["hz-02", "hz-03"] and v["extra"] == ["hz-99"],
      f"⛔ a partly-lost fleet resumes but SAYS WHAT CHANGED (gone={v['gone']}, extra={v['extra']}) — "
      f"the survivors have been billing the whole time the driver was dead")

v = ts.resume_verdict(st, ["other-1", "other-2"], T0 + 3600)
check(not v["ok"] and "new fleet, not a resume" in v["why"],
      "⛔ a fleet with NO survivors is refused — carrying on would fail every block for a reason "
      "that looks like a code fault")

v = ts.resume_verdict(st, ["hz-01"], T0 + DAY + 1)
check(not v["ok"] and "already closed" in v["why"], "a session past its window does not resume")

# ── 10. run_session: a whole simulated day, at $0 ────────────────────────────────────────────────
class Clock:
    def __init__(self, t=T0): self.t = t
    def now(self): return self.t
    def sleep(self, s): self.t += s


d2 = tempfile.mkdtemp(prefix="sess2_")
p2 = os.path.join(d2, "session.json")
st = ts.new_state(started_at=T0, duration_s=3600.0, fleet_ids=["hz-01"])
clk = Clock()
proved, heights = [], iter(range(966000, 966100))


def prove_ok(rng):
    clk.t += 540.0
    proved.append(rng)
    return {"ok": True, "wall_s": 540.0, "digest": "aa" * 16, "cards": 27}


events = []
s = ts.run_session(state=st, path=p2, prove=prove_ok,
                   work_fn=lambda: {"source": "tip", "range": str(next(heights))},
                   now=clk.now, sleep=clk.sleep, block_estimate_s=540.0,
                   on_event=events.append)
check(s["blocks_ok"] == 6, f"a 1-hour session at 540 s a block proves 6 ({s['blocks_ok']})")
check(any("stopping" in e for e in events), "and says why it stopped")
check(ts.load(p2)["blocks"], "the session file is on disk at the end")

# ⛔ THE STATE IS ON DISK AFTER EVERY BLOCK, NOT AT THE END -- the case it exists for is the driver
# never reaching the end. Proved by killing the driver mid-session and reloading.
st2 = ts.new_state(started_at=T0, duration_s=3600.0, fleet_ids=["hz-01"])
p3 = os.path.join(d2, "s3.json")
clk2, hs = Clock(), iter(range(967000, 967100))


def prove_then_die(rng):
    clk2.t += 540.0
    if len(ts.done_blocks(st2)) >= 2:
        raise KeyboardInterrupt("driver killed")
    return {"ok": True, "wall_s": 540.0, "digest": "bb" * 16, "cards": 27}


try:
    ts.run_session(state=st2, path=p3, prove=prove_then_die,
                   work_fn=lambda: {"source": "tip", "range": str(next(hs))},
                   now=clk2.now, sleep=clk2.sleep)
except KeyboardInterrupt:
    pass
recovered = ts.load(p3)
check(recovered is not None and len(ts.done_blocks(recovered)) == 2,
      f"⛔ a driver killed mid-session leaves 2 proved blocks on disk "
      f"({len(ts.done_blocks(recovered or {'blocks': {}}))}) — this is the whole resume guarantee")

# ⛔ A BLOCK THAT RAISES IS RECORDED AND THE SESSION CONTINUES -- but it is counted, so MAX_ATTEMPTS
# can retire it rather than the session retrying one block for a day.
st3 = ts.new_state(started_at=T0, duration_s=3600.0, fleet_ids=["hz-01"])
clk3 = Clock()
calls = []


def prove_always_fails(rng):
    clk3.t += 60.0
    calls.append(rng)
    raise RuntimeError("seg-serve panicked")


s3 = ts.run_session(state=st3, path=os.path.join(d2, "s4.json"), prove=prove_always_fails,
                    work_fn=lambda: {"source": "tip", "range": "968000"},
                    now=clk3.now, sleep=clk3.sleep, idle_s=60.0)
check(len(calls) == ts.MAX_ATTEMPTS,
      f"a block that always fails is tried exactly MAX_ATTEMPTS times ({len(calls)}), not for ever")
check(s3["blocks_failed"] == 1 and s3["failed_ranges"] == ["968000"],
      "and the session ends reporting it by name, rather than claiming success")

# ⛔ AN IDLE SESSION STILL HONOURS ITS DEADLINE. A board that never frees a block would otherwise
# sleep past the window holding a rented fleet with nothing to show.
st4 = ts.new_state(started_at=T0, duration_s=600.0, fleet_ids=["hz-01"])
clk4 = Clock()
s4 = ts.run_session(state=st4, path=os.path.join(d2, "s5.json"),
                    prove=lambda r: {"ok": True}, work_fn=lambda: {"source": "idle", "range": None},
                    now=clk4.now, sleep=clk4.sleep, idle_s=120.0)
check(clk4.t <= T0 + 600.0 + 1,
      f"⛔ an idle session stops AT its deadline ({clk4.t - T0:.0f}s of 600) — it does not sleep "
      f"past the window holding a rented fleet")
check(s4["blocks_ok"] == 0, "having proved nothing, which it reports rather than hides")

EXPECTED_CONTROL_FAILURES = {
    "a pod this session did not create is refused",
}

print()
if CONTROL:
    got = set(fails)
    hit = {e for e in EXPECTED_CONTROL_FAILURES if any(e in f for f in got)}
    if hit == EXPECTED_CONTROL_FAILURES:
        print("CONTROL OK — the fleet-ownership gate was removed and the assertion that detects it "
              "failed, as it must:")
        for e in sorted(hit):
            print(f"  - {e}")
        sys.exit(0)
    print("CONTROL FAILED — a pod belonging to something else was cleared for termination.")
    for e in sorted(EXPECTED_CONTROL_FAILURES - hit):
        print(f"  should have failed and did not: {e}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

#!/usr/bin/env python3
"""The spend a session stops on is the spend it writes down (hazync#513).

⛔ WHY THIS EXISTS — measured on the 2026-09-24 trial hour:

    session.json   spend_usd = $4.54
    RunPod         billed      $11.03

The in-memory accounting was fine — the session's own summary said $10.45. What was wrong is that
nothing SAVED it. `spend_fn()` accrues at the top of each loop, covering the block that just
finished; state is written when a block is recorded; and the loop then broke out without writing.
So the figure on disk was permanently one block behind, and this run's last block took 1,910 s.

⚠ THE FILE IS NOT A REPORT, IT IS THE BUDGET. A resumed session reads `spend_usd` from it and
checks `--budget-usd` against that number, so the gap silently refills a budget already spent.

⛔ AND THE LOOP HAS THREE EXITS. My first fix saved at the planner's `stop` branch only; the test
below happened to leave through the exhausted-idle guard and still saw $0.00 on disk, which is the
only reason I found out. Both exits are exercised here on purpose.

  python3 test_spend_persisted.py            # must PASS
  python3 test_spend_persisted.py --control  # the post-loop save is removed; MUST show as the gap
"""
import json
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_session                                                           # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


RATE_HR = 11.10
BLOCK_S = 1910.0            # the real 968,340: most of the run inside ONE prove() call


def drive(work_fn, label):
    """Run a session to completion and report (summary, on_disk, billed)."""
    if CONTROL:
        _last["spend"] = _last["blocks"] = None      # each run gets its own before/after
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "session.json")
        state = tip_session.new_state(started_at=0.0, duration_s=3600)
        clock, charged = {"t": 0.0}, {"t": 0.0}

        def now():
            return clock["t"]

        def sleep(s):
            clock["t"] += s

        def spend_fn():
            owed = RATE_HR * (clock["t"] - charged["t"]) / 3600.0
            charged["t"] = clock["t"]
            return owed

        def prove(rng):
            clock["t"] += BLOCK_S
            return {"ok": True, "block": str(rng), "wall_s": BLOCK_S, "cards": 15, "digest": "d"}

        summ = tip_session.run_session(
            state=state, path=path, prove=prove, work_fn=work_fn, now=now, sleep=sleep,
            on_event=lambda m: None, idle_s=1.0, block_estimate_s=lambda: BLOCK_S,
            budget_usd=20.0, spend_fn=spend_fn)
        # ⚠ Read INSIDE the context. Returning the path and opening it later reads a file the
        # TemporaryDirectory has already removed -- which is what my first version of this test did,
        # and it failed on the fixed code while the code was correct.
        reloaded = tip_session.load(path)
        on_disk = float((reloaded or {}).get("spend_usd", 0.0)) if reloaded else 0.0
        return summ.get("spend_usd", 0.0), on_disk, RATE_HR * clock["t"] / 3600.0


if CONTROL:
    # ⛔ THE OLD BEHAVIOUR, IDENTIFIED PRECISELY. The defect was that the save AFTER the loop did
    # not exist, so the file kept whatever the last recorded block wrote. That save is the only one
    # where the spend has moved while the block count has NOT -- a block save always adds a block,
    # and the idle branch writes nothing at all. Suppressing exactly it restores the old shape
    # without touching any other write.
    #
    # ⚠ An earlier version of this control flipped a flag inside `summary()`, which run_session
    # calls AFTER the save -- so it suppressed nothing and the control passed while testing
    # nothing. Hooking the thing itself is what makes this a control.
    _real_save = tip_session.save
    _last = {"spend": None, "blocks": None}

    def _save(path, state):
        blocks = len(state.get("blocks", {}))
        spend = float(state.get("spend_usd", 0.0))
        if _last["blocks"] is not None and blocks == _last["blocks"] and spend > _last["spend"] + 1e-9:
            return path                       # the post-loop save: dropped, as it used to be
        _last["blocks"], _last["spend"] = blocks, spend
        return _real_save(path, state)
    tip_session.save = _save


def same_block():
    # leaves through the EXHAUSTED-IDLE guard: the board keeps offering a block already proved
    return lambda: {"source": "board", "range": "124673"}


def new_blocks():
    # leaves through the planner's STOP: not enough of the window left for another block
    n = {"i": 0}

    def w():
        n["i"] += 1
        return {"source": "board", "range": str(124600 + n["i"])}
    return w


for maker, label in ((same_block, "exhausted-idle exit"), (new_blocks, "not-enough-time exit")):
    summ, disk, billed = drive(maker(), label)
    check(abs(disk - summ) < 0.01,
          f"{label}: what the session reports (${summ:.2f}) is what it writes (${disk:.2f})")
    check(disk > billed * 0.9,
          f"{label}: the saved figure is within 10% of billed (${disk:.2f} of ${billed:.2f})")

# ⚠ THE POINT IS THE BUDGET, NOT THE REPORT. A resume must see the real remaining budget.
summ, disk, billed = drive(new_blocks(), "resume")
left = 20.0 - disk        # exactly the arithmetic a resumed session does
check(left < 20.0 - billed * 0.9,
      f"a resumed session sees ${left:.2f} left of $20, not a budget that quietly refilled")

EXPECTED_CONTROL = {
    "exhausted-idle exit: what the session reports",
    "exhausted-idle exit: the saved figure is within 10%",
    "not-enough-time exit: what the session reports",
    "not-enough-time exit: the saved figure is within 10%",
    "a resumed session sees",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — without the post-loop save, the last block's cost is accrued and "
              "dropped, on BOTH exits:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("the budget on disk is the budget that was actually spent, whichever way the session ends")

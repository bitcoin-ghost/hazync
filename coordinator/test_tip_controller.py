#!/usr/bin/env python3
"""The tip controller's scaling decisions: resize the fleet from the prover's own progress (Phase 5 ⑧).

WHY THIS EXISTS. The tip target is every block proven inside 10 minutes and published at tip-1. The fleet
is sized DURING the proof, so the decision function is the feature — nothing else decides whether a card
is rented or dropped, and a wrong call either misses the target or spends money on cards that cannot help.

⛔ THE TWO GUARDS UNDER TEST, both of which protect against acting on a number that is not there:

  1. HOLD WHEN THERE IS NO PROJECTION. seg-serve reports "joins 25/2352" through the whole assembly
     phase with NO elapsed and NO ETA. Treating a missing projection as zero would resize the fleet at
     the two moments it is least informative — the start, and the join tree, which does not divide
     across workers at all (main.rs:5381). Adding cards there cannot help and still costs money.
  2. NEVER CULL THE LAST CARD. slow_tail compares each card against the fleet's own median on THIS
     block. With one card there is no median to be slow against, and the "slow" card is the only one
     proving. Dropping it stops the block.

⛔ PINNED LITERALS AND REAL CALLS. Every fixture below is a line seg-serve actually printed (the 2,352
segment run and the 71-segment block 230,000 run, both 2026-09-18), and every assertion calls the real
function and checks its real return. An earlier suite in this repo re-implemented the rule inside the
test and asserted its own arithmetic; it passed whatever the code did. The control is what caught it.

Usage:
  python3 test_tip_controller.py            # assertions; exit 0 on success
  python3 test_tip_controller.py --control  # both guards disabled; MUST fail
"""

import os
import sys

CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tip_controller as tc  # noqa: E402

if CONTROL:
    # ⛔ THE MODULE'S OWN FLAGS, not monkeypatches. Two earlier attempts patched from out here and were
    # silently INERT: stubbing projected_total_s to return 0 for a missing projection made the join phase
    # read as comfortably inside budget, so scale_decision still held — passing for the wrong reason; and
    # slow_tail's floor is max(1, keep_min), which no keep_min=0 argument can lower. The control reported
    # both guards as "should have failed and did not", which is exactly what it is for.
    tc._CONTROL_IGNORE_NO_PROJECTION = True
    tc._CONTROL_IGNORE_LAST_CARD = True

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── the parser: lines seg-serve really printed ────────────────────────────────────────────────────────

p = tc.parse_progress("     91/2352 segments  124s elapsed, ~3071s left")
check(p == {"phase": "segments", "done": 91, "total": 2352, "elapsed_s": 124, "eta_s": 3071},
      "the segment line parses: number first, noun plural, and the prover's OWN eta")

p71 = tc.parse_progress("    32/71 segments  60s elapsed, ~73s left")
check(p71 and p71["total"] == 71 and p71["eta_s"] == 73,
      "the small-block form (block 230,000, 71 segments) parses the same way")

j = tc.parse_progress("     joins 25/2352")
check(j == {"phase": "joins", "done": 25, "total": 2352, "elapsed_s": None, "eta_s": None},
      "the join line parses with NO timing, because seg-serve reports none there")

check(tc.parse_progress("  listening on 127.0.0.1:9110") is None,
      "a line that is not progress is not mistaken for progress")
check(tc.parse_progress(">>> PUSH-TRANSPORT RECEIPT VERIFIED against METHOD_ID") is None,
      "the receipt line is not progress either")

# ── the projection ────────────────────────────────────────────────────────────────────────────────────

check(tc.projected_total_s({"phase": "segments", "done": 91, "total": 2352,
                            "elapsed_s": 124, "eta_s": 3071}) == 3195,
      "the projection is elapsed + the prover's eta (124 + 3071 = 3195), not a rate we recomputed")

# ── scaling: the operator's rule ──────────────────────────────────────────────────────────────────────

SEG_SLOW = {"phase": "segments", "done": 91, "total": 2352, "elapsed_s": 124, "eta_s": 3071}
SEG_OK = {"phase": "segments", "done": 91, "total": 2352, "elapsed_s": 124, "eta_s": 320}
SMALL = {"phase": "segments", "done": 32, "total": 71, "elapsed_s": 60, "eta_s": 73}
JOINS = {"phase": "joins", "done": 25, "total": 2352, "elapsed_s": None, "eta_s": None}

d = tc.scale_decision(SMALL, 4)
check(d["action"] == "hold" and d["cards"] == 0,
      "a block projecting 133s against a 600s target holds: no cards are rented to beat a target already met")

d = tc.scale_decision(SEG_OK, 25)
check(d["action"] == "hold", "444s projected against 450s of headroom still holds")

d = tc.scale_decision(SEG_SLOW, 25)
check(d["action"] == "add" and d["cards"] == 12,
      "a block projecting 3195s against 450s ADDS cards, capped at the growth step")
check("178" in d["why"],
      "...and says what the naive 1/cards arithmetic implied, so the cap is visible rather than silent")

# ⛔ Growth is stepped, not stacked: each look measures the last one's effect.
seen, cards = [], 25
for _ in range(4):
    dd = tc.scale_decision(SEG_SLOW, cards)
    seen.append(cards)
    cards += dd["cards"]
check(seen == [25, 37, 49, 61],
      "successive looks grow 25 -> 37 -> 49 -> 61 rather than jumping to the implied 178")

d = tc.scale_decision(SEG_SLOW, 75, max_cards=75)
check(d["action"] == "at_limit" and d["cards"] == 0,
      "a maxed fleet that will still miss reports at_limit, NOT hold")
check("MISS" in d["why"],
      "...and says plainly that the block will miss the target, because that is when the operator is needed")

# 1. ⛔ THE FIRST CONTROL CASE.
d = tc.scale_decision(JOINS, 25)
check(d["action"] == "hold" and d["cards"] == 0,
      "with no projection (the join phase) the fleet holds rather than resizing on a missing number")

# ── the slow tail: measured against the fleet's own median ────────────────────────────────────────────

check(tc.slow_tail({"a": 1.0, "b": 1.1, "c": 0.9, "d": 0.2, "e": 0.25}) == ["d", "e"],
      "the slow tail is dropped, slowest first, against the fleet median on THIS block")
check(tc.slow_tail({"a": 0.2, "b": 0.2, "c": 0.2}) == [],
      "a block that is slow for EVERY card culls nobody — the block is slow, not the fleet")
check(tc.slow_tail({"a": 1.0, "b": 0.4}) == ["b"],
      "a half-speed card among two is dropped")

# ✅ AN INVARIANT, NOT A CONTROLLED GUARD. With one card the median IS that card's rate, so
# `r < median * fraction` is `0.5 < 0.3` — false. A lone card is never in its own tail, by arithmetic.
# This was listed as a control case through three failed "fixes" to the keep_min floor before the trace
# showed the floor was never what protected it. It stays asserted; it is simply not disableable.
check(tc.slow_tail({"a": 0.5}) == [],
      "a lone card is never below its own median, so it is never in the tail")

# ⚠ THE FLEET NEEDS A FAST MAJORITY FOR A TAIL TO EXIST AT ALL. With {1.0, 0.2, 0.25} the median is
# 0.25 — inside the slow pair — so the threshold is 0.15 and NOTHING is below it. Three cards, two of
# them plainly slower, and the correct answer is still "cull nobody". A first draft of the case below
# used exactly that shape and would have passed plain while failing to fail under control.
check(tc.slow_tail({"a": 1.0, "b": 0.2, "c": 0.25}) == [],
      "a median that sits inside the slow group detects no tail, so nothing is culled")

# 2. ⛔ THE SECOND CONTROL CASE — the keep_min FLOOR, a real guard and genuinely disableable.
# Two fast cards put the median at 0.625, so c and d are both a real tail; keep_min=3 lets only one go.
check(tc.slow_tail({"a": 1.0, "b": 1.0, "c": 0.2, "d": 0.25}, keep_min=3) == ["c"],
      "the keep_min floor caps how much of a real tail may be dropped, slowest first")
check(tc.slow_tail({"a": 1.0, "b": 1.0, "c": 0.2, "d": 0.25}, keep_min=1) == ["c", "d"],
      "...and with room to spare the whole tail goes")

# ── the work source ───────────────────────────────────────────────────────────────────────────────────

check(tc.next_work(967714, lambda: None) == {"source": "tip", "range": "967714"},
      "a waiting tip block is proved first")
check(tc.next_work(None, lambda: "98285") == {"source": "board", "range": "98285"},
      "with no tip block, the board's next block comes from the coordinator's HANDOUT")
# ⚠ FIELDS, NOT DICT EQUALITY (hazync#505). This compared the whole dict, so adding the REASON an
# idle carries broke an assertion whose subject -- "a busy board is idle, not an error" -- was
# untouched. The two things this must actually pin are the verdict and the range; the reason is
# asserted separately, below, because a missing one is its own bug.
_idle = tc.next_work(None, lambda: None)
check(_idle["source"] == "idle" and _idle["range"] is None,
      "a busy board (every block claimed) is idle, not an error: hazync maps that to EX_TEMPFAIL")
check(bool(_idle.get("why")) and "board" in _idle["why"],
      f"and it says the BOARD is what had nothing, so the session does not have to guess "
      f"({_idle.get('why')!r})")

print()
EXPECTED_CONTROL_FAILURES = {
    "with no projection (the join phase) the fleet holds rather than resizing on a missing number",
    "the keep_min floor caps how much of a real tail may be dropped, slowest first",
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

#!/usr/bin/env python3
"""A verified block must never un-finish, and board work must not un-finish a tip block.

⛔ OBSERVED LIVE, tip hour 4, 2026-09-29. The operator watched the headline counter fall from 1 to 0
and grid tiles revert from green to orange, every time board fill ran between tip blocks. Two causes,
one symptom:

  1. `_newest` was `max(int(h) for h in agg)` — the numerically HIGHEST height. A tip-following
     session interleaves board work at the frontier (133,100) with tip blocks (969,119), so the
     highest height is always the tip block, even while every card is demonstrably busy on a board
     block. The finished tip block kept the benefit of the doubt, `working_on_this` stayed true, and
     the silence rule never fired for it.

  2. `done` was recomputed from scratch every cycle from three inputs that can all lapse:
     `verified` is parsed out of `$RUNDIR/phase`, which the run OVERWRITES — in session mode it reads
     `SESSION · 1.0 h on 35 cards` and names no height at all, so the source the code calls
     "authoritative" is empty; `finished_by_cards` needs a card still reporting phase=done; and the
     silence rule loses to activity on any other block.

⚠ THIS HAS BEEN FIXED TWICE BEFORE in narrower forms (#499's mid-block handover, and the per-block
fix whose comment records "968,257 verified at 11:14:43, and by 11:18:41 the frame read 0 blocks").
Both addressed a path INTO the wrong answer. A latch closes the class: verification is not reversible,
so the frame should not be able to represent it as reversible.

    python3 test_done_latch.py             # done is monotonic; board work cannot un-finish a tip block
    python3 test_done_latch.py --control   # no latch and highest-height newest — the flip must reproduce
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collect  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0

TIP, BOARD = 969119, 133100


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def card(name, block, phase, heights, *, up=True, rate=2.09):
    """One card row as collect.py consumes it. `heights` is {h: (t0, t1)}."""
    return {"name": name, "block": block, "phase": phase, "up": up, "cost_hr": rate,
            "block_s": {str(h): {"t0": t0, "t1": t1, "n": 5, "segs": 100, "prove": 1.0, "asm": 0.1}
                        for h, (t0, t1) in heights.items()}}


def run(cards, state, now, verified=()):
    """blocks_from_cards, or the pre-fix behaviour under --control."""
    if not CONTROL:
        return collect.blocks_from_cards(cards, state, now, verified)
    # The shipped-before behaviour: highest height wins, and no latch.
    agg = {}
    for c in cards:
        for h, e in (c.get("block_s") or {}).items():
            a = agg.setdefault(int(h), {"t1": e["t1"]})
            a["t1"] = max(a["t1"], e["t1"])
    active = [c for c in cards if c.get("up") and c.get("phase") in
              ("proving", "assembling", "executed")]
    named = {str(c.get("block")) for c in active if c.get("block")}
    newest = max(agg, default=None)                      # ⛔ numerically highest
    out = []
    for h, a in agg.items():
        working = bool(active) and (str(h) in named or (not named and h == newest))
        out.append({"h": h, "done": (h in verified) or ((now - a["t1"]) > 5 and not working)})
    return out


def done_of(blocks, h):
    for b in blocks:
        if b["h"] == h:
            return bool(b.get("done"))
    return None


# ── the live sequence: a tip block finishes, then board fill starts ─────────────────────────────
state = {}
# t=100: the tip block is proved and quiet; nothing is working.
b = run([card("hz-1", None, "idle", {TIP: (10.0, 90.0)})], state, 100.0)
check(done_of(b, TIP) is True, f"tip {TIP} reads done once it is quiet")

# t=200: board fill is now RUNNING. No card names a height (only the coordinator ever does), and the
# board height is far below the tip height.
cards_boarding = [card("hz-1", None, "proving", {TIP: (10.0, 90.0), BOARD: (150.0, 199.0)}),
                  card("hz-2", None, "proving", {BOARD: (151.0, 199.5)})]
b = run(cards_boarding, state, 200.0)
got = done_of(b, TIP)
if CONTROL:
    check(got is False,
          f"control reproduces it: tip {TIP} flips back to NOT done while board {BOARD} runs — "
          f"the counter falls to 0 and the tile goes orange")
else:
    check(got is True,
          f"tip {TIP} STAYS done while board {BOARD} runs (it was {got})")

    # And the reason, independently: the fleet's most recent activity is the BOARD height.
    b2 = collect.blocks_from_cards(cards_boarding, {}, 200.0)
    check(done_of(b2, TIP) is True,
          "and it holds even with NO latched state — the active-height fix alone is sufficient")

    # ── the latch's own property: no input at all, still done ───────────────────────────────────
    # `verified` empty (the phase line names no height in session mode) and no card reports the
    # height any more. This is the state that produced the live regression.
    b3 = collect.blocks_from_cards(
        [card("hz-1", None, "proving", {BOARD: (150.0, 260.0)})], state, 300.0, verified=())
    check(TIP in state.get("done_heights", set()), "the latch remembers the finished tip height")
    check(done_of(b3, TIP) is None,
          "⚠ a height with no samples this cycle is simply absent, not reported as unfinished")

    # ── a block still being worked must NOT be latched early ────────────────────────────────────
    st2 = {}
    mid = collect.blocks_from_cards(
        [card("hz-1", str(TIP), "proving", {TIP: (10.0, 99.0)})], st2, 100.0)
    check(done_of(mid, TIP) is False, "a block a card NAMES as proving is not done")
    check(TIP not in st2.get("done_heights", set()),
          "⛔ and it is NOT latched — a latch that fires early would freeze a wrong answer for ever")

    # ── board blocks get the same protection ────────────────────────────────────────────────────
    st3 = {}
    collect.blocks_from_cards([card("hz-1", None, "idle", {BOARD: (10.0, 90.0)})], st3, 100.0)
    nxt = collect.blocks_from_cards(
        [card("hz-1", None, "proving", {BOARD: (10.0, 90.0), TIP: (150.0, 199.0)})], st3, 200.0)
    check(done_of(nxt, BOARD) is True,
          "a finished BOARD block stays done once the fleet moves to a tip block")

print()
if fails:
    print(f"FAIL: {fails}")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

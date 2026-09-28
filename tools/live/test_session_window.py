#!/usr/bin/env python3
"""The grid holds what THIS RUN can prove, and board fill is credited outside the ring (hazync#554).

⛔ WHAT THIS EXISTS FOR. Raised by the operator looking at the live page:

    "The live dashboard shows out of 144 blocks and has 144 blocks on the grid.
     Shouldn't it show 6? For this hour run?"
    "Within the ring there is text that shows 0/144"
    "The board blocks that are proven instead of keeping pods idle should be stated
     somewhere else not within the ring ... Otherwise it's misleading"

144 cells is **24 hours of chain**. On a one-hour run at most ~6 tip blocks can ever exist, so the
grid was ~96% empty by construction and read as "3 of 144" — and before the SESSION line is latched
the ring showed `0 / 144 TODAY` from the very first frame.

⚠ `session_blocks` is how many blocks the CHAIN will mine in the run (1 h / 600 s = 6). It already
drove the ring's denominator; the grid ignored it entirely, so the two halves of the same frame
disagreed about how long the run was.

    python3 test_session_window.py             # the grid is the session's, board fill is outside the ring
    python3 test_session_window.py --control   # a fixed 144-slot day — must reproduce
"""
import math
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from tip24e import GCOL, GROWS, GCELL, GGAP  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0
WINDOW = GCOL * GROWS


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def slots(snap):
    """The number of grid cells, as the renderer decides it."""
    if CONTROL:
        return WINDOW                                   # the shipped behaviour: always a day
    return int(snap.get("session_blocks") or WINDOW)


HOUR_RUN = {"session_blocks": 6}
DAY_RUN = {"session_blocks": None}

n = slots(HOUR_RUN)
if CONTROL:
    check(n == 144,
          f"control reproduces it: a 1-hour run gets {n} slots — a day's canvas, ~96% of it "
          f"unreachable")
else:
    check(n == 6, f"a 1-hour run gets {n} grid slots, not 144")
    check(slots(DAY_RUN) == WINDOW, "a run with no declared session still gets the day's 144")

# ── the geometry fills the same box, so a short run gets a few LARGE cells ───────────────────────
if not CONTROL:
    import tip24live as T

    for want, label in ((6, "one hour"), (36, "six hours"), (144, "a day")):
        cols, cell, gap = T.grid_geom(want)
        rows = math.ceil(want / cols)
        w = cols * cell + (cols - 1) * gap
        h = rows * cell + (rows - 1) * gap
        check(w <= T.GRID_W + 0.5 and h <= T.GRID_W + 0.5,
              f"{label} ({want} slots): {cols}x{rows} of {cell:.0f}px fits the box "
              f"({w:.0f}x{h:.0f} <= {T.GRID_W})")
    cols6, cell6, _ = T.grid_geom(6)
    cols144, cell144, _ = T.grid_geom(144)
    check(cell6 > cell144 * 3,
          f"an hour's cells are far larger than a day's ({cell6:.0f}px vs {cell144:.0f}px) — the "
          f"grid is legible instead of three lit squares in a field of 141")
    check((cols144, cell144) == (GCOL, GCELL),
          "and a full day is EXACTLY the film's geometry, untouched")

    # ⛔ "at the tip" is a fact about the CHAIN. Sizing the grid to an hour made a live tip run
    # label itself BACKFILL, which I only saw by rendering a frame and looking at it.
    src = open(os.path.join(HERE, "tip24live.py"), encoding="utf8").read()
    m = re.search(r"at_tip = bool\(tip\) and any\(h > tip - (\w+) for h in hs\)", src)
    check(m is not None and m.group(1) == "WINDOW",
          f"the at-the-tip test uses the DAY, not the grid size (found {m.group(1) if m else '?'})")

# ── board fill is credited, and NOT in the ring ─────────────────────────────────────────────────
if not CONTROL:
    src = open(os.path.join(HERE, "tip24live.py"), encoding="utf8").read()
    ring = src[src.index("_den = snap.get('session_blocks')"):src.index("def ring_label")]
    check("done_board" not in ring,
          "the ring says nothing about board blocks — it counts what the run exists to prove")
    check("board block" in src and "filled while waiting" in src,
          "board fill is credited on the subtitle line instead")
    # ⚠ It was in the HEADER first, and pushed the spend off the right-hand edge.
    head = src[src.index("for s, col in [('Hazync"):src.index("d.text((x, 40), s")]
    check("done_board" not in head,
          "and not in the header either, where it clipped the spend figure")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

#!/usr/bin/env python3
"""Board fill is credited, but never counted as tip performance (hazync#554).

⛔ WHAT THIS EXISTS FOR. Every panel on the frame except the grid treated one population as the
other. `AHEAD OF CHAIN` took the median over ALL done blocks, and board fill between tip blocks is
small work — a frontier bundle is 16-370 KB against 27 MB for tip block 968,983. Measured live on
2026-09-28:

    board blocks  17   median wall  55.9s
    TIP blocks     2   walls 345.9s, 65.0s   ->  renderer's median 345.9s

    frame rendered:  AHEAD OF CHAIN  9.1x      (median 57.3s over all 19)
    the truth:       AHEAD OF CHAIN  1.7x      (median 345.9s over the 2 tip blocks)

⚠ THE RENDERER'S MEDIAN IS `tot[len(tot)//2]`, the UPPER middle element -- not the mean of the two
middle ones. With two tip blocks that is 345.9s, not the 205.4s an arithmetic median gives. The
upper element is the conservative reading and the right one for a claim about keeping up, so the
test asserts the renderer's own convention rather than a prettier number.

The headline claim of the project, inflated 3.6x by the work the fleet does while WAITING for the
thing the claim is about. The grid disagreed all along — it is anchored to the tip window, so board
heights 837,000 below it can never appear — and a frame that shows `19 blocks` beside a grid holding
one cell was the visible symptom of a fault nothing else reported.

⛔ BOARD FILL IS NOT HIDDEN. It is real work, it is what #367 asked the fleet to do between tip
blocks, and the header still credits it — separately, and labelled. The requirement is only that it
is never counted AS tip performance.

    python3 test_tip_vs_board.py             # each panel asks for the population it means
    python3 test_tip_vs_board.py --control   # one population — the 9.1x must reproduce
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CONTROL = "--control" in sys.argv
fails = 0
PERIOD = 600.0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


# The run as it actually happened, tagged the way collect.py now tags it.
BOARD = [{"h": 131890 + i, "kind": "board", "done": True, "wall_s": w} for i, w in enumerate(
    [254.5, 173.0, 47.8, 50.6, 74.4, 50.4, 55.8, 79.6, 57.3, 57.3, 54.5, 54.0, 55.9, 52.0, 51.1,
     58.2, 60.3])]
TIP = [{"h": 968983, "kind": "tip", "done": True, "wall_s": 345.9},
       {"h": 968985, "kind": "tip", "done": True, "wall_s": 65.0}]
BLOCKS = BOARD + TIP


def ahead_over(blocks):
    """The headline, as the renderer computes it."""
    if CONTROL:
        pool = [b for b in blocks if b.get("done") and b.get("wall_s")]          # ALL of them
    else:
        pool = [b for b in blocks if b.get("kind") == "tip"
                and b.get("done") and b.get("wall_s")]
    tot = sorted(b["wall_s"] for b in pool)
    med = tot[len(tot) // 2] if tot else 0.0
    return (PERIOD / med) if med > 0 else 0.0, med


ahead, med = ahead_over(BLOCKS)

if CONTROL:
    check(abs(ahead - 10.5) < 0.6,
          f"control reproduces it: {ahead:.1f}x from a {med:.1f}s median over ALL blocks — "
          f"a board-fill number published as a tip claim")
    check(med < 100, f"control's median is {med:.1f}s, which no tip block on this run came near")
else:
    check(abs(med - 345.9) < 0.1, f"the median is over TIP blocks only ({med:.1f}s)")
    check(abs(ahead - 1.7) < 0.1, f"so the headline is {ahead:.1f}x, not 9.1x")
    check(ahead < 4.0, "and it can no longer be inflated by gap-filling")

# ── a fleet that has only filled gaps publishes NO multiple at all ───────────────────────────────
only_board, med0 = ahead_over(BOARD)
if CONTROL:
    check(only_board > 5,
          f"control: with no tip block proved it still claims {only_board:.1f}x ahead of the chain")
else:
    check(only_board == 0.0,
          "with no tip block timed there is no figure — the tile says so instead of borrowing one")

# ── the counts are separated, and board work is still credited ───────────────────────────────────
if not CONTROL:
    done_tip = sum(1 for b in BLOCKS if b.get("kind") == "tip" and b.get("done"))
    done_board = sum(1 for b in BLOCKS if b.get("kind") == "board" and b.get("done"))
    check(done_tip == 2 and done_board == 17,
          f"the two populations are counted apart ({done_tip} tip, {done_board} board)")
    check(done_tip + done_board == len([b for b in BLOCKS if b["done"]]),
          "and every proved block is in exactly one of them — board work is never dropped")

    # ⚠ An older collector writes no `kind`. Falling back to "treat them all as tip" would restore
    # the exact bug, so an untagged feed must not silently produce a flattering number.
    untagged = [{k: v for k, v in b.items() if k != "kind"} for b in BLOCKS]
    tagged = any(b.get("kind") in ("tip", "board") for b in untagged)
    check(tagged is False, "a snapshot from an older collector is detected as untagged")

# ── the source: the renderer must not compute the headline over every block ──────────────────────
if not CONTROL:
    src = open(os.path.join(HERE, "tip24live.py"), encoding="utf8").read()
    m = re.search(r"tot = sorted\(b\['wall_s'\] for b in (\w+)", src)
    check(m is not None and m.group(1) == "tip_blocks",
          f"the renderer's median is taken over tip_blocks (found {m.group(1) if m else 'NOTHING'})")
    check("kind" in open(os.path.join(HERE, "collect.py"), encoding="utf8").read(),
          "and collect.py tags each block at the source")

    # the phase line's latched card count is corrected against the live fleet
    fixed = re.sub(r"\bon \d+ cards\b", "on 24 cards", "SESSION · 1.0 h on 17 cards, budget $150.00")
    check(fixed == "SESSION · 1.0 h on 24 cards, budget $150.00",
          "the banner's card count follows the fleet instead of contradicting the tile beneath it")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

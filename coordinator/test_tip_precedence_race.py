#!/usr/bin/env python3
"""A tip bundle landing between reading the bridge and claiming must still win (hazync#585).

📏 MEASURED, tip hour 4, 2026-09-29. Bundle appearance times from the bridge, against the run's log:

    969,121's bundle appeared  08:55:29
    board block 133152 claimed 08:55:33   <- FOUR SECONDS later
    tip 969,121 not started until 08:58:40 <- 191 s late

⚠ TIP PRECEDENCE IS NOT WEAK IN GENERAL, and a fix built on that premise would be aimed at the wrong
thing. 969,122 was taken in the SAME SECOND the fleet freed up, and 133,087 was abandoned 3 s after
969,118's bundle landed. The hole is exactly one window: `tip_block` is a value the caller computes
BEFORE `next_work` runs, so a bundle arriving in that interval loses to a board claim which then holds
the fleet for its entire duration.

⛔ IT CANNOT BE REPRODUCED BY TIMING. It needs a bundle to land inside a window a few seconds wide, so
this drives the selection path directly with a bridge that answers "nothing" the first time and "here
is a tip block" the second — which is exactly the sequence the live failure took.

    python3 test_tip_precedence_race.py             # the late arrival wins
    python3 test_tip_precedence_race.py --control   # no re-check — the board claim must win
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_controller  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def select(tip_block, bridge_answers, board="133152"):
    """Run the real next_work. `bridge_answers` is what a re-check would return, in order."""
    claimed = []

    def claim_fn():
        claimed.append(board)
        return board

    seq = list(bridge_answers)

    def recheck():
        return seq.pop(0) if seq else None

    if CONTROL:
        out = tip_controller.next_work(tip_block, claim_fn)      # the shipped-before signature
    else:
        out = tip_controller.next_work(tip_block, claim_fn, recheck_tip_fn=recheck)
    return out, claimed


# ── 1. THE LIVE FAILURE: nothing pending, a bundle lands before the claim ───────────────────────
out, claimed = select(None, ["969121"])
if CONTROL:
    check(out["source"] == "board",
          f"control reproduces it: the board block wins ({out['range']}) though 969,121 had arrived")
    check(claimed == ["133152"], "and the board claim was actually made")
else:
    check(out["source"] == "tip" and out["range"] == "969121",
          f"the late-arriving tip block wins ({out['source']} {out['range']})")
    check(claimed == [], "⛔ and NO board claim is made — claiming then abandoning still costs the run")
    check(out.get("late_arrival") is True,
          "and it is marked as a late arrival, so the log can say why board work was skipped")

# ── 2. the common case is unchanged: a tip already pending never consults the bridge again ──────
if not CONTROL:
    calls = []

    def recheck_counted():
        calls.append(1)
        return "969999"

    out2 = tip_controller.next_work("969120", lambda: "133000", recheck_tip_fn=recheck_counted)
    check(out2["source"] == "tip" and out2["range"] == "969120",
          "a tip already pending is taken as before")
    check(calls == [], "⚠ and the re-check is NOT called — no extra bridge read on the hot path")

# ── 3. genuinely no tip: board work still happens ───────────────────────────────────────────────
if not CONTROL:
    out3, claimed3 = select(None, [None])
    check(out3["source"] == "board" and claimed3 == ["133152"],
          "with no tip block at all, board work is still claimed — the fill is not disabled")

# ── 4. ⛔ A FAILING RE-CHECK MUST NOT STOP THE RUN ──────────────────────────────────────────────
if not CONTROL:
    def boom():
        raise RuntimeError("bridge unreachable")

    out4 = tip_controller.next_work(None, lambda: "133001", recheck_tip_fn=boom)
    check(out4["source"] == "board",
          "⛔ a re-check that RAISES falls through to board work rather than killing the session")

# ── 5. and the idle verdict still carries its own reason ────────────────────────────────────────
if not CONTROL:
    out5 = tip_controller.next_work(None, lambda: None, recheck_tip_fn=lambda: None)
    check(out5["source"] == "idle" and out5.get("why"),
          f"an idle still says why ({str(out5.get('why'))[:40]})")

print()
if fails:
    print(f"FAIL: {fails}")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

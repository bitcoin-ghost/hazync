#!/usr/bin/env python3
"""--fresh-tip waits for a FRESH tip, and fills the wait with board work (hazync#367, #506).

⚠ THIS FILE USED TO ASSERT THE OPPOSITE, and was right to at the time. Board claims were being
fetched from the bridge, which emits nothing below EMIT_FROM, so every one failed instantly and
three in a row released a 36-card fleet 17 seconds into a session. Refusing board work under
--fresh-tip was the emergency measure that stopped that.

The fetch is fixed (board work now always comes from /api/witness), so the refusal has outlived its
cause — and its cost is measured: the 2026-09-23 flagship hour left a 15-card fleet idle for 31.4 of
60 minutes, $5.81 of rented GPU doing nothing, while the board had work waiting.

⛔ WHAT MUST STILL BE TRUE: the tip always wins. --fresh-tip still means only heights that did not
exist at boot, and a board block gives way the moment a tip bundle lands. That second half is
`tip_run.run_range(abort=...)` and is tested in test_board_fill.py; this file covers the planner.

  python3 test_fresh_tip.py            # must PASS
  python3 test_fresh_tip.py --control  # the old blanket refusal is restored; MUST show as the gap
"""
import os
import sys

sys.path.insert(0, ".")
import tip_controller

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def work_fn(fresh_tip, tip_available, floor, claim_calls, *, no_board_fill=False):
    """A model of the driver's plan step. Pinned to the real source below."""
    def claim_fn():
        claim_calls.append(1)
        return "123538"
    pending = tip_available if (tip_available and tip_available > floor) else None
    # CONTROL restores the old rule: --fresh-tip refuses the board unconditionally.
    if fresh_tip and pending is None and (no_board_fill or CONTROL):
        return {"source": "idle", "range": None}
    return tip_controller.next_work(pending, claim_fn)


# ── 1. the gap between tip blocks goes to the board ──────────────────────────────────────────────
calls = []
w = work_fn(True, 968307, 968307, calls)             # tip equals the floor: nothing fresh
check(w["source"] == "board" and w["range"] == "123538",
      f"--fresh-tip with no fresh tip takes BOARD work (got {w})")
check(len(calls) == 1, f"and actually claims it (claim called {len(calls)}x)")

# ── 2. a fresh tip still wins, and no claim is spent looking at the board ───────────────────────
calls = []
w = work_fn(True, 968308, 968307, calls)
check(w["source"] == "tip" and w["range"] == "968308", f"a FRESH tip is still proved first (got {w})")
check(not calls,
      f"⛔ and the board is not even asked — a claim taken here would be held for its TTL while the "
      f"fleet proves the tip (claim called {len(calls)}x)")

# ── 3. the escape hatch still buys a clean latency measurement ──────────────────────────────────
calls = []
w = work_fn(True, 968307, 968307, calls, no_board_fill=True)
check(w["source"] == "idle" and not calls,
      f"--no-board-fill restores the idle, for runs measuring tip latency alone (got {w})")

# ── 4. without --fresh-tip nothing changed ──────────────────────────────────────────────────────
calls = []
w = work_fn(False, 968307, 968307, calls)
check(w["source"] == "board", f"without --fresh-tip the board fallback is unchanged (got {w})")

# ── 5. ⛔ THE MODEL ABOVE IS NOT THE DRIVER. Pin it to the source, or they drift ─────────────────
# This file hand-writes the planner because the real one is a closure inside main() and cannot be
# imported. That is exactly how this test came to assert obsolete behaviour and still pass: the
# driver changed and the model did not. Asserting the shape of the real condition is what ties them
# together -- if someone restores the blanket refusal in tip_smoke.py, this fails.
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "tip_smoke.py")).read()
check("a.fresh_tip and pending is None and a.no_board_fill" in _src,
      "the driver's guard reads `a.fresh_tip and pending is None and a.no_board_fill`")
check("--no-board-fill" in _src, "and --no-board-fill is a real flag, not just a test fiction")

EXPECTED_CONTROL = {"--fresh-tip with no fresh tip takes BOARD work",
                    "and actually claims it"}
print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the old blanket refusal restored, the gaps go back to costing "
              "$5.81 an hour in idle cards:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected only the two board-fill assertions to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("the tip still comes first, and the wait for it is no longer paid for in idle cards")

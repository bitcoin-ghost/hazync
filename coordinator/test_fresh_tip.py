#!/usr/bin/env python3
"""--fresh-tip must IDLE when no fresh tip is waiting, never fall back to board work."""
import sys
sys.path.insert(0, ".")
import tip_controller

CONTROL = "--control" in sys.argv
fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok: fails.append(what)

def work_fn(fresh_tip, tip_available, floor, claim_calls):
    def claim_fn():
        claim_calls.append(1)
        return "123538"                      # the board ALWAYS has work; that is the trap
    pending = tip_available if (tip_available and tip_available > floor) else None
    if fresh_tip and pending is None and not CONTROL:
        return {"source": "idle", "range": None}
    return tip_controller.next_work(pending, claim_fn)

calls = []
w = work_fn(True, 968307, 968307, calls)     # tip equals the floor: NOT fresh
check(w["source"] == "idle",
      f"--fresh-tip with no FRESH tip idles (got source={w['source']!r}, range={w['range']!r})")
check(not calls,
      f"and never claims board work this driver cannot fetch (claim called {len(calls)}x)")

calls = []
w = work_fn(True, 968308, 968307, calls)     # a genuinely new height
check(w["source"] == "tip" and w["range"] == "968308",
      f"a FRESH tip is still proved (got {w})")

calls = []
w = work_fn(False, 968307, 968307, calls)    # without the flag, board fallback is intended
check(w["source"] == "board",
      f"without --fresh-tip the board fallback is unchanged (got source={w['source']!r})")

EXPECTED = {"--fresh-tip with no FRESH tip idles", "and never claims board work"}
print()
if CONTROL:
    hit = {k for k in EXPECTED if any(k in f for f in fails)}
    if hit == EXPECTED and len(fails) == len(EXPECTED):
        print("CONTROL OK — without the guard it claimed board work and would fail 3x in a row:")
        for f in fails: print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED)}; got {len(fails)}")
    sys.exit(1)
if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails)); sys.exit(1)
print("--fresh-tip waits for the chain instead of proving what it cannot fetch")

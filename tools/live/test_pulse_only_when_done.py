#!/usr/bin/env python3
"""A block that has not finished has no finish time (found on the 968,243/968,255 tip run, 2026-09-23).

⛔ WHY THIS EXISTS. The renderer animates a finished block travelling from the join tree to its cell
in the block map, for PULSE_S seconds after it finished. It fires on

    age = now - b['done_at'];  if 0.0 <= age < PULSE_S: draw it

and `done_at` was set unconditionally to `a["t1"]` -- the newest telemetry for that height. While a
block is still being PROVED, t1 advances to ~now on every tick, so `age` was ~1s forever and the
pulse re-launched every single frame. What it looks like on the page is not an obviously wrong
animation: it is the green dot at the join tree's convergence point drifting between frames, because
the pulse's dot is tethered to the root by a line and was mistaken for the root itself.

Measured live on block 968,243 while it was still proving: status=None, done_at = now - 1.1 s,
refreshed on every collector tick. Three consecutive published frames put the dot at
y = 401.5, 421.0, 428.0 -- a root that cannot move, moving.

  python3 test_pulse_only_when_done.py            # must PASS
  python3 test_pulse_only_when_done.py --control  # done_at set unconditionally again; MUST FAIL
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collect                                                                  # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


NOW = 1790155149.0
PULSE_S = 6.0


def blocks(*, last_sample_age, phase, verified=()):
    """One height, whose newest telemetry is `last_sample_age` seconds old."""
    t1 = NOW - last_sample_age
    cards = [{"name": "hz-smoke-12", "cost_hr": 0.49, "phase": phase, "block": 968243,
              "block_s": {"968243": {"t0": t1 - 300, "t1": t1, "segs": 10666,
                                     "prove": 300.0, "asm": 0.0, "n": 300}}}]
    return collect.blocks_from_cards(cards, {"since": 0.0}, NOW, set(verified))


# ⛔ THE CONTROL MUTATES THE SHIPPED FUNCTION so what fails below is the real code path.
if CONTROL:
    _orig = collect.blocks_from_cards

    def unconditional(cards, state, now, verified=()):
        out = _orig(cards, state, now, verified)
        for b in out:                      # CONTROL: done_at back to "newest telemetry, always"
            for c in cards:
                e = (c.get("block_s") or {}).get(str(b["h"]))
                if e:
                    b["done_at"] = e["t1"]
        return out

    collect.blocks_from_cards = unconditional


def pulse_fires(b, now=NOW):
    """Exactly the renderer's condition (tip24live.py)."""
    t = b.get("done_at")
    if not t:
        return False
    age = now - t
    return 0.0 <= age < PULSE_S


# ── 1. ⛔ THE CENTRAL CASE. Still proving, telemetry 1s old: no finish time, no pulse ─────────────
b = blocks(last_sample_age=1.1, phase="proving")[0]
check(not b["done"], f"a block still streaming is not done (done={b['done']})")
check(b["done_at"] is None,
      f"and carries NO finish time — it has not finished (done_at={b['done_at']})")
check(not pulse_fires(b), "so the pulse does not fire, and the tree's root sits still")

# ── 2. the pulse must still work for a block that genuinely finished ─────────────────────────────
b = blocks(last_sample_age=1.0, phase="done")[0]
check(b["done"], "a card reporting phase=done marks the block done")
check(b["done_at"] is not None and pulse_fires(b),
      f"and the pulse DOES fire for it — the fix must not kill the animation (done_at={b['done_at']})")

# ── 3. the silence rule: no card has mentioned the height for >5s ────────────────────────────────
b = blocks(last_sample_age=7.0, phase="proving")[0]
check(b["done"], "5 seconds of silence on a height still counts as done")
check(b["done_at"] is not None,
      "and it has a finish time — the last moment a card reported it, which is that instant")

# ── 4. a verified block, however old, is done ────────────────────────────────────────────────────
b = blocks(last_sample_age=1.0, phase="proving", verified=(968243,))[0]
check(b["done"] and b["done_at"] is not None,
      "a VERIFIED height is done even while a card is still streaming it")

# ── 5. ⛔ AND THE PULSE MUST STOP. Long after finishing, it must not still be animating ───────────
b = blocks(last_sample_age=1.0, phase="done")[0]
check(not pulse_fires(b, now=NOW + 60),
      "a minute later the pulse has stopped — it is a moment, not a state")

# ⚠ NAME EVERY ONE AND COUNT THEM. The control restores one line and two separate readouts go wrong:
# the block claims a finish time it does not have, and the renderer then animates on it.
EXPECTED_CONTROL = {
    "and carries NO finish time",
    "so the pulse does not fire",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with done_at set unconditionally, a block that is still proving reports a "
              "finish time and the pulse fires forever:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly {len(EXPECTED_CONTROL)} failures; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("only a finished block has a finish time, and the pulse is a moment rather than a state")

#!/usr/bin/env python3
"""The ring sits beside the traces, and never back on top of them.

⛔ WHY THIS EXISTS. The ring used to be drawn over the lanes — a 364px disc in the middle of a 738px
band — so every waveform ran behind it and the busy middle of each trace, the part worth looking at,
was the part hidden. Moving it left is a layout decision that a later geometry tweak could undo
without anyone noticing until a frame is on the public page.

⚠ This checks the INVARIANT, not the exact numbers. Pinning RCX=274 would fail the first time
somebody nudged the padding, which is a change nobody wants a test to forbid. What must hold is that
the ring's right edge is clear of the first lane, that the lanes still have room to draw in, and that
the ring has not been pushed off the page to satisfy the first two.

  python3 test_stage_layout.py            # must PASS
  python3 test_stage_layout.py --control  # the old centred geometry; MUST FAIL
"""
import os
import sys

CONTROL = "--control" in sys.argv
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tip24live as t                                                        # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


if CONTROL:
    # ⛔ THE CONTROL IS THE GEOMETRY THAT SHIPPED BEFORE: ring centred in the lane band.
    t.LX0 = 92
    t.RCX = (t.LX0 + t.LX1) // 2

ring_l, ring_r = t.RCX - t.ROUT, t.RCX + t.ROUT

check(ring_r <= t.LX0,
      f"the ring's right edge ({ring_r}) is clear of the first lane ({t.LX0}) — "
      f"{'no overlap' if ring_r <= t.LX0 else f'{ring_r - t.LX0}px of every trace is hidden'}")

check(ring_l >= t.PAD,
      f"and its left edge ({ring_l}) has not been pushed off the page's padding line ({t.PAD})")

# ⚠ Narrower is the point, but a lane band narrow enough to be useless is not an improvement. 240px
# still carries a 1 Hz waveform legibly; below that the trace is a smudge.
band = t.LX1 - t.LX0
check(band >= 240, f"the lanes still have room to read as waveforms ({band}px wide)")

# The join tree's leaf lines run from LX1 to TX0, so the lanes must stop before the tree starts.
check(t.LX1 <= t.TX0, f"the lanes end ({t.LX1}) before the join tree begins ({t.TX0})")

# ⛔ RCY IS SHARED. The block map's GY0 and the join tree's root both derive from it, so moving the
# ring vertically would silently drag them off the centre line they align to.
check(t.GY0 == t.RCY - (t.GROWS * t.GCELL + (t.GROWS - 1) * t.GGAP) // 2,
      "the block map is still centred on the ring's own centre line")

EXPECTED_CONTROL = {"the ring's right edge"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the old centred geometry the ring covers the traces again:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly the overlap assertion to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("the ring sits beside the traces, and the stage still lines up")

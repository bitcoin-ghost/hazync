#!/usr/bin/env python3
"""No connector is drawn from the join tree to the grid, and a finished block still reads as finished.

⛔ WHAT CHANGED, AND WHY THE OLD TEST IS GONE. This file used to assert the opposite: that a block
finishing drew a faint line from the tree's convergence point to that block's grid cell, and that the
line MOVED across the frame over PULSE_S. The operator removed it on 2026-09-29 — *"the green line
that points from folding to proving is shit — get rid of it"* — so those assertions were enforcing
behaviour the page is not supposed to have any more, and they failed as soon as it went.

⚠ WHAT THAT GIVES UP, AND WHY THIS TEST STILL EXISTS. The pulse was the only element marking the
MOMENT a block finished; every other element shows a state, so a block completing now looks identical
to one that completed ten minutes ago. That loss is deliberate, but the thing it was protecting is
not: a finished block must still be VISIBLE as finished. So this now pins both halves —
no connector, and the cell is green.

⛔ IT RENDERS REAL FRAMES AND COMPARES PIXELS. "The code ran" says nothing about what was drawn, and
the whole class of bug here is drawing nothing, or drawing it in the wrong place.

  python3 test_pulse.py            # no connector; a done block's cell is green
  python3 test_pulse.py --control  # reinstate a connector in the source — it MUST be detected
"""
import json
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "tip24live.py"), encoding="utf8").read()
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


WORK = tempfile.mkdtemp(prefix="pulse-")
TIP = 969119


def snap(done_age_s):
    """A snapshot with one finished tip block, `done_age_s` ago."""
    now = 1790000000.0
    return {
        "t": now, "tip": TIP, "session_blocks": 6,
        "cards": [{"name": "hz-1", "up": True, "cost_hr": 2.09, "phase": "idle",
                   "block": None, "power": [100.0]}],
        "blocks": [{"h": TIP, "kind": "tip", "done": True, "wall_s": 121.1,
                    "done_at": now - done_age_s, "arrive": now - 300, "segs": 2000,
                    "cost": 5.0, "cards": 38}],
    }


def frame(done_age_s, name, src=None):
    """Render a frame; returns its path. `src` overrides tip24live.py for the control."""
    sp = os.path.join(WORK, f"{name}.json")
    with open(sp, "w") as fh:
        json.dump(snap(done_age_s), fh)
    out = os.path.join(WORK, f"{name}.png")
    script = os.path.join(HERE, "tip24live.py")
    if src is not None:
        script = os.path.join(WORK, "tip24live_control.py")
        with open(script, "w") as fh:
            fh.write(src)
    r = subprocess.run([sys.executable, script, "--snap", sp, "--out", out],
                       capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        check(False, f"render {name} failed: {(r.stderr or '')[-300:]}")
        return None
    return out


def connector_pixels(png):
    """Pixels in the corridor BETWEEN the tree and the grid that are pulse-coloured.

    ⚠ Sampled in the band the old line crossed, away from the ring and the grid themselves, so a
    green grid cell cannot be mistaken for a connector.
    """
    from PIL import Image
    im = Image.open(png).convert("RGB")
    w, h = im.size
    n = 0
    for y in range(int(h * 0.42), int(h * 0.58)):
        for x in range(int(w * 0.48), int(w * 0.70)):
            r, g, b = im.getpixel((x, y))
            if g > r + 14 and g > b + 14 and g > 60:      # greenish and not near-black
                n += 1
    return n


def green_cells(png):
    """Greenish pixels in the grid band — a finished block's cell."""
    from PIL import Image
    im = Image.open(png).convert("RGB")
    w, h = im.size
    n = 0
    for y in range(int(h * 0.62), int(h * 0.95)):
        for x in range(int(w * 0.70), w - 4):
            r, g, b = im.getpixel((x, y))
            if g > r + 14 and g > b + 14 and g > 60:
                n += 1
    return n


# ── 1. the frame renders at all ─────────────────────────────────────────────────────────────────
fresh = frame(1.0, "fresh")
check(fresh is not None, "a frame with a just-finished block renders")

# ── 2. the finished block is still VISIBLY finished ─────────────────────────────────────────────
if fresh:
    gc = green_cells(fresh)
    check(gc > 40, f"the finished block's cell is green ({gc} px) — removing the pulse must not "
                   f"remove the only sign a block landed")

# ── 3. ⛔ NO CONNECTOR, at any age in what used to be the pulse window ──────────────────────────
if not CONTROL and fresh:
    found = {age: connector_pixels(f) for age, f in
             ((a, frame(a, f"age{int(a*10)}")) for a in (0.5, 2.0, 4.0, 5.5)) if f}
    check(all(v < 25 for v in found.values()),
          f"no connector is drawn between the tree and the grid at any age ({found})")
    check("d.line([TX1, RCY" not in SRC,
          "and the source no longer draws it")
    check("px, py" not in SRC,
          "⚠ nor keeps the pulse geometry as dead code — it was removed, not just stopped")

# ── 4. THE CONTROL: put a connector back and prove this test can see it ─────────────────────────
if CONTROL:
    # Reinstate exactly the line that was removed, at a fixed mid-frame position.
    marker = "    # ⛔ NO FINISH PULSE (operator, 2026-09-29"
    assert marker in SRC, "the comment marking the removal is not there — rebase this control"
    inject = ("    for _b in grid_blocks:\n"
              "        if _b.get('done_at'):\n"
              "            d.line([TX1, RCY, TX1 + 300, RCY + 40], fill=mix(OK, GROUND, .18), width=2)\n"
              "            break\n")
    ctrl = SRC.replace(marker, inject + marker, 1)
    f = frame(1.0, "control", src=ctrl)
    if f:
        px = connector_pixels(f)
        check(px >= 25,
              f"control: a reinstated connector IS detected ({px} px) — so assertion 3 can fail")

print()
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

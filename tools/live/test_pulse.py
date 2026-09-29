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
    # ⛔ `--once` IS NOT OPTIONAL. Without it tip24live renders in a LOOP and never exits, so the
    # subprocess times out and the failure reads as "the renderer is slow" — I lost two runs to that,
    # once blaming a loaded laptop. The renderer's default is the live behaviour; a test wants one frame.
    # ⚠ The control copy of the renderer lives OUTSIDE this directory, so its `from tip24e import …`
    # (and tip24e's own `from tip24c import …`, and `import tip24grid`) resolve only with HERE on the
    # path. The test this replaced carried the same note; I dropped it in the rewrite and the control
    # died on ModuleNotFoundError instead of testing anything.
    env = dict(os.environ, PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, script, "--once", "--snap", sp, "--out", out],
                       capture_output=True, text=True, timeout=120, env=env)
    if r.returncode != 0:
        check(False, f"render {name} failed: {(r.stderr or '')[-300:]}")
        return None
    return out


def connector_pixels(png, baseline=None):
    """Pixels differing from a no-connector baseline in the corridor between tree and grid.

    ⛔ DIFFERENCING, NOT A COLOUR THRESHOLD. My first version tested for "greenish" pixels and reported
    0 against a connector that WAS drawn: the line is `mix(OK, GROUND, .18)`, an 18% blend, which does
    not clear a naive green test. The test this replaced diffed against a baseline frame for exactly
    that reason. A detector that cannot see the thing it guards is worse than no detector — it reports
    "no connector" whether or not there is one.
    """
    from PIL import Image, ImageChops
    im = Image.open(png).convert("RGB")
    if baseline is None:
        return 0
    base = Image.open(baseline).convert("RGB")
    diff = ImageChops.difference(base, im)
    w, h = diff.size
    box = diff.crop((int(w * 0.46), int(h * 0.40), int(w * 0.72), int(h * 0.60)))
    bb = box.getbbox()
    if bb is None:
        return 0
    # Count actually-changed pixels, so a one-pixel antialiasing wobble is not a connector.
    return sum(1 for px in box.getdata() if px != (0, 0, 0))


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
    # Frames at different ages must be IDENTICAL in the corridor: with the connector gone, nothing
    # there depends on how long ago a block finished.
    base = frame(60.0, "baseline")          # long past any pulse window
    found = {age: connector_pixels(f, base) for age, f in
             ((a, frame(a, f"age{int(a*10)}")) for a in (0.5, 2.0, 4.0, 5.5)) if f and base}
    check(found and all(v < 25 for v in found.values()),
          f"no connector appears in the corridor at any age ({found})")
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
    base = frame(1.0, "control_base")            # unmodified renderer, same snapshot
    f = frame(1.0, "control", src=ctrl)          # with the connector put back
    if f and base:
        px = connector_pixels(f, base)
        check(px >= 25,
              f"control: a reinstated connector IS detected ({px} changed px) — so the real arm can fail")

print()
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

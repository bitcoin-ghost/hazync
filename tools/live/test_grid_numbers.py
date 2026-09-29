#!/usr/bin/env python3
"""Every grid cell that has a block shows its height, from the moment it starts proving (hazync#586).

⛔ WHY. The grid coloured cells by state and carried no label, so it said how many blocks were in each
state and never WHICH. After a run, five green cells told you nothing without going back to the log —
and while the green→orange reversion was being chased (#582) the operator could only describe the
symptom, not name the block that flipped.

⚠ RENDERS REAL FRAMES AND COMPARES PIXELS. "The code ran" says nothing about what was drawn, and the
failure mode here is text that is present but invisible — drawn in an ink that matches its own cell.

    python3 test_grid_numbers.py             # labels drawn, legible on every cell state
    python3 test_grid_numbers.py --control   # one fixed ink for all states — must go invisible
"""
import json
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

# ⛔ SAY SO ONCE, AT THE TOP. Without Pillow every render subprocess dies and the test prints twelve
# separate failures, each one a pixel assertion reporting `None px`, with the real reason —
# ModuleNotFoundError — buried inside a truncated traceback. It reads as a broken feature, and that is
# how it reached CI on this branch. A missing tool is not a failing assertion.
try:
    import PIL  # noqa: F401
except ImportError:
    print("SKIPPED-NOT: Pillow is missing. This test renders real frames and cannot run without it.")
    print("             python3 -m pip install Pillow")
    sys.exit(2)          # ⚠ not 0 — a dependency this test needs being absent is a failure to run it.
SRC = open(os.path.join(HERE, "tip24live.py"), encoding="utf8").read()
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


WORK = tempfile.mkdtemp(prefix="gridnum-")
NOW = 1790000000.0
TIP = 969122


def snap(blocks):
    return {"t": NOW, "tip": TIP, "session_blocks": 6,
            "clock_armed_at": NOW - 1800, "session_duration_s": 3600.0,
            "cards": [{"name": "hz-1", "up": True, "cost_hr": 2.09, "phase": "idle",
                       "block": None, "power": [100.0]}],
            "blocks": blocks}


def blk(h, done, **kw):
    b = {"h": h, "kind": "tip", "done": done, "arrive": NOW - 400, "segs": 2000,
         "cost": 5.0, "cards": 38}
    if done:
        b["wall_s"] = 254.5
        b["done_at"] = NOW - 60
    b.update(kw)
    return b


def frame(blocks, name, src=None):
    sp = os.path.join(WORK, f"{name}.json")
    json.dump(snap(blocks), open(sp, "w"))
    out = os.path.join(WORK, f"{name}.png")
    script = os.path.join(HERE, "tip24live.py")
    if src is not None:
        script = os.path.join(WORK, "ctrl.py")
        open(script, "w", encoding="utf8").write(src)
    env = dict(os.environ, PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, script, "--once", "--snap", sp, "--out", out],
                       capture_output=True, text=True, timeout=120, env=env)
    if not os.path.exists(out):
        check(False, f"render {name} failed: {((r.stdout or '') + (r.stderr or ''))[-200:]}")
        return None
    return out


def ink_in_grid(png, baseline):
    """Pixels in the grid band that differ from a baseline with no labels."""
    from PIL import Image, ImageChops
    a, b = Image.open(baseline).convert("RGB"), Image.open(png).convert("RGB")
    diff = ImageChops.difference(a, b)
    w, h = diff.size
    # ⚠ MEASURED FROM THE GEOMETRY, not guessed: GX0..W is x 0.70..1.00 and GY0 spans y 0.19..0.59,
    # because the grid is vertically CENTRED on the ring rather than sitting in the lower band. My
    # first crop sampled 0.55..0.98 and reported 0 px for a cell that was plainly drawn.
    box = diff.crop((int(w * 0.70), int(h * 0.18), w - 2, int(h * 0.60)))
    return sum(1 for px in box.getdata() if px != (0, 0, 0))


# ⛔⛔ THE BASELINE MUST DIFFER ONLY BY THE LABEL. My first version diffed against a frame with NO
# BLOCKS, so the cell's own fill dominated: both states reported an identical 22,801 px and the test
# would have passed with no label drawn at all — the cell appearing, measured as if it were text.
# This is the second time in this suite I have measured the wrong thing (see test_pulse.py).
#
# So the baseline renders the SAME snapshot with the label code removed. The only difference between
# the two frames is the text, and the pixel count is the text.
# ⚠ Slice to the END OF THE LINE, not a byte offset. `+120` landed mid-comment and produced source
# that would not parse — the render then failed for a reason that had nothing to do with labels.
_s = SRC.index("        if b:\n            # Full height where it fits")
_e = SRC.index("\n", SRC.index("d.text((cx + (gcell - tw) / 2, cy + gcell / 2 - 8), lab,")) + 1
LABEL_BLOCK = SRC[_s:_e]
assert "d.text((cx" in LABEL_BLOCK and LABEL_BLOCK.endswith("\n"), "could not isolate the label block"
NO_LABEL_SRC = SRC.replace(LABEL_BLOCK, "        if False:\n            pass\n")
import ast as _ast
_ast.parse(NO_LABEL_SRC)          # ⛔ or the baseline fails to render and every count reads None


def label_ink(blocks, name):
    """Pixels that exist ONLY because the label is drawn."""
    with_lab = frame(blocks, name)
    without = frame(blocks, name + "_nolab", src=NO_LABEL_SRC)
    if not (with_lab and without):
        return None
    return ink_in_grid(with_lab, without)

# ── 1. a DONE block is labelled ─────────────────────────────────────────────────────────────────
n = label_ink([blk(TIP, True)], "done")
check(n is not None and 30 < n < 4000,
      f"a DONE cell carries a legible label ({n} px of text, not a whole cell)")

# ── 2. ⛔ AND SO IS ONE STILL PROVING — the operator asked for it from the moment it starts ──────
n2 = label_ink([blk(TIP, False)], "proving")
check(n2 is not None and 30 < n2 < 4000,
      f"a block still PROVING is labelled too ({n2} px) — not only once it is finished")

# ── 3. the label survives the whole life of the block ───────────────────────────────────────────
counts = {s: label_ink([b], f"life_{s}") for s, b in
          (("proving", blk(TIP, False)), ("done", blk(TIP, True)))}
check(all(v and v > 30 for v in counts.values()),
      f"legible in every state it passes through ({counts})")
check(len({v for v in counts.values() if v}) > 0,
      "⚠ and the counts are TEXT pixels — a whole cell would be ~22,000, which is what the first "
      "version of this test was accidentally measuring")

# ── 4. ⛔ THE CONTROL: one fixed ink for all states must go invisible on at least one ────────────
if CONTROL:
    marker = ("            if b.get('done') or h == live_h:\n"
              "                ink = mix(GROUND, TEXT, .12)         # near-background dark, on a bright cell\n"
              "            else:\n"
              "                ink = mix(TEXT, GROUND, .85)         # bright, on the dim pending cell")
    assert marker in SRC, "the per-state ink block is not where this control expects it"
    # One ink for everything: the page background. Invisible on the near-background empty/pending cell.
    # One ink for every state: the dim pending fill itself. Invisible on exactly that cell.
    ctrl = SRC.replace(marker, "            ink = mix(MAP_PROVING, GROUND, .6)")
    lit = frame([blk(TIP, False)], "ctrl_lab", src=ctrl)
    unlit = frame([blk(TIP, False)], "ctrl_nolab", src=NO_LABEL_SRC)
    if lit and unlit:
        n = ink_in_grid(lit, unlit)
        check(n < 30,
              f"control: one fixed ink matching the cell leaves the label invisible ({n} px) — "
              f"which is why the ink is chosen per state")

print()
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

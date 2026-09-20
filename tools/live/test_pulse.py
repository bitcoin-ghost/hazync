#!/usr/bin/env python3
"""Tests for the pulse in tip24live.py — a finished block travelling to its cell.

The pulse is the only thing on the frame that marks the MOMENT a block finishes; every other element
shows a state, so a block completing looks identical to one that completed ten minutes ago. It is
also the easiest thing to get subtly wrong: fire for the wrong block, fire for ever, or fire for a
block that has no cell to land in. These render real frames and compare pixels, because "the code
ran" says nothing about whether anything was drawn.

  python3 test_pulse.py            # assertions; exit 0 on success
  python3 test_pulse.py --control  # the age window is removed; MUST fail
"""
import json
import os
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


try:
    from PIL import Image, ImageChops
except ImportError:
    print("  ⛔ Pillow is not installed — the renderer cannot be exercised at all.")
    print("     This is a REAL missing dependency, not a reason to skip: the live renderer died on")
    print("     exactly this on the tip box on 2026-09-20 and the public page kept serving a stale")
    print("     frame. Install Pillow rather than making this test silently pass.")
    sys.exit(1)

WORK = tempfile.mkdtemp(prefix="pulse_")
RENDER = os.path.join(HERE, "tip24live.py")
COLLECT = os.path.join(HERE, "collect.py")

# The control removes the age window, so the pulse fires for every block on every frame.
SRC = open(RENDER, encoding="utf8").read()
if CONTROL:
    assert "if not (0.0 <= age < PULSE_S):" in SRC, "the guard this control removes is not there"
    SRC = SRC.replace("if not (0.0 <= age < PULSE_S):\n            continue",
                      "if False:\n            continue")
    RENDER = os.path.join(WORK, "tip24live_control.py")
    open(RENDER, "w", encoding="utf8").write(SRC)

# ⚠ The control copy of the renderer lives outside this directory, so its `from tip24e import …`
# (and tip24e's own `from tip24c import …`) only resolve with HERE on the path.
ENV = dict(os.environ, PYTHONPATH=HERE + os.pathsep + os.environ.get("PYTHONPATH", ""))

subprocess.run(["python3", COLLECT, "--demo", "--once", "--out", f"{WORK}/base.json"],
               capture_output=True, check=True, env=ENV)
BASE = json.load(open(f"{WORK}/base.json"))
NOW = BASE["t"]


RENDER_FAILURES = []


def frame(ages, name):
    """Render one frame; `ages` maps block index -> seconds since it finished.

    A render that produces no file is RECORDED, not raised: on the live page that is a frozen
    dashboard, which is the failure worth naming rather than a traceback out of the harness.
    """
    s = json.loads(json.dumps(BASE))
    for b in s["blocks"]:
        b["done_at"] = NOW - 600
    for i, age in ages.items():
        s["blocks"][i]["done_at"] = NOW - age
    json.dump(s, open(f"{WORK}/{name}.json", "w"))
    r = subprocess.run(["python3", RENDER, "--once", "--snap", f"{WORK}/{name}.json",
                        "--out", f"{WORK}/{name}.png"], capture_output=True, text=True, env=ENV)
    if not os.path.exists(f"{WORK}/{name}.png"):
        RENDER_FAILURES.append((name, ((r.stdout or "") + (r.stderr or "")).strip()[:90]))
        return None
    return Image.open(f"{WORK}/{name}.png").convert("RGB")


quiet = frame({}, "quiet")                       # nothing finished recently


def moved(im):
    if im is None or quiet is None:
        return None
    return ImageChops.difference(quiet, im).getbbox()


# ── 1. a block that just finished draws something ────────────────────────────────────────────────
bb = moved(frame({-1: 1.0}, "fresh"))
check(bb is not None, "a block that finished 1 s ago draws a pulse")

# ── 2. ⛔ IT TRAVELS. A pulse pinned at the tree apex is not a pulse ──────────────────────────────
spans = []
for age in (0.5, 2.0, 4.0, 5.5):
    b = moved(frame({-1: age}, f"t{age}"))
    spans.append((age, b[2] if b else None))
check(all(x is not None for _, x in spans), f"the pulse draws at every age in flight ({spans})")
rights = [x for _, x in spans if x]
# ⚠ Guarded against an empty list: under the control NOTHING renders, and an IndexError out of the
# harness would hide which assertion actually detected the removed guard.
check(len(rights) >= 2 and rights == sorted(rights) and rights[-1] > rights[0] + 50,
      f"⛔ it MOVES toward the grid: right edge "
      f"{rights[0] if rights else 'n/a'} → {rights[-1] if rights else 'n/a'} across 0.5 s → 5.5 s")

# ── 3. ⛔ AND IT STOPS. A pulse that never expires paints every finished block for ever ───────────
check(moved(frame({-1: 20.0}, "old")) is None,
      "⛔ a block that finished 20 s ago draws NOTHING — past PULSE_S the pulse is over")

# ── 4. a snapshot with no done_at at all must still render ───────────────────────────────────────
s = json.loads(json.dumps(BASE))
for b in s["blocks"]:
    b.pop("done_at", None)
json.dump(s, open(f"{WORK}/legacy.json", "w"))
r = subprocess.run(["python3", RENDER, "--once", "--snap", f"{WORK}/legacy.json",
                    "--out", f"{WORK}/legacy.png"], capture_output=True, text=True, env=ENV)
check(os.path.exists(f"{WORK}/legacy.png"),
      f"⚠ a snapshot from an older collector (no done_at) still renders — the pulse simply does not "
      f"fire, rather than the frame failing ({r.stderr[:80]})")

# ── 5. ⚠ a clock that runs ahead of ours is not a fresh block ────────────────────────────────────
check(moved(frame({-1: -30.0}, "future")) is None,
      "⚠ a done_at in the FUTURE (a card clock ahead of ours) does not fire a pulse")

# ── 6. ⛔ EVERY FRAME MUST RENDER ─────────────────────────────────────────────────────────────────
# The age window also keeps the travel fraction in [0, 1). Without it a block that finished ten
# minutes ago gives f = 100, the token's radius goes to -293, and PIL refuses to draw the ellipse —
# so the renderer produces NO FRAME AT ALL. On the live page that is a dashboard frozen on its last
# good frame, which is precisely the failure the staleness banner exists to expose.
check(not RENDER_FAILURES,
      f"⛔ every frame rendered — a pulse with no age window drives the radius negative and PIL "
      f"refuses ({RENDER_FAILURES[:2]})")

EXPECTED_CONTROL_FAILURES = {
    "every frame rendered",
}

print()
if CONTROL:
    hit = {e for e in EXPECTED_CONTROL_FAILURES if any(e in f for f in fails)}
    if hit == EXPECTED_CONTROL_FAILURES:
        print("CONTROL OK — the age window was removed and the assertion that detects it failed, "
              "as it must:")
        for e in sorted(hit):
            print(f"  - {e}")
        sys.exit(0)
    print("CONTROL FAILED — a pulse that never expires went undetected.")
    for e in sorted(EXPECTED_CONTROL_FAILURES - hit):
        print(f"  should have failed and did not: {e}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

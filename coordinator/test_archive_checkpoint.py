#!/usr/bin/env python3
"""Tests for hazync-archive-checkpoint.sh's "is a rung due?" decision (hazync#347).

WHY THIS EXISTS. The script decides whether to keep a bridge checkpoint, and the failure it can have is
SILENT IN THE SAFE-LOOKING DIRECTION: when it wrongly concludes "not due" it exits 0, prints one tidy
line, and archives nothing. Nothing alerts, because nothing failed. Weeks later the rungs you assumed
were accumulating are not there, and the bundles prune_bundles.py deleted on the strength of
"there is a checkpoint below it" cannot be rebuilt.

⛔ THE SPECIFIC BUG THESE PIN. `LAST` used to be the newest rung in the archive, full stop. That is fine
with one bridge and wrong the moment two share an archive, which is exactly what the backfill walk does
(hazync-bridge-backfill.service walks 230,000 -> 740,000 while the live bridge is past 742,000):

    backfill archiver at H=300,000, seeing the live bridge's 740,000 rung
      -> H - LAST = 300000 - 740000 = -440000
      -> -440000 -lt 25000 is TRUE
      -> "not due", every hour, for the entire 41-hour walk, archiving nothing

The fix is to count only rungs AT OR BELOW the current height. Scenario 2 below is that case, and it is
what the positive control disables.

No real bridge and no real journal: `journalctl` is stubbed on PATH to report a chosen height, and every
run is DRY=1, so nothing is ever copied.

Usage:
  python3 test_archive_checkpoint.py            # assertions; exit 0 on success
  python3 test_archive_checkpoint.py --control  # the at-or-below rule is removed; MUST fail
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deploy", "hazync-archive-checkpoint.sh")

fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)

# ── the script under test, with the guard removed for the control ────────────────────────────────────
work = tempfile.mkdtemp(prefix="archck_")
script = os.path.join(work, "archive.sh")
src = open(SCRIPT).read()

GUARD = '[ "$_b" -gt "$H" ] && continue'
if GUARD not in src:
    print(f"CANNOT TEST: the guard line is not in {SCRIPT}.")
    print("These tests are pinned to the at-or-below rule; if it was renamed, update them deliberately.")
    sys.exit(2)

if CONTROL:
    # ⛔ Removes ONLY the at-or-below rule, so exactly one scenario should trip. Deliberately not
    # something broader like forcing LAST=0: that would also make scenario 1 wrong, and a control that
    # trips several assertions cannot tell you which guard it removed.
    src = src.replace(GUARD, ": # CONTROL: at-or-below rule removed")
open(script, "w").write(src)
os.chmod(script, 0o755)

# ── a stub journalctl, so no real unit is consulted ──────────────────────────────────────────────────
binv = os.path.join(work, "bin")
os.makedirs(binv)
def set_height(h):
    p = os.path.join(binv, "journalctl")
    open(p, "w").write(f'#!/bin/sh\necho "bridge: checkpoint @ {h}"\n')
    os.chmod(p, 0o755)

def run(height, rungs, spacing=25000):
    """DRY run against a fresh archive holding `rungs`; returns the script's stdout."""
    out_dir = tempfile.mkdtemp(prefix="out_", dir=work)
    arc_dir = tempfile.mkdtemp(prefix="arc_", dir=work)
    with open(os.path.join(out_dir, "state.bin"), "w") as f:
        f.write("x" * 4096)                      # must be non-empty or the script exits 2
    for r in rungs:
        open(os.path.join(arc_dir, r if isinstance(r, str) else f"state_{r}.bin"), "w").close()
    set_height(height)
    env = dict(os.environ)
    env.update({
        "PATH": binv + os.pathsep + env["PATH"],
        "HAZYNC_BRIDGE_OUT": out_dir,
        "HAZYNC_CKPT_ARCHIVE": arc_dir,
        "SPACING": str(spacing),
        "DRY": "1",
    })
    p = subprocess.run(["bash", script], env=env, capture_output=True, text=True, timeout=60)
    # DRY must never write a rung, whatever it decides.
    assert not [n for n in os.listdir(arc_dir) if n.endswith(".bin") and n not in
                [r if isinstance(r, str) else f"state_{r}.bin" for r in rungs]], "DRY run wrote a rung"
    return p.stdout

def due(out):      return "would archive" in out
def not_due(out):  return "not due" in out
def last_seen(out):
    m = re.search(r"newest rung (\d+)", out)
    return int(m.group(1)) if m else None

print(f"  (DRY runs against a stubbed journalctl{'; CONTROL: at-or-below rule removed' if CONTROL else ''})")

# 1. The live archiver's own case: a rung 2,257 blocks back is too close, so nothing is due.
o = run(742257, [230000, 740000])
check(not_due(o) and last_seen(o) == 740000, "the live bridge sees its own newest rung and waits")

# 2. ⛔ THE CONTROL CASE. The backfill archiver at a height BELOW the live bridge's rung. Counting the
#    newest rung overall gives a negative difference, which reads as "not due" for ever.
o = run(300000, [230000, 740000])
check(due(o) and last_seen(o) is None,
      "a bridge below another bridge's rung still archives (the never-fires bug)")

# 3. A rung above us and nothing below: there is no predecessor, so the first rung is due.
o = run(300000, [740000])
check(due(o), "a rung above the current height alone does not block the first archive")

# 4. An empty archive: the first rung is due.
check(due(run(300000, [])), "with no rungs at all the first is due")

# 5. Exactly at the spacing boundary — due (the comparison is -lt, so equality archives).
o = run(255000, [230000])
check(due(o), "exactly SPACING blocks past the last rung is due")

# 6. One block short of the boundary — not due.
o = run(254999, [230000])
check(not_due(o) and last_seen(o) == 230000, "one block short of SPACING is not due")

# 7. Malformed names must be skipped, not half-parsed. The .tmp case is real: the 230,000 rung was
#    streamed in as state_230000.bin.tmp, and a run mid-transfer must not treat it as a rung.
#
#    ⛔ The height is chosen so a misparse CHANGES THE ANSWER: with only the real 230,000 rung counting,
#    300,000 is 70,000 blocks past and due; if the .tmp were read as a 290,000 rung it would be 10,000
#    past and "not due". An assertion that merely accepted either outcome could not fail.
o = run(300000, ["state_.bin", "state_abc.bin", "state_290000.bin.tmp", "notastate.bin", 230000])
check(due(o), "malformed names and a partial .tmp are ignored, not counted as rungs")

# 8. The highest rung at or below wins, not merely the first found.
#
#    ⛔ 290,000 and not 300,000. At 300,000 the nearest rung below is 275,000, exactly SPACING away, and
#    the comparison is -lt — so it is DUE, and this scenario would have been a duplicate of scenario 5
#    while asserting the opposite. 290,000 sits 15,000 past 275,000, which discriminates properly: the
#    lowest rung (230,000) would be 60,000 past and due, and the highest (740,000) would be negative.
o = run(290000, [230000, 250000, 275000, 740000])
check(not_due(o) and last_seen(o) == 275000, "the nearest rung below is chosen, not the lowest or highest")

shutil.rmtree(work, ignore_errors=True)

# ⛔ THE CONTROL IS CHECKED AGAINST AN EXACT SET, NOT A COUNT.
#
# Three scenarios genuinely depend on the at-or-below rule, so "exactly one failure" is the wrong
# contract here. But counting is not merely imprecise, it is unsound: ANY pre-existing plain-mode failure
# also counts, so a control that removed nothing at all still reports "CONTROL OK". That is not
# hypothetical — it happened on the first run of this file. Scenario 8 was wrong (it asserted "not due"
# at exactly SPACING, where the script correctly says due), and with the guard RESTORED the neutered
# control still saw one failure and exited 0, announcing a working control that was doing nothing.
#
# Naming the expected set catches both shapes: a control that trips nothing, and one that trips a
# different set than the rule actually governs.
EXPECTED_CONTROL_FAILURES = {
    "a bridge below another bridge's rung still archives (the never-fires bug)",
    "a rung above the current height alone does not block the first archive",
    "the nearest rung below is chosen, not the lowest or highest",
}

print()
if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — the at-or-below rule was removed and exactly the {len(got)} assertion(s) "
              "that depend on it failed:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the at-or-below rule did not produce the expected failures.")
    missing = EXPECTED_CONTROL_FAILURES - got
    extra = got - EXPECTED_CONTROL_FAILURES
    if missing:
        print("  these should have failed and did not (the guard is not doing what these tests claim):")
        for f in sorted(missing):
            print(f"    - {f}")
    if extra:
        print("  these failed unexpectedly (a real regression, or a scenario that is simply wrong):")
        for f in sorted(extra):
            print(f"    - {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

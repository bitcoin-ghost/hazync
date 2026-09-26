#!/usr/bin/env python3
"""Every RunPod deploy path asks for SECURE hosts, not ALL (hazync#523).

⛔ WHY. `cloudType: ALL` includes RunPod's community hosts, and those mostly never start: RunPod
accepts the deploy and never publishes a port, so the caller waits out its full SSH timeout having
paid for a pod that never existed. Measured 2026-09-20 across two runs, and the split is by PRICE,
which is the tell:

    $0.34/hr    1 of 6 started   (17%)
    $0.49/hr    1 of 1 started
    $0.74/hr   14 of 14 started  (100%)

A 4-card run died outright: three of five never started, so the run could not reach its minimum.

⛔ AND THIS TEST EXISTS BECAUSE THE FIX ONLY LANDED IN ONE OF TWO PLACES. tip_smoke.py was corrected
with that measurement written beside it; sponsor_bot.py kept `cloudType: ALL` for another six days --
even though tip_smoke.py imports sponsor_bot.IMAGE and sponsor_bot.GPU_TYPES, so the two deploy paths
are siblings by construction. One file was fixed, its sibling was not, and nothing compared them.

⚠ For the BOT the consequence is worse than a slow run. A sponsor has already paid. The sponsorship
sits in `proving` while pods that never boot burn down RUNPOD_REFUSALS_MAX, and the person who paid
watches nothing happen.

  python3 test_pod_cloud_type.py            # must PASS
  python3 test_pod_cloud_type.py --control  # a deploy path put back to ALL; MUST be caught
"""
import os
import pathlib
import re
import sys

CONTROL = "--control" in sys.argv
ROOT = pathlib.Path(os.path.dirname(os.path.abspath(__file__))).parent

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ⛔ Search the WHOLE repo, with no filename filter. The bug was that somebody fixed the file they
# were looking at; a test that checks the two files we happen to know about would have the same blind
# spot as the fix did. Any new deploy path is caught by construction.
#
# ⚠ ANCHOR ON THE CALL, NOT ON THE WORD. A bare search for `cloudType:` also matches this file's own
# docstring and the comments that explain the bug, so the first version of this test failed on its
# own prose -- four "failures", none of them code. Find each podFindAndDeployOnDemand and read the
# cloudType belonging to THAT mutation.
#
# ⚠ And match the CALL SIGNATURE `podFindAndDeployOnDemand(input:`, not the bare name: the bare name
# also appears where the RESPONSE is unwrapped, `data["podFindAndDeployOnDemand"]["id"]`, which is
# not a deploy and has no cloudType to check.
SKIP_DIRS = {".git", "node_modules", "target", "__pycache__", "venv", ".venv"}
CALL = re.compile(r"podFindAndDeployOnDemand\s*\(\s*input\s*:")
CLOUD = re.compile(r"cloudType\s*:\s*([A-Z_]+)")
WINDOW = 400          # characters after the call in which its cloudType must appear

# ⚠ And skip THIS file. It is the scanner, so its prose necessarily quotes both the call signature
# and the bad value; without this it reports itself. That is not the blind spot the whole-repo sweep
# exists to avoid -- this file deploys nothing -- but every other file stays in scope.
SELF = os.path.basename(os.path.abspath(__file__))

paths = []
for p in ROOT.rglob("*"):
    if p.is_dir() or p.suffix not in (".py", ".sh", ".js", ".ts"):
        continue
    if any(part in SKIP_DIRS for part in p.parts) or p.name == SELF:
        continue
    paths.append(p)

found = []
for p in paths:
    try:
        src = p.read_text(errors="ignore")
    except OSError:
        continue
    for m in CALL.finditer(src):
        line = src[:m.start()].count("\n") + 1
        cm = CLOUD.search(src, m.start(), m.start() + WINDOW)
        # ⛔ A deploy with no cloudType at all is NOT a pass: RunPod's own default includes community
        # hosts, so silence here buys exactly the pods that never start.
        val = cm.group(1) if cm else "UNSET"
        # ⚠ The control restores the bug in memory only -- it never writes to the tree.
        if CONTROL and p.name == "sponsor_bot.py":
            val = "ALL"
        found.append((p.relative_to(ROOT), line, val))

# ⛔ A test that finds nothing must FAIL, not pass. If the mutation's shape ever changes -- a
# different key name, a templateId instead -- this file would otherwise go green by checking nothing,
# which is the failure mode it was written to prevent.
check(len(found) >= 2,
      f"found {len(found)} RunPod deploy path(s) to check (expected at least 2)")

for rel, line, val in found:
    check(val == "SECURE", f"{rel}:{line} asks for {val}"
          + ("" if val == "SECURE" else "  ⛔ community hosts mostly never start"))

print()
if CONTROL:
    if any("sponsor_bot.py" in f for f in fails):
        print("CONTROL OK — a deploy path put back to ALL is caught:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — the bug was restored but nothing failed")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print(f"all {len(found)} RunPod deploy paths ask for SECURE hosts")

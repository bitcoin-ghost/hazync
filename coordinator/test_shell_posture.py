#!/usr/bin/env python3
"""`grep -c` counts AND exits non-zero, and the fleet scripts must not fall for it again (hazync#462).

⛔ WHY THIS EXISTS. `grep -c` prints its count on stdout and exits **1** when the count is zero. So:

    P=$(grep -c "segments at po2" prove.log 2>/dev/null || echo 0)

fires the `|| echo 0` *in addition to* grep's own `0`, and `$P` becomes the two-line string "0\\n0".
Every `[ "$P" = "0" ]` test against it is false from then on — the progress read in `probe3.sh` could
never report a card as not-yet-proving. The same shape bites `pgrep -c`.

This is not a style rule. Check 1 runs the broken shape in a real shell and shows the output, so the
hazard is demonstrated rather than asserted, and the ban below has a reason attached.

  python3 test_shell_posture.py            # must PASS
  python3 test_shell_posture.py --control  # a file with the bad shape is added to the scan; MUST FAIL
"""
import os
import re
import subprocess
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── 1. the hazard, demonstrated in a real shell rather than described ────────────────────────────
# ⛔ THE FILE MUST EXIST AND NOT MATCH. This is the scenario that produces the bug, and getting it
# wrong hides it: against a MISSING file grep prints nothing and exits 2, so `|| echo 0` supplies the
# only "0" and the result looks perfectly fine. The hazard is grep printing its own 0 AS WELL.
with tempfile.TemporaryDirectory() as _d:
    _f = os.path.join(_d, "prove.log")
    open(_f, "w").write("this line does not contain the pattern\n")
    bad = subprocess.run(["bash", "-c",
                          f'P=$(grep -c nothing {_f} 2>/dev/null || echo 0); printf "%s" "$P"'],
                         capture_output=True, text=True).stdout
    good = subprocess.run(["bash", "-c",
                           f'P=$(grep -c nothing {_f} 2>/dev/null | head -1); P=${{P:-0}}; '
                           'printf "%s" "$P"'],
                          capture_output=True, text=True).stdout
check(bad == "0\n0",
      f"`|| echo 0` on a match-less grep -c yields TWO lines, so every = \"0\" test on it is "
      f"false (it produced {bad!r})")
check(good == "0",
      f"and `| head -1` with a ${{P:-0}} default yields the single 0 it should ({good!r})")

# ── 2. the shape is absent from every shell script in the repo ───────────────────────────────────
# ⚠ Matched on the SUBSTITUTION, not on `grep -c` alone: a bare `grep -c` whose status nobody reads
# is fine. And on `|| echo`, not `|| <anything>`: the hazard is a SECOND value supplied on top of the
# count grep already printed. `|| true` adds nothing and is correct — run-workers.sh:245 uses it for
# a display count. Banning the wider shape would flag good code and get the rule routed around.
SHAPE = re.compile(r"\$\(\s*(?:[^()]*\|\s*)?p?grep\s+-[a-zA-Z]*c[a-zA-Z]*\b[^()]*\|\|\s*echo")


def shell_files():
    out = subprocess.run(["git", "-C", REPO, "ls-files", "*.sh"],
                         capture_output=True, text=True).stdout.split()
    return [os.path.join(REPO, f) for f in out]


def scan(paths):
    hits = []
    for p in paths:
        try:
            text = open(p, encoding="utf8", errors="replace").read()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue                       # a comment explaining the trap is not the trap
            if SHAPE.search(line):
                hits.append(f"{os.path.relpath(p, REPO)}:{i}")
    return hits


paths = shell_files()
if not paths:
    print("CANNOT TEST: git ls-files returned no shell scripts")
    sys.exit(2)

if CONTROL:
    # ⛔ THE CONTROL IS THE ORIGINAL LINE, verbatim from probe3.sh before the fix.
    ctl = os.path.join(HERE, ".posture_control.sh")
    with open(ctl, "w") as fh:
        fh.write('#!/bin/bash\nP=$(grep -c "segments at po2" $RDIR/prove.log 2>/dev/null || echo 0)\n')
    paths = paths + [ctl]

hits = scan(paths)
check(not hits, f"no script uses the `$(grep -c … || echo)` shape ({len(paths)} scanned; hits={hits})")
if CONTROL:
    os.unlink(os.path.join(HERE, ".posture_control.sh"))

# ── 3. the fleet scripts declare a posture ───────────────────────────────────────────────────────
# ⚠ These are the scripts that drive rented cards: a silent mis-read costs money while it happens.
# `-e` is deliberately NOT required — they report per card and must not abort on one bad pod.
# See tools/milestone/README.md § Shell posture for why `-u` is still partial.
milestone = sorted(p for p in shell_files() if "/tools/milestone/" in p.replace(os.sep, "/"))
missing = [os.path.basename(p) for p in milestone
           if "pipefail" not in open(p, encoding="utf8", errors="replace").read()]
check(bool(milestone) and not missing,
      f"every tools/milestone script sets pipefail ({len(milestone)} scripts; missing={missing})")

EXPECTED_CONTROL_FAILURES = 1

print()
if CONTROL:
    if len(fails) == EXPECTED_CONTROL_FAILURES and any("grep -c" in f for f in fails):
        print("CONTROL OK — a file carrying the original probe3.sh line was added to the scan and "
              "the scan flagged it")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly {EXPECTED_CONTROL_FAILURES} failure naming the shape, "
          f"got {len(fails)}: {fails}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("the `grep -c` trap is demonstrated, banned, and absent")

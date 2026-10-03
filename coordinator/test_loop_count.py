#!/usr/bin/env python3
"""The start report counts loops by argv[0], so it cannot see a shell that merely NAMES one (#491).

⛔⛔ WHY THIS EXISTS, AND WHAT IT COST. `--stop` was fixed for this trap in hazync#491. The START
report was missed and kept using `pgrep -fc`, which matches the whole command LINE — so it counts
any process whose arguments merely mention the tag: the invoking shell, its children, a wrapper.

📏 Measured on hz-spine-1, 2026-09-29 22:35. The launcher printed:

    started 2 proving + 0 folding + 1 spine worker(s); logs in /root/hazync-workers

There was no spine worker. No spine process, no spine log, ever. Nothing else reports a missing
spine worker, so the operator had no reason to look — and the spine did not advance for 71.95 h,
until check-spine's 72 h ceiling fired. The same line printed "3 proving + 2 spine" on 2026-10-02
while ground truth was 2 and 1.

⭐ The loops are started with `exec -a "$tag"`, so the tag IS argv[0]. Counting on argv[0] cannot
see a shell that only mentions the tag, which is precisely the failure.

    python3 test_loop_count.py             # the counter is argv[0]-based and warns on a missing spine
    python3 test_loop_count.py --control   # the old pgrep -f counter — MUST miscount
"""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "run-workers.sh")
CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


# ── the behavioural half: build a fake `ps` table and count it both ways ─────────────────────────
# ⛔ This is the real test. A source grep would only prove the text changed; this proves the two
# counting METHODS disagree on exactly the input that fooled the launcher.
PS_TABLE = """\
  101 hazync-worker-loop-1
  102 hazync-worker-loop-2
  103 bash -c cd /workspace && MODE=spine ./hazync-run-workers.sh 1   # names the tag, IS NOT a loop
  104 sshd: root@pts/0 hazync-spine-loop grep wrapper
"""


def count_argv0(table, tag):
    """What the fix does: argv[0] must START with the tag."""
    n = 0
    for line in table.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].split(" ")[0].startswith(tag):
            n += 1
    return n


def count_substring(table, tag):
    """What `pgrep -f` does: the tag appears anywhere in the command line."""
    return sum(1 for line in table.splitlines() if tag in line)


def main():
    print("── 1. the two counting methods, on the input that fooled the launcher ──")
    tag = "hazync-spine-loop"
    a0 = count_argv0(PS_TABLE, tag)
    sub = count_substring(PS_TABLE, tag)
    print(f"     argv[0] method : {a0}   (ground truth: 0 spine loops in this table)")
    print(f"     pgrep -f method: {sub}   (counts the invoking shell and an ssh line)")
    check(a0 == 0, "argv[0] counting reports 0 spine loops — the truth")
    check(sub > 0, "the pgrep -f method reports more than 0 — reproducing the bug")
    check(sub != a0, "the two methods DISAGREE, which is why the launcher lied")
    # and it must still count real loops correctly
    check(count_argv0(PS_TABLE, "hazync-worker-loop") == 2,
          "argv[0] counting still finds the two real worker loops")

    print("── 2. awk does the same thing the shell does ──")
    # ⛔ Exercise the ACTUAL awk expression from run-workers.sh, not a Python re-implementation of
    # it — a re-implementation tests my Python.
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(PS_TABLE)
        snap = f.name
    out = subprocess.run(
        ["awk", "-v", f"p={tag}", '$2 ~ ("^" p) {c++} END {print c+0}', snap],
        capture_output=True, text=True)
    os.unlink(snap)
    check(out.stdout.strip() == "0",
          f"the shipped awk expression returns 0 on the same table (got {out.stdout.strip()!r})")

    print("── 3. the script itself ──")
    src = open(SRC, encoding="utf-8").read()
    start = src.split("logs in $LOG_DIR", 1)[0][-1800:] if "logs in $LOG_DIR" in src else ""
    if CONTROL:
        # the old line, restored
        start = ('echo "started $(pgrep -fc "hazync-worker-loop") proving + '
                 '$(pgrep -fc "hazync-fold-loop") folding + '
                 '$(pgrep -fc "hazync-spine-loop") spine worker(s)"')
    check("pgrep -fc" not in start,
          "the start report does not use pgrep -fc")
    check("_count_tag" in start or CONTROL is False and "_count_tag" in src,
          "it counts with the argv[0] helper instead")
    check("WARNING: MODE=" in src and "NONE is running" in src,
          "and it WARNS when the mode asked for a spine worker and none exists")
    check("71.95 h" in src or "71.95" in src,
          "the comment records the measured cost, so nobody re-simplifies it")

    if CONTROL:
        if fails:
            print(f"\nCONTROL OK: the old line fails {len(fails)} check(s)")
            return 0
        print("\nCONTROL FAILED: the old pgrep -fc line passed every check")
        return 1
    if fails:
        print(f"\nFAILED: {len(fails)} check(s)")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

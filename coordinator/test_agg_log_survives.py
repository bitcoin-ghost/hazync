#!/usr/bin/env python3
"""Every block's aggregate log survives the next block, and harvest fetches it (hazync#526).

⛔ WHY. `seg-serve` is restarted for each block, and the launch command began `rm -f agg.log agg.err`.
So the only block whose segment count, join round-trips and timings still existed at teardown was the
LAST one. Measured on the board-fill trial (2026-09-24): four blocks proved, segment totals available
for exactly one, and the writeup had to say so --

    "⚠ Segment counts are only available for 968,340. `agg.log` is cleared at the start of each
     block, so the earlier blocks' totals were overwritten before harvest. That is a gap in the
     evidence, not a rounding choice."

A run whose purpose is a measurement cannot destroy three quarters of it on the way.

⛔ AND APPENDING IS NOT ENOUGH ON ITS OWN. tip_harvest fetches a FIXED list of filenames one by one,
so rotating to agg.log.<timestamp> would write the evidence faithfully and then never fetch it --
the same loss with extra steps. The cumulative name has to be in AGG_LOGS, and this test checks both
halves, because either alone is useless.

  python3 test_agg_log_survives.py            # must PASS
  python3 test_agg_log_survives.py --control  # `rm -f` restored; MUST lose the earlier block
"""
import os
import re
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


# ── 1. behaviour: run the real shell across three blocks ───────────────────────────────────────
sys.path.insert(0, HERE)
import tip_runner                                                           # noqa: E402

rotate = tip_runner.TipRunner._rotate_agg if hasattr(tip_runner, "TipRunner") else None
if rotate is None:
    # The class name is not the point of this test; find whatever defines _rotate_agg.
    for name in dir(tip_runner):
        obj = getattr(tip_runner, name)
        if isinstance(obj, type) and hasattr(obj, "_rotate_agg"):
            rotate = obj._rotate_agg
            break

check(rotate is not None, "the runner has a rotate step at all")

if rotate is not None:
    with tempfile.TemporaryDirectory() as d:
        def start_block(label, previous_log):
            cmd = rotate(None, label)
            if CONTROL:
                cmd = "rm -f agg.log agg.err"          # ⛔ the bug, restored
            subprocess.run(["bash", "-c", cmd], cwd=d, check=False)
            if previous_log is not None:
                with open(os.path.join(d, "agg.log"), "w") as f:
                    f.write(previous_log)

        # ⚠ The FIRST block has no previous log. A rotate that fails on a missing file would take
        # down the launch command it is chained to with &&, so this case matters more than it looks.
        start_block("height 968339", "TOTAL 4897 segments\nexecution 783.1 s\n")
        start_block("height 968340", "TOTAL 9600 segments\nexecution 1910.0 s\n")
        start_block("height 968341", None)

        hist = os.path.join(d, "agg-history.log")
        text = open(hist).read() if os.path.exists(hist) else ""

        check("4897 segments" in text,
              "⛔ the FIRST block's segment count survived two later blocks")
        check("9600 segments" in text,
              "and so did the second's")
        check(not os.path.exists(os.path.join(d, "agg.log")),
              "agg.log itself is cleared, so the new block starts from empty")
        seps = len(re.findall(r"===== rotated .* before height \d+ =====", text))
        check(seps >= 2, f"each rotation is separated and labelled ({seps} separators)")


# ── 2. ⛔ and harvest must ASK for it, or none of the above leaves the pod ──────────────────────
harvest = open(os.path.join(HERE, "tip_harvest.py")).read()
check('"agg-history.log"' in harvest,
      "⛔ tip_harvest fetches agg-history.log (a file nobody fetches is a file nobody has)")

runner_src = open(os.path.join(HERE, "tip_runner.py")).read()
check(len(re.findall(r"self\._rotate_agg\(", runner_src)) >= 2,
      "both aggregate launch paths rotate, not just one")
check(not re.search(r'&& rm -f agg\.log agg\.err && ', runner_src),
      "⛔ no launch path still begins by deleting the log")

EXPECTED_CONTROL = {
    "the FIRST block's segment count survived",
    "and so did the second's",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — with `rm -f` restored, every block but the last loses its evidence:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {sorted(EXPECTED_CONTROL)}, got {sorted(hit)}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("every block's aggregate log survives the run, and harvest fetches it")

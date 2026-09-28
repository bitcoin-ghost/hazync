#!/usr/bin/env python3
"""The next tip block is the LOWEST unproved one, not the highest (hazync#556).

⛔ WHAT THIS EXISTS FOR. `highest_tip_bundle` was the selector. On 2026-09-28 blocks 968,984 and
968,985 were mined 5 seconds apart, so both bundles appeared at 13:24:59, the session took 985, and
984 was never proved and never can be — `--fresh-tip` puts the floor above it on every later run.

    968,983  appeared 12:11:21Z  accepted 12:17:12Z  blocks_behind: 0
    968,985  appeared 12:24:59Z  accepted 12:26:41Z  blocks_behind: 0
             ^ nothing between them, in any artifact the run produced

⚠ The shell filter is tested against a REAL directory, not a mock. Mocking the runner only proves
that whatever the fake returns comes back; the part that decides which filenames count never runs —
and that filter is where `bundle_<h>.json.tmp` (the file the bridge is mid-write) has bitten before.

    python3 test_tip_sequential.py             # sequence is kept, and a jump names what it skips
    python3 test_tip_sequential.py --control   # highest-wins — the skip must reproduce
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_board as B  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def choose(heights, max_behind):
    """The selection, as the session makes it."""
    hs = sorted(heights)
    if CONTROL:
        return (hs[-1] if hs else None), []      # the old behaviour: the highest bundle wins
    return B.choose_tip(hs, max_behind)


# ── 1. the shell filter, run for real ────────────────────────────────────────────────────────────
if not CONTROL:
    with tempfile.TemporaryDirectory() as d:
        for n in ("bundle_968983.json", "bundle_968984.json", "bundle_968985.json",
                  "bundle_968986.json.tmp",          # ⛔ mid-write: must NOT count
                  "state.bin", "state.head", "bundle_.json", "notabundle_968999.json"):
            open(os.path.join(d, n), "w").close()
        os.makedirs(os.path.join(d, "undo"), exist_ok=True)

        out = subprocess.run(["bash", "-lc", B.bundles_above_cmd(d, 968983)],
                             capture_output=True)
        got = sorted(int(x) for x in out.stdout.decode().split() if x.isdigit())
        check(got == [968984, 968985],
              f"above 968,983 the real pipeline returns {got} — ascending, .tmp excluded")

        out = subprocess.run(["bash", "-lc", B.bundles_above_cmd(d, 968985)], capture_output=True)
        check(out.stdout.decode().split() == [],
              "above the newest complete bundle it returns nothing")

        out = subprocess.run(["bash", "-lc", B.bundles_above_cmd(d, 0)], capture_output=True)
        got = sorted(int(x) for x in out.stdout.decode().split() if x.isdigit())
        check(got == [968983, 968984, 968985],
              f"from a zero floor it returns every COMPLETE bundle: {got}")
        check(968986 not in got,
              "⛔ the bundle the bridge is still writing is never offered as work")

    # an unreachable bridge is [] — the same as "nothing waiting", so a blip never aborts a block
    check(B.tip_bundles_above("h", 0, runner=lambda c: type("R", (), {"returncode": 255,
                                                                     "stdout": b""})()) == [],
          "an ssh failure reads as 'nothing waiting', never as an error that stops the session")
    check(B.next_tip_bundle("h", 0, runner=lambda c: type("R", (), {
        "returncode": 0, "stdout": b"968985\n968984\n968983\n"})()) == 968983,
          "next_tip_bundle returns the LOWEST even when the listing arrives unsorted")

# ── 2. the 2026-09-28 case, exactly: two bundles land together ───────────────────────────────────
chosen, skipped = choose([968984, 968985], max_behind=3)
if CONTROL:
    check(chosen == 968985,
          "control reproduces it: 985 is chosen and 984 is skipped with nothing recorded")
    check(skipped == [], "control records NO skip — which is why the gap was invisible")
else:
    check(chosen == 968984, f"the LOWER of two simultaneous bundles is proved first (got {chosen})")
    check(skipped == [], "and nothing is skipped")

# ── 3. sequence is kept over a queue ─────────────────────────────────────────────────────────────
chosen, skipped = choose([101, 102, 103], max_behind=3)
if CONTROL:
    check(chosen == 103, "control: the newest wins however many are waiting")
else:
    check(chosen == 101 and skipped == [], "a queue within the bound is proved in order")

# ── 4. past the bound it jumps — and SAYS what that abandons ─────────────────────────────────────
chosen, skipped = choose([101, 102, 103, 104], max_behind=3)
if CONTROL:
    check(chosen == 104 and skipped == [],
          "control: it jumps and still records nothing, so the hole is silent")
else:
    check(chosen == 104, f"past --tip-max-behind it jumps to the newest (got {chosen})")
    check(skipped == [101, 102, 103],
          f"and names every height that abandons: {skipped}")
    # ⚠ This is the property that makes a gap auditable rather than merely absent.
    check(set(skipped) | {chosen} == {101, 102, 103, 104},
          "every pending height is either proved or explicitly recorded as skipped")

# ── 5. never-jump, and the empty case ────────────────────────────────────────────────────────────
if not CONTROL:
    chosen, skipped = B.choose_tip([101, 102, 103, 104, 105], 0)
    check(chosen == 101 and skipped == [],
          "--tip-max-behind 0 stays sequential no matter how far behind")
    check(B.choose_tip([], 3) == (None, []), "nothing pending is (None, [])")
    check(B.choose_tip(None, 3) == (None, []), "and None is handled like nothing pending")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

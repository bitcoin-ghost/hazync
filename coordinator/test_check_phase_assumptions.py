#!/usr/bin/env python3
"""Two integrity checks were calibrated for a phase the system has left (hazync#515).

Both fired continuously, on a healthy system, for a whole day — 75 alerts from one and 7 from the
other — and both were right about the raw fact and wrong about what it meant.

⛔ 1. check-spine: "the spine has not advanced for 14.4 h ... INTEGRITY FAILURE"

The genesis proof was perfect: 7 of its 8 checks passed, including that its cumulative work equals
the node's chainwork. The operator had deliberately set the fleet to fold-only, because
`extend-spine` costs THE SAME whatever the chunk width — so folding first and absorbing later buys
far more per GPU step. Measured 2026-09-24 on one A40 fleet:

    into the collapsed backlog   3,320 blocks / 283 s = 11.7 blocks/s   (chunks up to 1,024)
    near the fold frontier         288 blocks / 840 s =  0.34 blocks/s  (chunks of 2 and 4)

A 34x difference, same fleet, minutes apart. "Behind" is a deliberate, cheaper state. What the check
must separate is "behind while folds keep arriving" from "behind while NOTHING is arriving" — and
those are indistinguishable if you only ask whether the spine moved.

⛔ 2. hazync-archive-checkpoint: "no 'checkpoint @ N' line in the last 200 journal entries"

`checkpoint @ N` is printed on the WALK path, every 2,000 blocks. At the tip the chain makes ~6
blocks an hour, so that line appears about once a FORTNIGHT, while 200 entries is ~17 hours. The
check could not pass at the tip, ever. ⚠ And the checkpoint itself was never late: main.rs:4343
saves state on every catch-up cycle. Only the log line was rare.

  python3 test_check_phase_assumptions.py            # must PASS
  python3 test_check_phase_assumptions.py --control  # old rules restored; MUST show as the gap
"""
import importlib.machinery
import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── part 1: the spine stall verdict ─────────────────────────────────────────────────────────────
_loader = importlib.machinery.SourceFileLoader("check_spine", os.path.join(HERE, "check-spine.py"))
_spec = importlib.util.spec_from_loader("check_spine", _loader)
cs = importlib.util.module_from_spec(_spec)
_loader.exec_module(cs)


def verdict(spine_age_h, fold_age_h, *, waiting=True, behind_max_h=72, stall_h=2, fold_idle_h=2):
    """Reproduce the stall branch exactly, on a real sqlite db."""
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "c.db")
        c = sqlite3.connect(db)
        c.execute("CREATE TABLE vranges (id TEXT, lo INTEGER, hi INTEGER, ts REAL)")
        if waiting:
            c.execute("INSERT INTO vranges VALUES ('117417', 117417, 117417, ?)",
                      (time.time() - fold_age_h * 3600,))
        c.commit(); c.close()

        rep = cs.Report() if hasattr(cs, "Report") else None
        age = spine_age_h * 3600
        hi = 117416
        # the branch under test, lifted verbatim in shape from check-spine.py
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        w = con.execute("SELECT id FROM vranges WHERE lo = ? LIMIT 1", (hi + 1,)).fetchone()
        row = con.execute("SELECT MAX(ts) FROM vranges").fetchone()
        con.close()
        fold_age = (time.time() - float(row[0])) if (row and row[0]) else None

        if not w:
            return "ok-nothing-waiting"
        if CONTROL:
            # ⛔ THE OLD RULE: a waiting proof plus a stale spine is always an INTEGRITY FAILURE.
            return "fail"
        if fold_age is not None and fold_age <= fold_idle_h * 3600 and age <= behind_max_h * 3600:
            return "ok-folding-live"
        if fold_age is not None and fold_age <= fold_idle_h * 3600:
            return "fail-ceiling"
        return "fail"


# the situation that fired 75 alerts: 14.4 h behind, folds arriving every minute
check(verdict(14.4, 0.02) == "ok-folding-live",
      f"14.4 h behind with folding LIVE reads as deliberate, not an integrity failure "
      f"(got {verdict(14.4, 0.02)})")

# ⛔ the failure the check exists for: behind AND nothing feeding it
check(verdict(14.4, 30.0) == "fail",
      f"14.4 h behind with the newest fold 30 h old still FAILS — nothing is feeding it "
      f"(got {verdict(14.4, 30.0)})")

# ⚠ and "folding is live" must not excuse an unbounded backlog
check(verdict(100.0, 0.02) == "fail-ceiling",
      f"100 h behind fails even with folding live — past the 72 h ceiling "
      f"(got {verdict(100.0, 0.02)})")

# nothing waiting is still fine, folding or not
check(verdict(14.4, 0.02, waiting=False) == "ok-nothing-waiting",
      "a stale spine with nothing proven above it is not a fault")


# ⛔ THE BRANCH ABOVE IS A MODEL, AND A MODEL DRIFTS. check-spine's stall logic lives inside main()
# behind a network fetch and a bitcoin-cli call, so it cannot be invoked directly here. That is
# exactly how test_fresh_tip came to assert obsolete behaviour and pass. Pin the model to the real
# source: if someone removes the fold-liveness branch, this fails.
_src = open(os.path.join(HERE, "check-spine.py")).read()
check("SELECT MAX(ts) FROM vranges" in _src,
      "check-spine actually asks when the newest fold arrived")
check("a.fold_idle_secs" in _src and "a.behind_max_secs" in _src,
      "and uses both the fold-idle window and the hard ceiling")
check("--fold-idle-secs" in _src and "--behind-max-secs" in _src,
      "and they are real options, not test fictions")
check("but folding is LIVE" in _src,
      "and the passing verdict says WHY it is not a fault, so the next reader does not re-file it")

# ── part 2: the checkpoint height source ────────────────────────────────────────────────────────
SH = os.path.join(HERE, "deploy", "hazync-archive-checkpoint.sh")

with tempfile.TemporaryDirectory() as d:
    bridge, arch = os.path.join(d, "b"), os.path.join(d, "a")
    os.makedirs(bridge); os.makedirs(arch)
    open(os.path.join(bridge, "state.bin"), "w").write("x" * 1024)
    # the live shape: the sidecar is current, the journal's checkpoint line is a fortnight stale
    open(os.path.join(bridge, "state.head"), "w").write(
        "968376 00000000000000000000ed6b1ae619d84187464b644d580e519028737c140822\n")
    open(os.path.join(arch, "state_965457.bin"), "w").write("x")

    env = dict(os.environ, DRY="1", HAZYNC_BRIDGE_OUT=bridge, HAZYNC_CKPT_ARCHIVE=arch,
               SPACING="25000", BRIDGE_UNIT="a-unit-that-does-not-exist", MIN_FREE_GB="0")
    if CONTROL:
        # ⛔ THE OLD RULE: the journal is the only source. With no journal, no height.
        env["HAZYNC_CKPT_IGNORE_SIDECAR"] = "1"
        src = open(SH).read().replace(
            'H=$(awk \'NR==1 && $1 ~ /^[0-9]+$/ {print $1}\' "$DIR/state.head" 2>/dev/null)',
            'H=""')
        SH_RUN = os.path.join(d, "old.sh")
        open(SH_RUN, "w").write(src)
        os.chmod(SH_RUN, 0o755)
    else:
        SH_RUN = SH

    r = subprocess.run(["bash", SH_RUN], env=env, capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip()
    check(r.returncode == 0,
          f"at the tip, with no usable journal line, the check still runs (exit {r.returncode})")
    check("968376" in out,
          f"and gets the height from state.head ({out.splitlines()[0][:80] if out else 'no output'})")
    check("not due" in out,
          f"and correctly reports no rung is due (968,376 - 965,457 = 2,919 < 25,000)")

EXPECTED_CONTROL = {
    "14.4 h behind with folding LIVE reads as deliberate",
    # ⚠ The old rule returns a bare "fail" for every stale spine, so the CEILING distinction does not
    # exist under it either. Both verdicts alert, so this one is a technicality rather than a second
    # demonstration of the bug -- but it does differ, and a control that quietly tolerated a
    # difference it did not predict would be worth nothing.
    "100 h behind fails even with folding live",
    "at the tip, with no usable journal line, the check still runs",
    "and gets the height from state.head",
    "and correctly reports no rung is due",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the old rules restored, a healthy system alerts on both checks:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("both checks now describe the phase the system is actually in")

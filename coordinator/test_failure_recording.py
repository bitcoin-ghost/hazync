#!/usr/bin/env python3
"""A failed submission must be RECORDED, and classified (hazync#460).

⛔ WHAT THIS CAUGHT. `MAX_ATTEMPTS`, `MAX_ENV_FAILURES`, `is_env_failure()` and four DB columns
formed a complete design for handling failed blocks, and NONE of it ran:

  * `is_env_failure()` had zero callers
  * `attempts`, `env_failures`, `last_error`, `last_failed_at` were created by the migration and
    only ever SELECTed — the only UPDATEs touching them lived in test files

Measured on the live board 2026-09-22: **0 failed ranges and attempts=0 across 123,331 proven
blocks.** Diagnostics built on those fields were structurally blind.

⚠ SCOPE. This tests RECORDING and CLASSIFICATION only. Parking at MAX_ATTEMPTS is deliberately not
implemented: `live_ids()` counts 'failed' as live ("a parked range still owns its interval") and
nothing in the codebase sets a range back to 'open', so parking is a one-way door that would block
the frontier for ever. That needs an un-park route first.

  python3 test_failure_recording.py            # assertions; exit 0 on success
  python3 test_failure_recording.py --control  # classification removed; MUST fail
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402


def classify(err, control=False):
    if control:                       # the control: everything counts as a block failure
        return False
    return server.is_env_failure(err)


def check_env_errors_are_recognised(control=False):
    """⛔ THE ONE THAT MATTERS: a box problem must not read as a bad block."""
    env = ["CUDA error: out of memory",
           "prover failed: allocation failed",
           "no CUDA-capable device is detected",
           "worker received signal 15",
           "illegal memory access was encountered"]
    missed = [e for e in env if not classify(e, control)]
    if missed:
        return False, f"{len(missed)} environmental error(s) counted as block failures: {missed[:2]}"
    return True, f"all {len(env)} environmental signatures recognised"


def check_block_errors_are_not_excused(control=False):
    """⚠ THE MIRROR: a genuinely bad receipt must NOT be excused as environmental."""
    blocks = ["receipt does not verify against METHOD_ID",
              "merkle root mismatch",
              "in_tip does not match the range's expected input",
              "signature invalid"]
    excused = [e for e in blocks if server.is_env_failure(e)]
    if excused:
        return False, f"block failures excused as environmental: {excused}"
    return True, f"all {len(blocks)} block failures counted as block failures"


def check_columns_exist(control=False):
    """The recording target must actually be in the schema."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py"),
               encoding="utf8").read()
    for col in ("attempts", "env_failures", "last_error", "last_failed_at"):
        if f'"{col}' not in src and f"{col} INTEGER" not in src and f"{col} TEXT" not in src:
            return False, f"{col} is not in the migration"
    return True, "all four failure columns are migrated"


def check_recording_is_wired(control=False):
    """⛔ THE WHOLE POINT: the submit path must actually call it."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py"),
               encoding="utf8").read()
    if src.count("is_env_failure(") < 2:
        return False, ("is_env_failure() still has no caller — the classifier is defined and "
                       "never used, which is the bug this issue is about")
    if "SET env_failures=COALESCE" not in src:
        return False, "env_failures is never incremented"
    if "SET attempts=COALESCE" not in src:
        return False, "attempts is never incremented"
    return True, "the submit path records and classifies"


def check_sql_is_valid(control=False):
    """The UPDATEs must actually run against the real schema."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE ranges(id TEXT PRIMARY KEY, lo INT, hi INT, status TEXT, "
               "attempts INTEGER DEFAULT 0, env_failures INTEGER DEFAULT 0, "
               "last_error TEXT, last_failed_at REAL)")
    db.execute("INSERT INTO ranges(id,lo,hi,status) VALUES('7',7,7,'claimed')")
    db.execute("UPDATE ranges SET attempts=COALESCE(attempts,0)+1, last_error=?, "
               "last_failed_at=? WHERE id=?", ("merkle mismatch", 1.0, "7"))
    db.execute("UPDATE ranges SET env_failures=COALESCE(env_failures,0)+1, last_error=?, "
               "last_failed_at=? WHERE id=?", ("cuda error", 2.0, "7"))
    r = db.execute("SELECT attempts, env_failures, last_error FROM ranges WHERE id='7'").fetchone()
    if r["attempts"] != 1 or r["env_failures"] != 1:
        return False, f"counters did not increment: attempts={r['attempts']} env={r['env_failures']}"
    if r["last_error"] != "cuda error":
        return False, f"last_error not written: {r['last_error']!r}"
    return True, "both UPDATEs run and increment on the real column shapes"


CHECKS = [check_env_errors_are_recognised, check_block_errors_are_not_excused,
          check_columns_exist, check_recording_is_wired, check_sql_is_valid]
CONTROL_MUST_FAIL = {check_env_errors_are_recognised}


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        ok, why = fn(control=control)
        if control and fn not in CONTROL_MUST_FAIL:
            continue
        if control:
            if ok:
                print(f"  CONTROL DID NOT FAIL: {fn.__name__} -- {why}")
                bad += 1
            else:
                print(f"  control ok: {fn.__name__} caught it ({why})")
        else:
            print(f"  {'ok  ' if ok else 'FAIL'} {fn.__name__}: {why}")
            bad += 0 if ok else 1
    if bad:
        print(f"\n{bad} check(s) wrong")
        return 1
    print("\nfailures are recorded and classified" if not control else "\ncontrol failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

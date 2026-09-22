#!/usr/bin/env python3
"""A block that cannot be proved gets parked — and can be brought back (hazync#460, second half).

⛔ WHY THIS EXISTS. #470 made failures get RECORDED but deliberately refused to park, because parking
was a one-way door: `overlapping()` counts 'failed' as live, and nothing ever set a range back to
'open'. The two halves belong together and both can regress silently, so both are pinned here:

  * MAX_ATTEMPTS worth of BLOCK-implicating failures must park the range
  * an ENVIRONMENTAL failure must never park it, no matter how many there are — the whole reasoning
    of `is_env_failure` is that an OOM on an oversubscribed GPU says nothing about the block
  * the un-park route must actually un-park, resetting the counter that parked it
  * it must refuse the two ways it could do damage: no reason, or a range that is not parked

  python3 test_parking.py            # must PASS
  python3 test_parking.py --control  # the classification is thrown away; MUST FAIL
"""
import os
import sqlite3
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "deploy"))

import importlib.util                                                            # noqa: E402
from importlib.machinery import SourceFileLoader                                 # noqa: E402

import server                                                                    # noqa: E402

_ld = SourceFileLoader("unpark", os.path.join(HERE, "deploy", "hazync-unpark.py"))
_spec = importlib.util.spec_from_loader("unpark", _ld)
unpark_mod = importlib.util.module_from_spec(_spec)
_ld.exec_module(unpark_mod)

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def fresh_db():
    d = tempfile.mkdtemp(prefix="park_")
    path = os.path.join(d, "c.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE ranges(id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, status TEXT, "
              "attempts INTEGER DEFAULT 0, env_failures INTEGER DEFAULT 0, "
              "last_error TEXT, last_failed_at REAL)")
    c.execute("INSERT INTO ranges VALUES('29664', 29664, 29664, 'open', 0, 0, NULL, NULL)")
    c.commit()
    return path, c


# ⛔ THE CONTROL MUTATES THE SHIPPED RULE, NOT A COPY OF IT. `record_submission_failure` is called
# here exactly as the submit handler calls it, and the control removes the environmental/block
# distinction from `server.is_env_failure` itself — so what fails below is the real code path.
if CONTROL:
    server.is_env_failure = lambda _e: False


def record_failure(c, rid, err):
    """One failed submission, through the function the coordinator itself calls."""
    server.record_submission_failure(c, rid, err)
    c.commit()


def status(c, rid="29664"):
    return c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone()


# ── 1. block-implicating failures park, at MAX_ATTEMPTS and not before ───────────────────────────
path, c = fresh_db()
for i in range(server.MAX_ATTEMPTS - 1):
    record_failure(c, "29664", "receipt does not verify against the claimed range")
check(status(c)["status"] == "open",
      f"{server.MAX_ATTEMPTS - 1} failures is not yet a park (status={status(c)['status']})")
record_failure(c, "29664", "receipt does not verify against the claimed range")
check(status(c)["status"] == "failed",
      f"the {server.MAX_ATTEMPTS}th block-implicating failure PARKS it (status={status(c)['status']})")

# ── 2. ⛔ THE CENTRAL CASE. Environmental failures must NEVER park, however many ──────────────────
path2, c2 = fresh_db()
OOM = "prove failed: CUDA error: out of memory"
check(CONTROL or server.is_env_failure(OOM),
      "the shipped classifier calls an OOM environmental (sanity)")
for _ in range(server.MAX_ENV_FAILURES + 5):
    record_failure(c2, "29664", OOM)
r = status(c2)
check(r["status"] == "open",
      f"{server.MAX_ENV_FAILURES + 5} OOMs did NOT park the block — the fleet is the suspect, not "
      f"the block (status={r['status']})")
check((r["attempts"] or 0) == 0,
      f"and none of them counted toward MAX_ATTEMPTS (attempts={r['attempts']})")
check((r["env_failures"] or 0) == server.MAX_ENV_FAILURES + 5,
      f"they were all counted as environmental (env_failures={r['env_failures']})")

# ── 3. a late submission must not drag a VERIFIED range back ─────────────────────────────────────
path3, c3 = fresh_db()
c3.execute("UPDATE ranges SET status='verified', attempts=?", (server.MAX_ATTEMPTS - 1,))
c3.commit()
record_failure(c3, "29664", "receipt does not verify against the claimed range")
check(status(c3)["status"] == "verified",
      f"a failure arriving after the range verified leaves it verified ({status(c3)['status']})")

# ── 4. the un-park route ─────────────────────────────────────────────────────────────────────────
uc = unpark_mod.connect(path)
check(len(unpark_mod.parked(uc)) == 1, "the parked range is listed")
moved = unpark_mod.unpark(uc, ["29664"], "the 4-worker OOM incident, not the block")
r = status(c)
check(moved == ["29664"] and r["status"] == "open",
      f"un-parking puts it back on the board (moved={moved}, status={r['status']})")
# ⛔ THE COUNTER MUST RESET or the next single failure re-parks it — an un-park that does not un-park.
check((r["attempts"] or 0) == 0, f"and resets attempts (attempts={r['attempts']})")
check("the 4-worker OOM incident" in (r["last_error"] or ""),
      "and records WHY, where the next person will read it")
record_failure(c, "29664", "receipt does not verify against the claimed range")
check(status(c)["status"] == "open",
      f"one failure after an un-park does not immediately re-park ({status(c)['status']})")

# ── 5. the two ways it could do damage ───────────────────────────────────────────────────────────
rc = unpark_mod.main(["--db", path, "--unpark", "29664"])            # no --reason
check(rc == 2 and status(c)["status"] == "open",
      f"un-parking without a --reason is refused (rc={rc})")

path4, c4 = fresh_db()
c4.execute("UPDATE ranges SET status='verified'")
c4.commit()
uc4 = unpark_mod.connect(path4)
moved4 = unpark_mod.unpark(uc4, ["29664"], "typo")
check(moved4 == [] and status(c4)["status"] == "verified",
      f"a VERIFIED range is never rewritten to open by a mistyped id (moved={moved4})")

EXPECTED_CONTROL = {
    "and none of them counted toward MAX_ATTEMPTS",
    "they were all counted as environmental",
    "did NOT park the block",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — with the environmental/block distinction removed, a capacity incident "
              "parked a perfectly good block:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected the env-classification assertions to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("bad blocks park, capacity incidents do not, and a park can be undone")

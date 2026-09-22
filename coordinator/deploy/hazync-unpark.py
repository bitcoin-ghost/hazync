#!/usr/bin/env python3
"""Put a parked range back on the board (hazync#460).

⛔ WHY THIS EXISTS, AND WHY IT CAME FIRST. `MAX_ATTEMPTS` parks a range by setting `status='failed'`,
and `live_ids()`/`overlapping()` count 'failed' as LIVE -- a parked range keeps its interval so a
wider range cannot be claimed straight over the block that is failing. That is correct, and it makes
parking a ONE-WAY DOOR: nothing in `server.py` ever sets a range back to 'open', so a range parked in
error would have pinned the frontier for ever. #470 recorded failures but refused to park for exactly
this reason. This script is the recovery route, and parking was only switched on once it existed.

    hazync-unpark.py                            # list what is parked, and why
    hazync-unpark.py --unpark 29664 --reason "OOM during the 4-worker incident, not the block"
    hazync-unpark.py --unpark-all --reason "..."   # after fixing a fleet-wide cause

⚠ A REASON IS REQUIRED. Un-parking discards the evidence that put the range there; if nobody can say
why, the honest answer is to leave it parked and go and look. The reason is written to `last_error`
so the next person reads it rather than an error that no longer applies.

⚠ RUN IT ON THE COORDINATOR BOX, AGAINST THE LIVE DB, WITH THE SERVICE RUNNING. The coordinator uses
WAL, so a short write from a second process is safe. It is NOT safe to copy the DB, edit the copy and
put it back -- that silently discards every range proved while you were editing.
"""
import argparse
import os
import sqlite3
import sys
import time

DB = os.environ.get("COORD_DB", "/opt/hazync/coordinator/coordinator.db")


def connect(path):
    c = sqlite3.connect(path, timeout=30)
    c.row_factory = sqlite3.Row
    # ⚠ The coordinator is live. WAL lets this write without blocking its readers; a busy_timeout
    # means a concurrent checkpoint is waited out rather than reported as a failure.
    c.execute("PRAGMA busy_timeout=30000")
    return c


def parked(c):
    return list(c.execute(
        "SELECT id, lo, hi, attempts, env_failures, last_error, last_failed_at "
        "FROM ranges WHERE status='failed' ORDER BY lo"))


def show(rows):
    if not rows:
        print("nothing is parked.")
        return
    print(f"{len(rows)} parked range(s):\n")
    now = time.time()
    for r in rows:
        age = now - (r["last_failed_at"] or now)
        print(f"  {r['id']:>12}  blocks {r['lo']}-{r['hi']}  "
              f"attempts={r['attempts'] or 0} env_failures={r['env_failures'] or 0}  "
              f"last failed {age / 3600:.1f} h ago")
        print(f"                {(r['last_error'] or '(no error recorded)')[:160]}")
    print("\nun-park with:  hazync-unpark.py --unpark <id> --reason '<why this block is not the problem>'")


def unpark(c, ids, reason):
    """Return the ids actually moved. Resets BOTH counters, deliberately."""
    moved = []
    stamp = f"un-parked {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: {reason}"
    for rid in ids:
        # ⛔ SCOPED TO status='failed'. Without it a typo'd id would silently rewrite a VERIFIED range
        # to open, throwing away a proof that is on disk and re-offering a block already done.
        # ⚠ BOTH COUNTERS RESET. Leaving `attempts` at MAX_ATTEMPTS would re-park the range on its
        # very next failure, which is an un-park that does not un-park -- the inert-subsystem shape
        # this whole issue is about. The failure history is not lost: it goes into last_error.
        cur = c.execute("UPDATE ranges SET status='open', attempts=0, env_failures=0, "
                        "last_error=?, last_failed_at=NULL WHERE id=? AND status='failed'",
                        (stamp[:500], str(rid)))
        if cur.rowcount:
            moved.append(str(rid))
    c.commit()
    return moved


def main(argv=None):
    ap = argparse.ArgumentParser(description="put a parked range back on the board")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--unpark", action="append", default=[], metavar="ID")
    ap.add_argument("--unpark-all", action="store_true")
    ap.add_argument("--reason", default="")
    a = ap.parse_args(argv)

    if not os.path.exists(a.db):
        print(f"no such database: {a.db}  (set COORD_DB, or --db)", file=sys.stderr)
        return 2
    c = connect(a.db)
    rows = parked(c)

    if not a.unpark and not a.unpark_all:
        show(rows)
        return 0

    if not a.reason.strip():
        print("refusing: --reason is required. Un-parking discards the evidence that put the range\n"
              "there, so say what makes this block innocent. If you cannot, leave it parked.",
              file=sys.stderr)
        return 2

    ids = [r["id"] for r in rows] if a.unpark_all else a.unpark
    if not ids:
        print("nothing is parked.")
        return 0
    moved = unpark(c, ids, a.reason.strip())
    for rid in ids:
        if str(rid) in moved:
            print(f"  un-parked {rid} — open again, attempts and env_failures reset to 0")
        else:
            # ⚠ Saying WHICH is the point: "not parked" and "no such range" are different mistakes.
            here = c.execute("SELECT status FROM ranges WHERE id=?", (str(rid),)).fetchone()
            why = f"status is '{here['status']}', not 'failed'" if here else "no such range"
            print(f"  ⛔ {rid} NOT changed — {why}", file=sys.stderr)
    c.close()
    return 0 if len(moved) == len(ids) else 1


if __name__ == "__main__":
    sys.exit(main())

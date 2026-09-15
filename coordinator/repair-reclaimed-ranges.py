#!/usr/bin/env python3
"""#339 one-off repair: put back the verified `ranges` rows that claim() overwrote.

Until #339, a claim that read the frontier just before a submit for frontier+1 committed would re-offer that block, and
its INSERT OR REPLACE turned the block's row from 'verified' back into 'claimed', losing receipt_sha, verified_at and
the prover's assignee. The proof file, its `vranges` record and its `submissions` row were untouched, so everything
lost can be put back from them. check-proofs.py reports each such row as "has no verified record in `ranges`".

A row is restored only when ALL of these hold:
  1. it is on the genesis chain: `hi` is at or below the frontier. Above it, the claim may be the #281 re-offer of a
     cover that cannot seam, and restoring that row would make submit() refuse its re-proof with 409;
  2. its claim is dead: nothing claimed or beat on it for --min-claim-age seconds (7200);
  3. it has a `vranges` record, and a `submissions` row for the same id with verified=1 by the key that record names
     (the latest such row is used); and
  4. the stored proof file hashes to that submission's receipt_sha.
Every other claimed row is listed with the reason it was left alone.

    ./coordinator/repair-reclaimed-ranges.py            # dry run: say what would change, change nothing
    ./coordinator/repair-reclaimed-ranges.py --apply    # change it, in one transaction

The coordinator can keep running: --apply holds SQLite's write lock only while it re-reads and updates the rows.
Exit status: 0 done (or nothing to do); 2 could not run (no database, no frontier).
Environment: COORD_DB, COORD_PROOFS.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request


def plan(c, proofs, frontier, min_claim_age, now):
    """([(id, submission row)] to restore, [(id, why)] left alone)."""
    fixes, left = [], []
    for r in c.execute("SELECT r.id, r.hi, r.claimed_at, r.last_beat, v.pubkey AS v_pubkey FROM ranges r "
                       "LEFT JOIN vranges v ON v.id = r.id WHERE r.status = 'claimed' ORDER BY r.lo").fetchall():
        rid = r["id"]
        if r["v_pubkey"] is None:
            left.append((rid, "no vranges record: an ordinary claim, nothing was overwritten"))
            continue
        if r["hi"] > frontier:
            left.append((rid, f"above the frontier ({frontier}): may be a #281 re-offer, whose re-proof this would refuse"))
            continue
        idle = now - max(r["claimed_at"] or 0, r["last_beat"] or 0)
        if idle < min_claim_age:
            left.append((rid, f"claimed or beaten {int(idle)} s ago: may still be live, run again later"))
            continue
        s = c.execute("SELECT pubkey, handle, receipt_sha, ts FROM submissions WHERE range_id = ? AND verified = 1 "
                      "AND pubkey = ? ORDER BY ts DESC LIMIT 1", (rid, r["v_pubkey"])).fetchone()
        if s is None:
            left.append((rid, "no verified submission by the key its vranges record names"))
            continue
        try:
            with open(os.path.join(proofs, f"proof_{rid}.bin"), "rb") as f:
                got = hashlib.sha256(f.read()).hexdigest()
        except OSError as e:
            left.append((rid, f"cannot read its proof file: {e}"))
            continue
        if got != s["receipt_sha"]:
            left.append((rid, "its proof file does not hash to the verified submission's receipt"))
            continue
        fixes.append((rid, s))
    return fixes, left


def main(argv=None):
    ap = argparse.ArgumentParser(description="#339: restore verified ranges rows that claim() overwrote.")
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"))
    ap.add_argument("--proofs", default=os.environ.get("COORD_PROOFS", "/var/lib/hazync/proofs"))
    ap.add_argument("--frontier", type=int, help="the frontier height (default: read from --meta-url)")
    ap.add_argument("--meta-url", default="http://127.0.0.1:8899/api/meta")
    ap.add_argument("--min-claim-age", type=float, default=7200)
    ap.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.db):
        print(f"COULD NOT RUN: no database at {a.db}")
        return 2
    if a.frontier is None:
        try:
            with urllib.request.urlopen(a.meta_url, timeout=30) as resp:
                a.frontier = int(json.load(resp)["frontier"])
        except Exception as e:
            print(f"COULD NOT RUN: cannot read the frontier from {a.meta_url}: {e}")
            return 2

    c = sqlite3.connect(a.db, timeout=60, isolation_level=None)
    c.row_factory = sqlite3.Row
    if a.apply:
        c.execute("BEGIN IMMEDIATE")        # nothing can claim or submit between the reading and the writing
    fixes, left = plan(c, a.proofs, a.frontier, a.min_claim_age, time.time())
    verb = "restore" if a.apply else "would restore"
    for rid, s in fixes:
        print(f"  {verb} {rid}: verified by {s['handle']} at {s['ts']:.0f}, receipt {s['receipt_sha'][:16]}")
    for rid, why in left:
        print(f"  leave   {rid}: {why}")
    if a.apply:
        for rid, s in fixes:
            c.execute("UPDATE ranges SET status = 'verified', receipt_sha = ?, verified_at = ?, assignee = ?, handle = ?,"
                      " claimed_at = NULL, last_beat = NULL, claim_nonce = NULL, claim_signed = NULL"
                      " WHERE id = ? AND status = 'claimed'",
                      (s["receipt_sha"], s["ts"], s["pubkey"], s["handle"], rid))
        c.execute("COMMIT")
    c.close()
    print()
    print(f"{len(fixes)} row(s) {'restored' if a.apply else 'to restore (dry run, nothing changed: add --apply)'}, "
          f"{len(left)} claimed row(s) left alone, frontier {a.frontier}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

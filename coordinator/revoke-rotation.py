#!/usr/bin/env python3
"""#311: the operator revokes a key rotation, or reinstates one revoked by mistake.

`POST /api/rotate` moves a contributor's attribution to a new key when both keys sign. Whoever holds a copy of
`key.hex` can make both signatures, so a thief can move a contributor's whole history onto a key of their own, and
the owner cannot move it back: each old key rotates once. This is the operator's way to undo that.

Nothing is deleted or rewritten. The `rotations` row stays, and each decision is a new row in
`rotation_revocations` (action `revoke` or `reinstate`, a reason, who, when); the latest row for a rotation decides.
The coordinator reads rotations fresh every time, so attribution stops following a revoked rotation at once and the
board shows it on its next rebuild (under a minute), with no restart. A key whose rotation is revoked cannot rotate
again, because whoever stole it holds it too.

    ./coordinator/revoke-rotation.py list                    # every rotation, whether it is in force, its history
    ./coordinator/revoke-rotation.py revoke <old key> --reason "stolen key; owner confirmed on 2026-09-16"
    ./coordinator/revoke-rotation.py revoke <old key> --reason "..." --apply
    ./coordinator/revoke-rotation.py reinstate <old key> --reason "revoked in error" --apply

<old key> is the rotated-away public key in hex, or a unique prefix of at least 10 characters (the board shows 10).
Without --apply nothing is written. Run it as the coordinator's user (`runuser -u hazync -- ...`) so SQLite's -wal
and -shm files stay theirs, and pass --actor so the record says who decided.

Exit status: 0 done, or a dry run; 1 refused (no such rotation, an ambiguous prefix, nothing to change, a blank
reason, a reinstatement that would close a cycle); 2 could not run (no database, or one from a coordinator older
than #311, which has no `rotation_revocations` table yet).
Environment: COORD_DB.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

MAX_DEPTH = 32   # server.ROTATE_MAX_DEPTH


def utc(ts):
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts)) if ts else "an unknown time"


def latest_actions(c):
    """{(old, new): latest action}: the coordinator's rule (server.revoked_edges), the newest row for a rotation wins."""
    out = {}
    for r in c.execute("SELECT old_pubkey, new_pubkey, action FROM rotation_revocations ORDER BY id"):
        out[(r["old_pubkey"], r["new_pubkey"])] = r["action"]
    return out


def in_force(c):
    """{old: new} for every rotation not revoked: what server.rotation_map() returns (test_rotation_revoke.py checks)."""
    latest = latest_actions(c)
    return {r["old_pubkey"]: r["new_pubkey"] for r in c.execute("SELECT old_pubkey, new_pubkey FROM rotations")
            if latest.get((r["old_pubkey"], r["new_pubkey"])) != "revoke"}


def resolve(pk, rmap):
    """server.resolve_pubkey over a given map."""
    cur, seen = pk, {pk}
    for _ in range(MAX_DEPTH):
        nxt = rmap.get(cur)
        if nxt is None or nxt in seen:
            return cur
        seen.add(nxt)
        cur = nxt
    return cur


def describe(c, pk):
    r = c.execute("SELECT handle FROM contributors WHERE pubkey=?", (pk,)).fetchone()
    n = c.execute("SELECT COUNT(*) FROM vranges WHERE pubkey=?", (pk,)).fetchone()[0]
    return f"{pk[:16]} ({(r['handle'] if r and r['handle'] else 'no handle')}, {n:,} proofs signed by this key)"


def find(c, key):
    """(rotation row, None) or (None, why not)."""
    key = (key or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{10,64}", key):
        return None, "the old key must be hex, at least 10 characters (the board shows 10)"
    rows = c.execute("SELECT * FROM rotations WHERE old_pubkey LIKE ? ORDER BY created", (key + "%",)).fetchall()
    if not rows:
        return None, f"no rotation from a key starting {key}"
    if len(rows) > 1:
        return None, (f"{key} matches {len(rows)} rotated keys, give more of it: "
                      + ", ".join(r["old_pubkey"][:16] for r in rows))
    return rows[0], None


def cmd_list(c, as_json):
    latest = latest_actions(c)
    out = []
    for r in c.execute("SELECT * FROM rotations ORDER BY created, old_pubkey").fetchall():
        edge = (r["old_pubkey"], r["new_pubkey"])
        history = [dict(action=h["action"], reason=h["reason"], actor=h["actor"], at=h["created"]) for h in c.execute(
            "SELECT * FROM rotation_revocations WHERE old_pubkey=? AND new_pubkey=? ORDER BY id", edge)]
        out.append(dict(old=r["old_pubkey"], new=r["new_pubkey"], created=r["created"],
                        in_force=latest.get(edge) != "revoke", history=history))
    if as_json:
        print(json.dumps({"rotations": out}, indent=1))
        return 0
    if not out:
        print("no rotations")
    for x in out:
        print(f"{'in force' if x['in_force'] else 'REVOKED '}  {describe(c, x['old'])}\n"
              f"          -> {describe(c, x['new'])}, rotated {utc(x['created'])}")
        for h in x["history"]:
            print(f"          {h['action']} {utc(h['at'])} by {h['actor'] or 'unknown'}: {h['reason']}")
    return 0


def cmd_change(c, a):
    action = a.cmd
    reason = (a.reason or "").strip()
    if not reason:
        print("REFUSED: --reason must say why; it is kept with the record for good")
        return 1
    if a.apply:
        c.execute("BEGIN IMMEDIATE")     # nothing can rotate or revoke between these checks and the write

    def refuse(msg):
        print(f"REFUSED: {msg}")
        if a.apply:
            c.execute("ROLLBACK")
        return 1

    rot, why = find(c, a.old)
    if rot is None:
        return refuse(why)
    old, new = rot["old_pubkey"], rot["new_pubkey"]
    revoked = latest_actions(c).get((old, new)) == "revoke"
    print(f"rotation {describe(c, old)}\n      -> {describe(c, new)}, rotated {utc(rot['created'])}: "
          f"{'REVOKED' if revoked else 'in force'}")
    if action == "revoke" and revoked:
        return refuse("already revoked, nothing to change")
    if action == "reinstate" and not revoked:
        return refuse("in force, nothing to reinstate")
    rmap = in_force(c)
    before = resolve(old, rmap)
    if action == "reinstate":
        if resolve(new, rmap) == old:
            return refuse(f"reinstating it would close a cycle: {new[:16]} now leads back to {old[:16]}")
        rmap[old] = new
    else:
        rmap.pop(old, None)
    print(f"the blocks {old[:16]} signed are shown under {before[:16]} now, and under {resolve(old, rmap)[:16]} after")
    if not a.apply:
        print(f"dry run: would {action} it (reason: {reason}). Nothing written; add --apply to record it.")
        return 0
    c.execute("INSERT INTO rotation_revocations(old_pubkey, new_pubkey, action, reason, actor, created)"
              " VALUES(?,?,?,?,?,?)", (old, new, action, reason, a.actor, time.time()))
    c.execute("COMMIT")
    print(f"{'revoked' if action == 'revoke' else 'reinstated'}, recorded by {a.actor}. "
          "The board follows it on its next rebuild, under a minute; no restart.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="#311: revoke a key rotation, or reinstate one revoked by mistake.")
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="every rotation, whether it is in force, and its history")
    ls.add_argument("--json", action="store_true")
    for name in ("revoke", "reinstate"):
        p = sub.add_parser(name)
        p.add_argument("old", help="the rotated-away public key (hex), or a unique prefix of 10+ characters")
        p.add_argument("--reason", required=True, help="why; kept with the record for good")
        p.add_argument("--actor", default=os.environ.get("SUDO_USER") or os.environ.get("USER") or "operator",
                       help="who decided (default: $SUDO_USER or $USER)")
        p.add_argument("--apply", action="store_true", help="record it (default: dry run)")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.db):
        print(f"COULD NOT RUN: no database at {a.db}")
        return 2
    c = sqlite3.connect(a.db, timeout=60, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in ("rotations", "rotation_revocations") if t not in have]
        if missing:
            print(f"COULD NOT RUN: {a.db} has no {missing[0]} table. Deploy a coordinator with #311 first; "
                  "it creates the table when it starts.")
            return 2
        return cmd_list(c, a.json) if a.cmd == "list" else cmd_change(c, a)
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""#311: an operator revocation stops attribution following a rotation, and changes nothing else.

A stolen key.hex can sign both halves of a rotation, so a thief can move a contributor's history onto a key of
their own. `coordinator/revoke-rotation.py` lets the operator undo that. What must hold:

  * REVOKED MEANS NOT FOLLOWED: the old key's blocks come back to it on the board, with no restart.
  * NOTHING IS DELETED: the rotation row stays, and the revocation is a new row with its reason.
  * NO WAY BACK FOR THE THIEF: a revoked key cannot rotate again, and trying is a 409, not an IntegrityError.
  * UNDONE ONLY BY RECORD: `reinstate` is another row, and it refuses to close a cycle.
  * ONE RULE: the tool's view of which rotations are in force is exactly `server.rotation_map()`.

Usage:
  python3 test_rotation_revoke.py            # assertions; exit 0 on success
  python3 test_rotation_revoke.py --control  # the coordinator ignores revocations, as before #311; MUST fail
It runs from test_rotation.py, which CI already runs, so no workflow change is needed.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
_tmp = tempfile.NamedTemporaryFile(prefix="revoke_", suffix=".db", delete=False); _tmp.close()
os.environ["COORD_DB"] = _tmp.name
os.environ.setdefault("COORD_WEB", HERE)
_modf = tempfile.NamedTemporaryFile(prefix="modblock_", suffix=".txt", delete=False); _modf.close()
os.environ["MOD_BLOCK_FILE"] = _modf.name
sys.path.insert(0, HERE)
import server  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    print("SKIP: `cryptography` not installed — rotation signatures cannot be exercised")
    sys.exit(0)

server.init_db()
if CONTROL:
    server._CONTROL_REVOCATIONS_IGNORED = True
    print("CONTROL: the coordinator ignores revocations -- the checks below MUST fail")

TOOL = os.path.join(HERE, "revoke-rotation.py")
fails = []


def check(cond, what, out=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)
        if out:
            print("      " + out.strip()[-1200:].replace("\n", "\n      "))


class Key:
    def __init__(self):
        self._sk = Ed25519PrivateKey.generate()
        self.pk = self._sk.public_key().public_bytes_raw().hex()

    def sign(self, msg):
        return self._sk.sign(msg).hex()


def q(sql, args=()):
    c = server.db()
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def reset():
    for t in ("rotations", "rotation_revocations", "vranges", "contributors", "submissions"):
        q(f"DELETE FROM {t}")
    open(_modf.name, "w").close()


def add_range(pk, lo, hi, handle):
    q("INSERT OR REPLACE INTO vranges(id,lo,hi,pubkey,handle,ts) VALUES(?,?,?,?,?,?)",
      (f"{lo}-{hi}-{pk[:6]}", lo, hi, pk, handle, time.time()))
    q("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)", (pk, handle, time.time()))


def rotate(a, b, handle=None):
    ts = time.time()
    msg = server.rotate_message(a.pk, b.pk, ts)
    body = {"old_pubkey": a.pk, "new_pubkey": b.pk, "ts": ts, "sig_old": a.sign(msg), "sig_new": b.sign(msg)}
    if handle:
        body["handle"] = handle
    return server.rotate(body)


def tool(*args, db=None):
    p = subprocess.run([sys.executable, TOOL, "--db", db or _tmp.name, *args], capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def board():
    """{handle: blocks} for every leaderboard row that has blocks."""
    return {r["handle"]: r["blocks"] for r in server.state()["leaderboard"] if r["blocks"] > 0}


def proved():
    return {pk: v["proved"] for pk, v in server.contributions_by_pubkey().items()}


def actions():
    return [r["action"] for r in q("SELECT action FROM rotation_revocations ORDER BY id")]


print("== a thief's rotation, revoked ==")
reset()
owner, thief = Key(), Key()
add_range(owner.pk, 0, 99, "owner")
code, _ = rotate(owner, thief, handle="thief")
check(code == 200 and board() == {"thief": 100}, f"the thief's rotation moves the owner's 100 blocks ({board()})")

code, out = tool("revoke", owner.pk[:12], "--reason", "stolen key.hex; owner confirmed")
check(code == 0 and "dry run" in out and actions() == [], "a dry run names the rotation and records nothing", out)

code, out = tool("revoke", owner.pk[:12], "--reason", "stolen key.hex; owner confirmed", "--actor", "test-op", "--apply")
rec = q("SELECT * FROM rotation_revocations")
check(code == 0 and len(rec) == 1 and rec[0]["action"] == "revoke" and rec[0]["reason"] == "stolen key.hex; owner confirmed"
      and rec[0]["actor"] == "test-op" and rec[0]["old_pubkey"] == owner.pk and rec[0]["new_pubkey"] == thief.pk,
      "--apply records one revocation of that exact rotation, with its reason and who decided", out)
check(server.resolve_pubkey(owner.pk) == owner.pk, "the owner's key resolves to itself again")
p = proved()
check(p.get(owner.pk) == 100 and thief.pk not in p, "the 100 proved blocks count for the owner again, none for the thief")
check(board() == {"owner": 100}, f"the board shows the owner's own row, with no restart ({board()})")
rows = q("SELECT new_pubkey FROM rotations WHERE old_pubkey=?", (owner.pk,))
check(len(rows) == 1 and rows[0]["new_pubkey"] == thief.pk, "the rotation row is still there: nothing is deleted")

code, body = rotate(owner, Key())
check(code == 409 and "revoked" in (body or {}).get("error", ""),
      f"the stolen key cannot rotate again: 409, saying it was revoked ({code} {body})")
check(len(q("SELECT * FROM rotations WHERE old_pubkey=?", (owner.pk,))) == 1, "...and no second rotation row was written")
code, out = tool("revoke", owner.pk[:12], "--reason", "again", "--apply")
check(code == 1 and "already revoked" in out and actions() == ["revoke"], "revoking it twice is refused and records nothing", out)

print("== reinstating is another record ==")
code, out = tool("reinstate", owner.pk, "--reason", "revoked in error", "--apply")
check(code == 0 and actions() == ["revoke", "reinstate"], "reinstate adds a second row and keeps the first", out)
check(server.resolve_pubkey(owner.pk) == thief.pk and board() == {"thief": 100},
      f"the rotation is followed again ({board()})")
code, out = tool("reinstate", owner.pk, "--reason", "twice", "--apply")
check(code == 1 and "nothing to reinstate" in out and len(actions()) == 2, "reinstating a rotation in force is refused", out)

print("== a chain: revoking the middle rotation ==")
reset()
a, b, c3 = Key(), Key(), Key()
add_range(a.pk, 0, 9, "a"); add_range(b.pk, 10, 19, "b"); add_range(c3.pk, 20, 29, "c")
rotate(a, b); rotate(b, c3)
check(server.resolve_pubkey(a.pk) == c3.pk, "before: A->B->C resolves A to C")
code, out = tool("revoke", b.pk[:16], "--reason", "B was stolen", "--apply")
check(code == 0, "revoking B->C is accepted", out)
check(server.resolve_pubkey(a.pk) == b.pk and server.resolve_pubkey(b.pk) == b.pk
      and server.resolve_pubkey(c3.pk) == c3.pk, "after: A still reaches B, and B and C are each their own")
p = proved()
check(p.get(b.pk) == 20 and p.get(c3.pk) == 10 and a.pk not in p,
      f"B keeps A's blocks and its own (20), C only its own (10): {sorted(p.values())}")

print("== reinstating refuses to close a cycle ==")
reset()
a, t = Key(), Key()
add_range(a.pk, 0, 9, "a")
rotate(a, t)
tool("revoke", a.pk[:12], "--reason", "stolen", "--apply")
code, _ = rotate(t, a)
check(code == 200, f"with A->T revoked, T may rotate to A ({code})")
code, out = tool("reinstate", a.pk[:12], "--reason", "undo", "--apply")
check(code == 1 and "cycle" in out and actions() == ["revoke"],
      "reinstating A->T would close A->T->A, so it is refused and nothing is recorded", out)

print("== the tool refuses what it cannot be sure of ==")
code, out = tool("revoke", "abc", "--reason", "x")
check(code == 1 and "at least 10" in out, "a prefix shorter than 10 characters is refused", out)
code, out = tool("revoke", "f" * 20, "--reason", "x")
check(code == 1 and "no rotation" in out, "a key with no rotation is refused", out)
q("INSERT INTO rotations(old_pubkey,new_pubkey,created) VALUES(?,?,?)", ("abcdef0123" + "1" * 54, "2" * 64, 1))
q("INSERT INTO rotations(old_pubkey,new_pubkey,created) VALUES(?,?,?)", ("abcdef0123" + "3" * 54, "4" * 64, 2))
code, out = tool("revoke", "abcdef0123", "--reason", "x", "--apply")
check(code == 1 and "matches 2" in out and actions() == ["revoke"], "an ambiguous prefix is refused and records nothing", out)
code, out = tool("revoke", a.pk[:12], "--reason", "   ", "--apply")
check(code == 1 and "reason" in out, "a blank reason is refused", out)
old_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False); old_db.close()
_e = sqlite3.connect(old_db.name)
_e.execute("CREATE TABLE rotations(old_pubkey TEXT PRIMARY KEY, new_pubkey TEXT NOT NULL, created REAL)")
_e.commit(); _e.close()
code, out = tool("list", db=old_db.name)
check(code == 2 and "COULD NOT RUN" in out, "a database from before #311 is COULD NOT RUN, never an empty list", out)
code, out = tool("list", db=os.path.join(tempfile.gettempdir(), "no-such-hazync-coordinator.db"))
check(code == 2 and "COULD NOT RUN" in out, "no database is COULD NOT RUN", out)

print("== the tool and the coordinator agree on what is in force ==")
reset()
k = [Key() for _ in range(5)]
rotate(k[0], k[1]); rotate(k[1], k[2]); rotate(k[3], k[4])
tool("revoke", k[1].pk[:12], "--reason", "x", "--apply")
tool("revoke", k[3].pk[:12], "--reason", "x", "--apply")
tool("reinstate", k[3].pk[:12], "--reason", "y", "--apply")
code, out = tool("list", "--json")
listed = {r["old"]: r["new"] for r in json.loads(out)["rotations"] if r["in_force"]} if code == 0 else None
served = server.rotation_map()
check(listed == served, f"`list --json` in force == server.rotation_map() ({len(listed or {})} vs {len(served)} rotations)")
code, out = tool("list")
check(code == 0 and out.count("REVOKED") == 1 and out.count("in force") == 2 and "reinstate" in out,
      "`list` shows two rotations in force, one revoked, and the history", out)

for _f in (_tmp.name, _modf.name, old_db.name):
    try:
        os.remove(_f)
    except OSError:
        pass
print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — revocations ignored and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — revocations were ignored and every check still passed.")
    sys.exit(1)
if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("revocation: a revoked rotation is not followed, nothing is deleted, and the stolen key cannot rotate again.")

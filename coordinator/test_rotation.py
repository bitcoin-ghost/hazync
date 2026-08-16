#!/usr/bin/env python3
"""Tests for #113 key rotation.

The endpoint is small; the risk is not. Three things here are worth more than the rest:

  * OVERLAP. Two keys belonging to one person prove interleaved ranges. Their merged total must be
    the union, not the sum, or a rotation silently mints blocks that were never proved and the
    leaderboard stops reconciling with the headline `proven` count.
  * CONSENT. A signature from the old key alone must not move anything, and neither must one from the
    new key alone. Otherwise rotation is either a way to annex someone's history or a way to dump
    yours onto them.
  * MODERATION. A takedown must survive a rotation, or it is escaped by generating a fresh key.

Run: python3 coordinator/test_rotation.py     (silent success, non-zero exit on failure)
"""
import os, sys, time, tempfile

_tmp = tempfile.NamedTemporaryFile(prefix="rotation_", suffix=".db", delete=False); _tmp.close()
os.environ["COORD_DB"] = _tmp.name
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
_modf = tempfile.NamedTemporaryFile(prefix="modblock_", suffix=".txt", delete=False); _modf.close()
os.environ["MOD_BLOCK_FILE"] = _modf.name
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
except ImportError:
    print("SKIP: `cryptography` not installed — rotation signatures cannot be exercised")
    sys.exit(0)

server.init_db()

FAILS = []
def check(cond, what):
    if not cond:
        FAILS.append(what)
        print(f"[FAIL] {what}")

class Key:
    def __init__(self):
        self._sk = Ed25519PrivateKey.generate()
        self.pk = self._sk.public_key().public_bytes_raw().hex()
    def sign(self, msg: bytes) -> str:
        return self._sk.sign(msg).hex()

def reset():
    """Fresh tables between cases — rotations are append-only and PRIMARY KEY'd, so cases would
    otherwise contaminate each other through leftover edges."""
    c = server.db()
    for t in ("rotations", "vranges", "contributors", "submissions"):
        c.execute(f"DELETE FROM {t}")
    c.commit(); c.close()
    open(_modf.name, "w").close()

def add_range(pk, lo, hi, handle="x"):
    c = server.db()
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,pubkey,handle,ts) VALUES(?,?,?,?,?,?)",
              (f"{lo}-{hi}-{pk[:6]}", lo, hi, pk, handle, time.time()))
    c.execute("INSERT OR IGNORE INTO contributors(pubkey,handle,first_seen) VALUES(?,?,?)",
              (pk, handle, time.time()))
    c.commit(); c.close()

def do_rotate(a: Key, b: Key, ts=None, sign_old=None, sign_new=None, handle=None):
    ts = time.time() if ts is None else ts
    msg = server.rotate_message(a.pk, b.pk, ts)
    body = {"old_pubkey": a.pk, "new_pubkey": b.pk, "ts": ts,
            "sig_old": sign_old if sign_old is not None else a.sign(msg),
            "sig_new": sign_new if sign_new is not None else b.sign(msg)}
    if handle: body["handle"] = handle
    return server.rotate(body)

# ---------------------------------------------------------------- resolution ----------------------
reset()
a, b, c_ = Key(), Key(), Key()
check(server.resolve_pubkey(a.pk) == a.pk, "an un-rotated key resolves to itself")

code, _ = do_rotate(a, b)
check(code == 200, f"a well-formed rotation is accepted (got {code})")
check(server.resolve_pubkey(a.pk) == b.pk, "A->B resolves A to B")
check(server.resolve_pubkey(b.pk) == b.pk, "the head resolves to itself")

code, _ = do_rotate(b, c_)
check(code == 200, f"chaining B->C is accepted (got {code})")
check(server.resolve_pubkey(a.pk) == c_.pk, "A->B->C resolves A all the way to C")

# A corrupt DB must not hang a request thread.
reset()
x, y = Key(), Key()
conn = server.db()
conn.execute("INSERT INTO rotations(old_pubkey,new_pubkey,created) VALUES(?,?,?)", (x.pk, y.pk, 0))
conn.execute("INSERT INTO rotations(old_pubkey,new_pubkey,created) VALUES(?,?,?)", (y.pk, x.pk, 0))
conn.commit(); conn.close()
t0 = time.time()
server.resolve_pubkey(x.pk)
check(time.time() - t0 < 2, "a hand-inserted cycle terminates rather than hanging")

# ---------------------------------------------------------------- the overlap case ----------------
# The one that would silently inflate the board. Old box proved 100-199, new box 150-249.
reset()
a, b = Key(), Key()
add_range(a.pk, 100, 199)
add_range(b.pk, 150, 249)
before = server.distinct_blocks_by_pubkey()
check(before.get(a.pk) == 100 and before.get(b.pk) == 100, "pre-rotation each key counts its own 100")
code, _ = do_rotate(a, b)
check(code == 200, "rotation accepted for the overlap case")
after = server.distinct_blocks_by_pubkey()
check(a.pk not in after, "the rotated-away key no longer holds a total of its own")
check(after.get(b.pk) == 150,
      f"merged total is the UNION 100..249 = 150, not the sum 200 (got {after.get(b.pk)})")

# Non-overlapping ranges must still add up normally.
reset()
a, b = Key(), Key()
add_range(a.pk, 0, 99); add_range(b.pk, 500, 599)
do_rotate(a, b)
check(server.distinct_blocks_by_pubkey().get(b.pk) == 200, "disjoint ranges merge to the plain sum")

# Exactly-adjacent ranges are one run, not two.
reset()
a, b = Key(), Key()
add_range(a.pk, 0, 99); add_range(b.pk, 100, 199)
do_rotate(a, b)
check(server.distinct_blocks_by_pubkey().get(b.pk) == 200, "adjacent ranges merge without a gap")

# ---------------------------------------------------------------- consent + replay ----------------
reset()
a, b, imposter = Key(), Key(), Key()
add_range(a.pk, 0, 99)

ts = time.time()
msg = server.rotate_message(a.pk, b.pk, ts)
code, _ = do_rotate(a, b, ts=ts, sign_old=imposter.sign(msg))
check(code == 403, f"a rotation not signed by the OLD key is refused (got {code})")
code, _ = do_rotate(a, b, ts=ts, sign_new=imposter.sign(msg))
check(code == 403, f"a rotation not signed by the NEW key is refused (got {code})")

# A signature over a DIFFERENT pair must not be replayable onto this one.
other = Key()
stolen = a.sign(server.rotate_message(a.pk, other.pk, ts))
code, _ = do_rotate(a, b, ts=ts, sign_old=stolen)
check(code == 403, f"a signature bound to another target key does not transfer (got {code})")

code, _ = do_rotate(a, b, ts=time.time() - server.ROTATE_MAX_SKEW - 60)
check(code == 400, f"a stale timestamp is refused (got {code})")

code, _ = do_rotate(a, a)
check(code == 400, f"rotating a key to itself is refused (got {code})")

code, _ = do_rotate(a, b)
check(code == 200, "the genuine rotation still succeeds after the failed attempts")
code, _ = do_rotate(a, b)
check(code == 409, f"rotating an already-rotated key is refused (got {code})")
code, _ = do_rotate(a, Key())
check(code == 409, "an already-rotated key cannot fork to a second head")
code, _ = do_rotate(b, a)
check(code == 400, f"a rotation that would close a cycle is refused (got {code})")

# ---------------------------------------------------------------- board-level effects -------------
reset()
a, b = Key(), Key()
add_range(a.pk, 0, 99, handle="old-name")
add_range(b.pk, 200, 299, handle="new-name")
st = server.state()
check(st["progress"]["contributors"] == 2, "two keys read as two contributors before rotation")
do_rotate(a, b)
st = server.state()
check(st["progress"]["contributors"] == 1,
      f"after rotation the pair counts as ONE contributor (got {st['progress']['contributors']})")
lead = [x for x in st["leaderboard"] if x["blocks"] > 0]
check(len(lead) == 1, f"the leaderboard shows one merged row (got {len(lead)})")
check(lead and lead[0]["blocks"] == 200, "the merged row carries both keys' blocks")
check(lead and lead[0]["handle"] == "new-name", "the merged row uses the head's handle")

# Rotating to a key that has never proved anything must not lose the total.
reset()
a, fresh = Key(), Key()
add_range(a.pk, 0, 99, handle="stranded")
code, res = do_rotate(a, fresh)
check(code == 200, "rotation to a never-seen key is accepted")
st = server.state()
lead = [x for x in st["leaderboard"] if x["blocks"] > 0]
check(len(lead) == 1 and lead[0]["blocks"] == 100,
      f"blocks survive a rotation onto a key with no history (got {lead})")
check(lead and lead[0]["handle"] == "stranded", "the old handle carries over when none is supplied")

# ---------------------------------------------------------------- moderation ----------------------
reset()
a, b = Key(), Key()
add_range(a.pk, 0, 99)
do_rotate(a, b)
with open(_modf.name, "w") as f:
    f.write(a.pk + "\n")                       # block the OLD key, after it has already rotated
st = server.state()
shown = [x for x in st["leaderboard"] if x["blocks"] > 0]
check(not shown, "blocking a rotated-away key still hides the head it resolves to")

reset()
a, b = Key(), Key()
add_range(a.pk, 0, 99)
with open(_modf.name, "w") as f:
    f.write(a.pk + "\n")
code, _ = do_rotate(a, b)
check(code == 403, f"a blocked key cannot rotate away from a takedown (got {code})")

# ---------------------------------------------------------------- teardown ------------------------
for _f in (_tmp.name, _modf.name):
    try: os.remove(_f)
    except Exception: pass

if FAILS:
    print(f"\nrotation: {len(FAILS)} FAILED")
    sys.exit(1)
print("rotation: all checks passed")

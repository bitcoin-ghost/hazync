#!/usr/bin/env python3
"""
Tests for the spine and fold paths added in v0.13.0 (#30, #37) — the parts that do not need a GPU.

WHAT THIS DOES NOT COVER, stated first so nothing reads as more assurance than it is:

  * Real STARK verification of a spine. `verify_spine` shells out to the host binary, which is 184 MB
    and not in CI, so these tests run with VERIFY_MODE=mock. They exercise the STORE / MONOTONIC /
    SERVE logic around verification, never verification itself.
  * Actual folding. `fold-range` and `extend-spine` are prove operations. CI has no GPU and, on CPU,
    one fold is ~2-3 minutes. Those are verified by hand against real board receipts and recorded in
    prover/evidence/extend_spine_*.txt.

What IS covered here is the logic that has no excuse for being untested: which pairs get offered for
folding, what a range id is allowed to be, and whether a shorter spine can overwrite a longer one.

The third of those is why this file exists. `parse_range` rejected arbitrary-width ranges — right for
claims, since two claim ids must never partially overlap — but folding produces arbitrary widths by
construction, so a folded [1..2] was refused as "invalid range id" AFTER the fold had been done. That
was found by running it, not by reading it, and nothing in CI would have caught it.

Usage:
  python3 test_spine_fold.py            # assertions; silent success, exit 0
  python3 test_spine_fold.py --control  # break the logic on purpose; the tests MUST fail
"""
import base64
import json
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpspine = tempfile.mkdtemp(prefix="spine_")
_tmpproofs = tempfile.mkdtemp(prefix="proofs_")
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = _tmpspine
os.environ["COORD_PROOFS"] = _tmpproofs
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()

fails = []


def check(cond, what):
    if cond:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)


def seed(ranges):
    """Put verified ranges on the board. `ranges` is a list of (lo, hi)."""
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")
    for lo, hi in ranges:
        rid = str(lo) if lo == hi else f"{lo}-{hi}"
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (rid, lo, hi))
        c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
                  " VALUES(?,?,?,?,?,'','t',0,0,'0')", (rid, lo, hi, f"in{lo}", f"out{hi}"))
    c.commit()
    c.close()


def pairs():
    return {(p["left"], p["right"]) for p in server.foldable(32)}


# ── foldable(): which pairs are offered ───────────────────────────────────────────────────────────
print("== foldable() ==")

seed([])
check(server.foldable() == [], "an empty board offers nothing to fold")

# Only ALIGNED siblings fold. With 1,2,3 the sole tree pair is (1,2): [2..3] is not aligned to its
# own width, and block 3's sibling (block 4) does not exist yet. The earlier version of this test
# asserted {(1,2),(2,3)} because that is what the buggy implementation did — it was written from the
# code rather than from the property, so it passed against behaviour that did not converge.
seed([(1, 1), (2, 2), (3, 3)])
check(pairs() == {("1", "2")}, "only the aligned sibling pair is offered, not every adjacent pair")

seed([(1, 1), (2, 2), (3, 3), (4, 4)])
check(pairs() == {("1", "2"), ("3", "4")}, "four blocks offer both sibling pairs at the leaf level")

seed([(1, 1), (3, 3)])
check(server.foldable() == [], "non-adjacent ranges are not a foldable pair")

# The one that matters: a pair whose fold ALREADY EXISTS must not be offered again, or workers burn
# GPU time re-folding what the board already has.
seed([(1, 1), (2, 2), (3, 3), (4, 4), (1, 2), (3, 4)])
got = pairs()
if CONTROL:
    # Break exactly that rule — the pre-check that skips already-folded pairs — and prove this test
    # notices. A test that cannot fail here would let duplicate work go unnoticed forever.
    _orig = server.foldable

    def _broken(limit=8):
        with server._lock:
            c = server.db()
            rows = c.execute("SELECT id, lo, hi FROM vranges ORDER BY lo").fetchall()
            c.close()
        starts = {}
        for r in rows:
            starts.setdefault(r["lo"], []).append(r)
        out = []
        for r in rows:
            for s in starts.get(r["hi"] + 1, ()):
                out.append({"left": r["id"], "right": s["id"], "lo": r["lo"], "hi": s["hi"],
                            "result": f"{r['lo']}-{s['hi']}"})
                if len(out) >= limit:
                    return out
        return out
    server.foldable = _broken
    got = pairs()
check(("1", "2") not in got, "a pair whose fold already exists is not offered again")
check(("1-2", "3") not in got,
      "a folded range does NOT pair with a bare neighbour — different widths are not siblings")
check(("1-2", "3-4") in got, "two folded siblings DO fold into their parent")

seed([(i, i) for i in range(1, 12)])
check(len(server.foldable(3)) == 3, "the limit is respected")

# CONVERGENCE. This is the property that was missing and it cost 486 redundant folds on the live
# board before anyone noticed — 581 folds to cover 96 blocks, where a tree needs 95. Offering "any
# adjacent pair whose span does not exist" wanders into every (start, width) combination, because
# each fold creates a new operand. Assert the tree instead: N blocks must fold in exactly N-1 steps
# and terminate.
seed([(i, i) for i in range(1, 17)])
folds = 0
while True:
    ps = server.foldable(32)
    if not ps:
        break
    p = ps[0]
    c = server.db()
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
              " VALUES(?,?,?,'','','','t',0,0,'0')", (p["result"], p["lo"], p["hi"]))
    c.commit(); c.close()
    folds += 1
    if folds > 100:
        break                                   # runaway: the old behaviour never terminated
check(folds == 15, f"16 blocks fold in exactly 15 steps and stop (got {folds})")
c = server.db()
built = {(r["lo"], r["hi"]) for r in c.execute("SELECT lo,hi FROM vranges WHERE hi>lo")}
c.close()
check((1, 16) in built and (1, 8) in built and (9, 16) in built,
      "it builds the aligned tree, up to the full [1..16] root")
check(all(server._tree_node(lo, hi) for lo, hi in built),
      "every range it produced is an aligned power-of-two tree node — no stray widths")


# ── range ids: the collision that broke a real fold ───────────────────────────────────────────────
print("== range ids ==")

check(server.parse_any_range("5") == (5, 5), "a single block is a range")
check(server.parse_any_range("1-2") == (1, 2), "an ARBITRARY-width range is valid for submission")
check(server.parse_any_range("100-299") == (100, 299), "so is a wide one")
check(server.parse_any_range("5-1") is None, "hi < lo is refused")
check(server.parse_any_range("abc") is None, "a non-numeric id is refused")
check(server.parse_any_range("../etc/passwd") is None, "a path is refused (this guards /api/proof/<id>)")
# The distinction that caused the bug: claims stay on the aligned grid, submissions do not.
check(server.parse_range("1-2") is None, "claims still REJECT an unaligned range (no overlapping claim ids)")


# ── spine: store, monotonic, serve ────────────────────────────────────────────────────────────────
print("== spine ==")

check(server.spine_head() is None, "no spine to begin with")

# Sign for real. The coordinator verifies ed25519 over the receipt bytes before it looks at anything
# else, so a test using dummy hex would only ever prove that signature checking works — every spine
# assertion below would pass vacuously on a 403.
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as _ser
_sk = Ed25519PrivateKey.generate()
_pk = _sk.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()

def submit(hi):
    server._MOCK_HI = hi
    receipt = b"receipt-%d" % hi
    return server.submit_spine({"pubkey": _pk, "sig": _sk.sign(receipt).hex(), "handle": "t",
                                "receipt": base64.b64encode(receipt).decode()})

# The mock verifier reports hi=0; drive it from _MOCK_HI so monotonicity is actually exercised.
_real_verify = server.verify_spine
def _mock_verify(receipt):
    ok, note, meta = _real_verify(receipt)
    if ok:
        meta = dict(meta, hi=getattr(server, "_MOCK_HI", 0))
    return ok, note, meta
server.verify_spine = _mock_verify

code, obj = submit(3)
check(code == 200, "a spine is accepted")
check((server.spine_head() or {}).get("hi") == 3, "and becomes the served head")

code, obj = submit(2)
check(code == 409, "a SHORTER spine is refused (409)")
check((server.spine_head() or {}).get("hi") == 3, "and the head is unchanged")

code, obj = submit(4)
check(code == 200 and (server.spine_head() or {}).get("hi") == 4, "a longer spine advances the head")

check(os.path.exists(os.path.join(_tmpspine, "spine.bin")), "the receipt is written to disk")
check(os.path.exists(os.path.join(_tmpspine, "spine.json")), "and the json that advertises it")

code, _ = server.submit_spine({"pubkey": _pk, "sig": "cd" * 64, "handle": "t"})
check(code == 400, "a submission with no receipt is refused")

code, _ = server.submit_spine({"pubkey": _pk, "sig": "cd" * 64, "handle": "t",
                               "receipt": base64.b64encode(b"receipt-9").decode()})
check(code == 403, "a receipt with a bad signature is refused")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — broke the already-folded check and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the logic was broken on purpose and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("spine and fold logic behaves (GPU-dependent paths excluded — see the module docstring).")

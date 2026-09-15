#!/usr/bin/env python3
"""
Tests for CLAIM_RETAKE_WAIT: a key does not get back a block its own claim let lapse without a single beat.

WHY THIS EXISTS. #296 releases a claim that has never beaten after CLAIM_GRACE, so that OTHER workers can take
the block sooner. Nothing stopped the same key taking it straight back. Measured on the live board 2026-09-14:
`ghost:dda215` held frontier block 67,532 for over three hours, re-claiming it every time its claim lapsed; it
asked every few seconds, so it always asked first, and the frontier never moved. The rule claim() already
states for a live claim -- "a fresh claim still cannot re-take its own block" (the 39,318 rule, one bad block
must not consume a worker) -- stopped at the grace.

The wait applies only to the key whose claim lapsed, only to a claim that NEVER beat, and only for
CLAIM_RETAKE_WAIT after it claimed. Every other key is offered the block as soon as the grace ends. Both paths
that hand out a block are covered: the frontier re-offer (#281) and the lowest-free scan.

Usage:
  python3 test_claim_retake.py            # assertions; exit 0 on success
  python3 test_claim_retake.py --control  # no wait (CLAIM_RETAKE_WAIT=0); tests MUST fail
"""
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
# Pinned literals, and every scenario is written against them (see test_claim_grace.py for why).
WAIT, GRACE, TTL = 3600, 600, 3600
os.environ["CLAIM_RETAKE_WAIT"] = "0" if CONTROL else str(WAIT)
os.environ["CLAIM_GRACE"] = str(GRACE)
os.environ["CLAIM_TTL"] = str(TTL)
os.environ["CLAIM_OPEN_MAX"] = "0"          # the per-key cap is tested in test_claim_cap.py, not here

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()
server.provable_tip = lambda: 1_000
server.witness_available = lambda h: True

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def claim(pk):
    code, r = server.claim({"pubkey": pk, "handle": "w" + pk[:4]})
    return r.get("range") if code == 200 else f"HTTP {code}"


def q(sql, args=()):
    c = server.db()
    rows = c.execute(sql, args).fetchall()
    c.commit()
    c.close()
    return rows


def reset():
    q("DELETE FROM ranges")
    q("DELETE FROM vranges")


HOG, OTHER = "dd" * 32, "bb" * 32
NOW = time.time()

print("== the lowest-free scan ==")
reset()
first = claim(HOG)
q("UPDATE ranges SET claimed_at=? WHERE id=?", (NOW - GRACE - 60, first))
again = claim(HOG)
check(again != first, f"a key whose never-beaten claim on {first} lapsed is given another block, not {first} again ({again})")
check(claim(OTHER) == first, f"every other key is offered block {first} as soon as the grace ends")

print("== the frontier re-offer (#281) ==")
reset()
# Block 1 is covered by a proof that does not seam, so claim() re-offers it before any other block.
# It has been there an hour: #339 re-offers only a cover the frontier snapshot had already seen.
q("INSERT INTO vranges(id, lo, hi, pubkey, handle, ts) VALUES('bad-1', 1, 1, 'x', 'x', ?)", (NOW - 3600,))
real_frontier = server._frontier_snapshot
server._frontier_snapshot = lambda: (time.time(), (0, server.GENESIS_TIP, 0, 0))
try:
    blocker = claim(HOG)
    check(blocker == "1", f"the frontier block is offered first ({blocker})")
    q("UPDATE ranges SET claimed_at=? WHERE id='1'", (NOW - GRACE - 60,))
    again = claim(HOG)
    check(again != "1", f"once its claim lapsed unworked, the same key is not given the frontier block back ({again})")
    got = claim(OTHER)
    check(got == "1", f"another key is ({got})")
finally:
    server._frontier_snapshot = real_frontier

print("== what the wait does not cover ==")
reset()
b = claim(HOG)
q("UPDATE ranges SET claimed_at=?, last_beat=? WHERE id=?", (NOW - TTL - 300, NOW - TTL - 60, b))
check(claim(HOG) == b, f"a claim that DID beat and then stopped past CLAIM_TTL may be taken again by the same key ({b})")
reset()
b = claim(HOG)
q("UPDATE ranges SET claimed_at=? WHERE id=?", (NOW - WAIT - 60, b))
check(claim(HOG) == b, f"after CLAIM_RETAKE_WAIT the same key may take its old block again ({b})")
reset()
b = claim(HOG)
q("UPDATE ranges SET claimed_at=? WHERE id=?", (NOW - 60, b))
check(claim(OTHER) != b, f"a claim still inside the grace keeps its block from everyone ({b})")

if CONTROL:
    if fails:
        print(f"CONTROL OK — no wait, and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the wait was off and every test still passed.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("a key does not take back a block it let lapse unworked; every other key is offered it at once.")

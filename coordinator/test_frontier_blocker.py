#!/usr/bin/env python3
"""
#281 follow-up: a block that COVERS the frontier's next block but cannot seam onto it must still be
handed out, and must register as a stall.

THE HOLE THIS CLOSES, stated plainly because the first fix did not close it:

#283 refuses a wide range whose bounds start inside coverage it is not backed by. That kills the shape
that froze the board on 2026-09-11. It does NOT kill the failure, because the same symptom is reachable
another way: a proof of the right HEIGHT against the wrong predecessor state — a fork, or a stale
bundle after a reorg. At width 1 that is trivial to submit and nothing rejects it, and nothing should:
proving out of order is the entire design, and the coordinator cannot know which predecessor is real.

The result is identical. The block is `proven`, so `claim()` — which reads COVERAGE — walks past it.
The row over it is `verified`, so `stalled_for` reported 0. The frontier stops, `proven` keeps
climbing, and every signal stays green.

So the real fix is not at submit. It is that:
  1. `claim()` must ask what the CHAIN needs, not what the board merely lacks; and
  2. `state()` must treat a verified range over the blocker as a stall, not as health.

WHAT THIS DOES NOT COVER: real STARK verification (VERIFY_MODE=mock), and whether a re-proof of the
blocker actually seams — that depends on the prover getting a correct bundle, which is the bridge's
job, not the coordinator's. What is asserted is that the board keeps OFFERING the block and keeps
SAYING it is stuck, instead of going quiet.

Usage:
  python3 test_frontier_blocker.py            # assertions; exit 0 on success
  python3 test_frontier_blocker.py --control  # restore the old behaviour; these tests MUST fail
"""
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

os.environ["COORD_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.init_db()

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


# The bridge serves every height in these tests; witness availability is a separate concern (#261) and
# letting it gate this would make the test about the wrong thing.
server.witness_available = lambda h: True
server.provable_tip = lambda: 1000


def seed_fork_at(bad):
    """A genesis-anchored run 1..bad-1, then block `bad` proved against the WRONG predecessor.

    Heights are contiguous and every proof is individually valid — this is not a bad-bounds range and
    #283 would not look at it twice. Only the boundary is wrong.
    """
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")

    def put(lo, hi, in_tip, in_b, out_tip, out_b, status="verified", verified_at=None):
        rid = str(lo) if lo == hi else f"{lo}-{hi}"
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,verified_at) VALUES(?,?,?,?,?)",
                  (rid, lo, hi, status, verified_at))
        c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,"
                  "range_work,in_bhash,out_bhash) VALUES(?,?,?,?,?,'','t',0,0,'1',?,?)",
                  (rid, lo, hi, in_tip, out_tip, in_b, out_b))

    prev_tip, prev_b = server.GENESIS_TIP, "b0"
    for h in range(1, bad):
        put(h, h, prev_tip, prev_b, f"t{h}", f"b{h}")
        prev_tip, prev_b = f"t{h}", f"b{h}"
    # the fork: right height, wrong predecessor. Verified an hour ago.
    put(bad, bad, "FORKTIP", "FORKB", f"t{bad}", f"b{bad}", verified_at=time.time() - 3600)
    for h in range(bad + 1, bad + 4):
        put(h, h, f"t{h-1}", f"b{h-1}", f"t{h}", f"b{h}")
    c.commit()
    c.close()
    server._frontier_invalidate()


if CONTROL:
    # Put back exactly what was there before: claim() scanning coverage from block 1 with no notion of
    # the frontier, and stalled_for exempting a verified row. If the assertions still pass with this
    # in place, they are not testing anything.
    server.frontier_hi = lambda: -1            # claim(): _blocker can never match, so coverage wins
    _real_state = server.state

    def _old_state():
        st = _real_state()
        st.pop("blocked", None)                # state(): the key that was computed and never returned
        return st
    server.state = _old_state

print("== a fork at the frontier, not a bad-bounds range ==")
seed_fork_at(6)

check(server._frontier_chain()[0] == 5,
      "the frontier stops at 5 — block 6 proves against the wrong predecessor")

server.state()                      # first read stamps the frontier mark
time.sleep(1.1)

code, got = server.claim({"pubkey": "p" * 64, "handle": "tester", "nonce": "n1"})
check(code == 200, "the coordinator still has work to hand out")
check(got.get("range") == "6",
      f"claim() offers block 6 — the block the CHAIN needs — not the first uncovered one (offered {got.get('range')})")

st = server.state()
check("blocked" in st, "the board PUBLISHES a blocker at all — the key used to be computed and dropped")
blk = st.get("blocked") or {}
check(blk.get("block") == 6, f"it names the block the frontier needs (block={blk.get('block')})")
check(blk.get("id") == "6", "and the range responsible for it")
check((blk.get("stalled_for") or 0) > 0,
      f"it reports a stall rather than zero (stalled_for={blk.get('stalled_for')})")
check((blk.get("stalled_for") or 0) >= 1,
      "the stall keeps counting even though claim() just rewrote the blocking row")

# #285: stalled_for alone stops meaning "wrong" as the frontier climbs -- above ~block 180,000 the
# MEDIAN block is tens of minutes of healthy proving. The judgement has to be published separately.
check(blk.get("needs_attention") is False,
      f"a blocker a live worker is proving does NOT need attention (why={blk.get('why')!r})")

print("== a healthy board is not disturbed ==")
c = server.db()
c.execute("DELETE FROM vranges")
c.execute("DELETE FROM ranges")
prev_tip, prev_b = server.GENESIS_TIP, "b0"
for h in range(1, 9):
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (str(h), h, h))
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,"
              "range_work,in_bhash,out_bhash) VALUES(?,?,?,?,?,'','t',0,0,'1',?,?)",
              (str(h), h, h, prev_tip, f"t{h}", prev_b, f"b{h}"))
    prev_tip, prev_b = f"t{h}", f"b{h}"
c.commit()
c.close()
server._frontier_invalidate()

check(server._frontier_chain()[0] == 8, "an unbroken board has its frontier at the top of the run")
code, got = server.claim({"pubkey": "q" * 64, "handle": "tester2", "nonce": "n2"})
check(got.get("range") == "9", "the next block is offered exactly once — no re-proving of covered work")
check((server.state().get("blocked") or {}).get("stalled_for") == 0,
      "and a board that is merely working reports no stall")

print("== the unseamable cover DOES need attention ==")
seed_fork_at(6)
# Nobody claims it this time: the fork range sits over the blocker as `verified`, which is the #281
# shape -- the one case that can never resolve itself no matter how long anyone waits.
blk = (server.state().get("blocked") or {})
check(blk.get("needs_attention") is True,
      f"a VERIFIED range over the blocker needs attention (why={blk.get('why')!r})")
check("seam" in (blk.get("why") or ""), "...and says why, in terms of the seam that cannot form")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — restored the old claim/stall behaviour and {len(fails)} assertion(s) failed.")
        sys.exit(0)
    print("CONTROL FAILED — the old behaviour was restored and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("the frontier's blocker is offered and reported, and a healthy board is untouched.")

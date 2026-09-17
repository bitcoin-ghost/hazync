#!/usr/bin/env python3
"""
#360: the blocked-block warning must judge the CURRENT claim, not the block's handover history.

WHY THIS EXISTS. The test that decided whether a held blocker is "waiting" or "stuck" was:

    _continuous = _claim_age >= stalled_for - CLAIM_TTL

`claim()` does INSERT OR REPLACE with claimed_at=now, so every hand-over resets `_claim_age` while
`stalled_for` keeps climbing from the frontier's own high-water mark. After ONE hand-over the
expression can never be true again for the rest of that block's life, so the "large block, not a
stall" exemption became unreachable and the current holder was accused however well it was proving.

Measured on the live board 2026-09-17: block 74,928 (814 inputs) held the frontier ~10 h. The holder
had taken it 2h37m earlier and was beating every ~50 s, and had four completed blocks of 2.3-3.3 h
behind it — every one verified. The board reported "may be failing on a bug already fixed". That
message cost three successive wrong diagnoses in one night.

⚠ THE FIX MUST NOT UNDO #286. Block 39,413 really was stuck: a 757 s assembly against a 600 s silence
timeout, killed and re-claimed in a loop for five hours, heartbeating the whole time. "Is it beating"
alone would call that healthy. What separates them is whether THIS claim has outlived a full claim
cycle: a kill-retry loop never does, a worker grinding a big block does. So the test is SETTLED AND
LIVE, and scenario 3 below is the 39,413 regression guard.

WHAT THIS DOES NOT COVER: no proving, no GPU, no real verification (VERIFY_MODE=mock). Rows are
written the way the handlers write them, and `state()` is driven for real.

Usage:
  python3 test_blocker_message.py            # assertions; exit 0 on success
  python3 test_blocker_message.py --control  # restores the latching _continuous test; MUST fail
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

# ⛔ PIN THE CLAIM CYCLE and build every age below from this literal, not from server.CLAIM_TTL.
# test_claim_grace.py records why: deriving the scenarios from a constant the control also moves lets
# the control pass for a reason that has nothing to do with the change under test.
TTL = 3600
server.CLAIM_TTL = TTL

# The bridge serves every height here; witness availability is a separate concern (#261).
server.witness_available = lambda h: True
server.provable_tip = lambda: 100000

if CONTROL:
    server._CONTROL_BLOCKER_CONTINUOUS = True

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


def scenario(fr, stalled_for, claim_age, beat_age, handle="a prover"):
    """A genesis-anchored chain 1..fr, with fr+1 CLAIMED at the given ages. Returns state()['blocked'].

    `stalled_for` is driven through meta.frontier_mark, which is how state() measures it — the
    blocking row's own timestamps cannot express it, because claim() resets them on every hand-over.
    """
    now = time.time()
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")
    prev_tip, prev_b = server.GENESIS_TIP, "b0"
    for h in range(1, fr + 1):
        rid = str(h)
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,verified_at) VALUES(?,?,?,'verified',?)",
                  (rid, h, h, now - 1))
        c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,"
                  "range_work,in_bhash,out_bhash) VALUES(?,?,?,?,?,'','t',0,0,'1',?,?)",
                  (rid, h, h, prev_tip, f"t{h}", prev_b, f"b{h}"))
        prev_tip, prev_b = f"t{h}", f"b{h}"
    nb = fr + 1
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at,last_beat)"
              " VALUES(?,?,?,'claimed','pk_test',?,?,?)",
              (str(nb), nb, nb, handle, now - claim_age, now - beat_age))
    c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('frontier_mark',?)", (f"{fr}:{now - stalled_for}",))
    c.commit()
    c.close()
    server._frontier_invalidate()
    st = server.state()
    assert st["progress"]["frontier"] == fr, f"scenario frontier is {st['progress']['frontier']}, expected {fr}"
    return st["blocked"]


print("1. a big block under ONE long claim, still beating (block 55,862's shape)")
b = scenario(fr=40, stalled_for=9830, claim_age=9830, beat_age=30)
check(b["needs_attention"] is False, "a settled, beating claim is not flagged")
check("not a stall" in b["why"], "and is described as a large block, not a stall")

print("2. RE-TAKEN, but the current holder is settled and beating (block 74,928's shape) -- #360")
b = scenario(fr=50, stalled_for=36000, claim_age=9425, beat_age=41, handle="G H O S T")
check(b["needs_attention"] is False,
      "a healthy claim on a re-taken block is NOT flagged (the whole point of #360)")
check("may be failing on a bug already fixed" not in b["why"],
      "and the holder is not accused of running a buggy worker")

print("3. claims turning over faster than a cycle, still beating (block 39,413's shape) -- #286 guard")
b = scenario(fr=60, stalled_for=20000, claim_age=700, beat_age=30)
check(b["needs_attention"] is True, "churning claims are still flagged")
check("keep turning over" in b["why"], "and the reason names the churn, not the holder's version")

print("4. a settled claim whose heartbeat went stale")
b = scenario(fr=70, stalled_for=20000, claim_age=9000, beat_age=5000)
check(b["needs_attention"] is True, "a stale heartbeat is still flagged")
check("no proving progress is landing" in b["why"], "and the reason names the dead heartbeat")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK: the old _continuous test fails {len(fails)} assertion(s), as it must")
        sys.exit(0)
    print("CONTROL BROKEN: the old _continuous test passed everything — this suite proves nothing")
    sys.exit(1)

if fails:
    print(f"FAILED ({len(fails)}): " + "; ".join(fails))
    sys.exit(1)
print("all assertions passed")

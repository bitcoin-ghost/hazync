#!/usr/bin/env python3
"""
The chain-wide folded count, and the per-row `fold` flag the block map reads.

WHY THIS EXISTS. The home page states "X% of Bitcoin's history is proven, X% is folded, X% is anchored",
so it needs BLOCKS THAT HAVE BEEN FOLDED as a share of the chain. Three ways to get that wrong, and two
of them were live:

  * counting fold OPERATIONS. The leaderboard's `folded` column does `+= 1` per range -- 8,393 today
    against 9,854 blocks. Right for a leaderboard, wrong for a percentage of the chain.
  * calling any WIDE range a fold. A wide range is a fold only if two already-verified ranges tile it
    exactly; one that broke new ground was proved outright, however wide. `blockmap.js` judged by width
    and so coloured the #281 overlap ranges -- 250 blocks of real proving -- as folds.
  * computing it from /api/vranges. That payload is ordered by `lo`, and the test asks what was already
    verified WHEN A RANGE ARRIVED. A fold's children sort after it, so every seam test fails and the
    answer is zero. Measured: zero. This is why the coordinator publishes the flag.

WHAT THIS DOES NOT COVER: real STARK verification (VERIFY_MODE=mock). The fold test is pure bookkeeping
over the vranges table and does not touch a proof.

  python3 test_folded_count.py            # must PASS
  python3 test_folded_count.py --control  # width-based test restored; MUST FAIL
"""
import os
import sys
import tempfile

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


def seed(ranges):
    """Verified ranges in SUBMISSION order -- ts is what separates a fold from a proof."""
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")
    for i, (lo, hi) in enumerate(ranges):
        rid = str(lo) if lo == hi else f"{lo}-{hi}"
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (rid, lo, hi))
        c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,"
                  "range_work) VALUES(?,?,?,?,?,'','t',?,0,'0')",
                  (rid, lo, hi, f"in{lo}", f"out{hi}", i))          # ts = arrival order
    c.commit()
    c.close()


if CONTROL:
    # Put back the test the block map used: any wide range is a fold. It cannot tell a fold from a
    # range that was simply proved wide, which is the whole point.
    server.is_fold_seam = lambda lo, hi, by_start, ends_at: True

print("== a real fold tree ==")
# four blocks proved singly, folded in pairs, then once more into [1..4]
seed([(1, 1), (2, 2), (3, 3), (4, 4), (1, 2), (3, 4), (1, 4)])
check(server.folded_count() == 4,
      f"all four blocks count once, however deep the tree (got {server.folded_count()})")

print("== a wide range that was PROVED, not folded ==")
# [10..20] arrives with nothing beneath it: new ground, proved outright
seed([(10, 20)])
check(server.folded_count() == 0,
      f"a wide range with no children underneath it is not a fold (got {server.folded_count()})")

# the #281 shape: a wide range over ground another wide range already covered
seed([(30, 40), (41, 50), (30, 50)])
check(server.folded_count() == 21,
      f"...but one that tiles two earlier ranges exactly IS a fold (got {server.folded_count()})")

print("== blocks, not operations ==")
seed([(1, 1), (2, 2), (3, 3), (4, 4), (1, 2), (3, 4), (1, 4)])
c = server.db()
ops = len(server.fold_spans(c))
c.close()
check(ops == 3 and server.folded_count() == 4,
      f"3 fold operations over 4 blocks, counted separately (ops={ops}, blocks={server.folded_count()})")

print("== published where the pages can read it ==")
seed([(1, 1), (2, 2), (1, 2)])
st = server.state()
check(st["progress"].get("folded") == 2,
      f"progress.folded is in /api/state (got {st['progress'].get('folded')!r})")
c = server.db()
vr = server.build_vranges(c, set())
c.close()
flagged = {(v["lo"], v["hi"]) for v in vr if v.get("fold")}
check(flagged == {(1, 2)},
      f"only the fold carries the flag, and singles do not (flagged {sorted(flagged)})")
check(all("fold" not in v for v in vr if v["lo"] == v["hi"]),
      "the flag is omitted rather than sent false on every row")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — width-based fold test restored and {len(fails)} assertion(s) failed.")
        sys.exit(0)
    print("CONTROL FAILED — the width test was restored and everything still passed.")
    sys.exit(1)
if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("folded counts blocks, only real folds count, and the flag is published.")

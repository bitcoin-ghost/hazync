#!/usr/bin/env python3
"""
Tests for the proved / folded / anchored split on the leaderboard.

WHY THIS EXISTS. The board showed one number per contributor — distinct blocks covered by any range
they submitted. A fold submits a WIDE range, so folding credited the folder with blocks other people
proved. Measured on the live board before this change: the columns summed to 43,868 against 42,711
blocks actually proven, 1,157 counted twice — once for the prover, once for the folder. Anchoring,
meanwhile, counted for nothing at all, so 5,000-odd spine absorptions were invisible.

THE WHOLE DIFFICULTY IS TELLING A FOLD FROM A PROOF, because both are a wide row in `vranges`. The
submit gate already draws the line — a range either covers fresh territory or is exactly tiled by
ranges already on the board — and only the second is a fold. `fold-range` is BINARY, so the test is a
seam: some earlier range ends where another earlier range begins, inside the span.

Getting it wrong is silent and plausible either way. Call a proof a fold and a contributor loses
blocks they really proved (bip-448's five ranges from the #281 overlap, ~250 blocks). Call a fold a
proof and the old double-count comes straight back.

WHAT THIS DOES NOT COVER: no proving, no verification, no GPU. Rows are written by hand in the shapes
the coordinator writes them.

Usage:
  python3 test_contributions.py            # assertions; exit 0 on success
  python3 test_contributions.py --control  # treat every wide range as a fold; the tests MUST fail
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()

if CONTROL:
    # The break: every wide range is a fold. This is the intuitive rule, it is what the shape of the
    # row suggests, and it is wrong for exactly the ranges somebody broke new ground with.
    server.is_fold_seam = lambda lo, hi, by_start, ends_at: True

fails = []
def check(ok, what):
    if ok:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)

AA, BB, CC = "aa" * 32, "bb" * 32, "cc" * 32
T = [1000.0]
def vr(lo, hi, pk):
    T[0] += 1
    c = server.db()
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
              " VALUES(?,?,?,'','',?,?,?,0,'0')", (f"{lo}-{hi}", lo, hi, pk, {AA: "alice", BB: "bob", CC: "cara"}[pk], T[0]))
    c.commit(); c.close()
def spine(hi, pk):
    T[0] += 1
    c = server.db()
    c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
              " VALUES(?,?,?,'sha','sig',1,'note',?)", (f"spine:1-{hi}", pk, "x", T[0]))
    c.commit(); c.close()

# alice proves four single blocks; bob folds them in pairs, then cara folds the pair-of-pairs. Nobody
# but alice proved anything, and the fold work belongs to bob and cara.
for h in (1, 2, 3, 4):
    vr(h, h, AA)
vr(1, 2, BB)
vr(3, 4, BB)
vr(1, 4, CC)
# cara breaks fresh ground with a WIDE range — a direct multi-block proof, not a fold.
vr(10, 20, CC)
# NB no second fold of [1..4]: vranges is keyed by range id and submit() answers 409 for a span that
# is already verified, so two people cannot both own the same fold. Writing one here would silently
# REPLACE the other's row and test a state the API cannot reach.
spine(4, AA)
spine(4, AA)     # a repeated head: still an absorption attempt by alice
spine(20, BB)

d = server.contributions_by_pubkey()
g = lambda pk, k: d.get(pk, {}).get(k, 0)  # noqa: E731

check(g(AA, "proved") == 4, f"single-block proofs count as proved (alice: {g(AA,'proved')}, want 4)")
check(g(AA, "folded") == 0, "a prover who folded nothing has no folds")
check(g(BB, "folded") == 2, f"each fold counts once (bob: {g(BB,'folded')}, want 2)")
check(g(BB, "proved") == 0,
      f"A FOLDER IS CREDITED NO BLOCKS — they proved none of them (bob: {g(BB,'proved')}, want 0)")
check(g(CC, "folded") == 1, f"cara's [1..4] is a fold (got {g(CC,'folded')})")
check(g(CC, "proved") == 11,
      f"cara's fresh [10..20] is a PROOF of 11 blocks, not a fold (got {g(CC,'proved')})")
check(g(AA, "anchored") == 2 and g(BB, "anchored") == 1,
      f"absorptions are counted per contributor (alice {g(AA,'anchored')}, bob {g(BB,'anchored')})")
check(g(CC, "anchored") == 0, "someone who never absorbed has no anchors")
check(g(BB, "proved") + g(CC, "proved") + g(AA, "proved") == 15,
      "the proved column counts each block once per contributor who proved it, and no folds")

# A contributor who ONLY folds, or ONLY anchors, must still appear — they were invisible before.
check(set(d) >= {AA, BB, CC}, "everyone who did any of the three kinds appears")


if CONTROL:
    if fails:
        print(f"CONTROL OK — every wide range treated as a fold and {len(fails)} assertion(s) failed.")
        sys.exit(0)
    print("CONTROL FAILED — proofs were misread as folds and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("proved / folded / anchored are counted as the work each contributor actually did.")

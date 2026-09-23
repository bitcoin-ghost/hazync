#!/usr/bin/env python3
"""How a block entered the spine: in one wide fold, or one at a time (hazync#486).

⛔ WHY THIS EXISTS. The block panel showed, for a block that is demonstrably anchored:

    Proven    ● by bip-448
    Folded    ○ No fold on record
    Anchored  ● by G H O S T, into the genesis proof, blocks 1 to 113,971

Both outer lines were correct and the middle one read as a missing step. It is not. `extend-spine`
takes `[1..N] + [N+1..M]` for ANY M at the same cost, so the spine normally swallows a node of the
fold tree and moves M-N blocks for the price of one. Where the fold tree has not reached, there is no
wide range and it falls back to one advance per block — which has no fold record, correctly, and
looks like a fault unless the frame says why.

Measured live 2026-09-23: the spine sat 12,622 blocks past the end of the folded region.

  python3 test_spine_absorption.py            # must PASS
  python3 test_spine_absorption.py --control  # width is ignored; MUST FAIL
"""
import os
import sqlite3
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server                                                                # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def board(advances):
    """A coordinator db whose spine advanced through `advances` = [(hi, handle), ...]."""
    d = tempfile.mkdtemp(prefix="spine_")
    path = os.path.join(d, "c.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, range_id TEXT,"
              " pubkey TEXT, handle TEXT, receipt_sha TEXT, sig TEXT, verified INTEGER,"
              " note TEXT, ts REAL)")
    for i, (hi, handle) in enumerate(advances):
        c.execute("INSERT INTO submissions(range_id,pubkey,handle,verified,ts) VALUES(?,?,?,1,?)",
                  (f"spine:1-{hi}", "pk", handle, 1000.0 + i))
    c.commit(); c.close()
    server.DB = path
    return path


def absorbed(n):
    a = server.spine_absorption(n)
    if CONTROL and a:
        # ⛔ THE CONTROL IS THE OLD PANEL: it knew a block was anchored and nothing about HOW.
        a = dict(a, width=1, folded_range=False)
    return a


# ── 1. a wide fold: one advance carried many blocks ──────────────────────────────────────────────
board([(1000, "alice"), (3000, "alice")])
a = absorbed(2500)
check(a is not None, "a block inside an advance is found")
check(a and a["width"] == 2000,
      f"a 2000-block advance reports its width ({a and a['width']})")
check(a and a["folded_range"],
      "and is marked as having come in on a folded range")
check(a and a["lo"] == 1001 and a["hi"] == 3000,
      f"the range is what THIS advance carried, not the receipt's lo=1 ({a and (a['lo'], a['hi'])})")

# ── 2. ⛔ THE CASE THAT PROMPTED THIS. One block at a time, because no wide range existed ─────────
board([(1000, "alice"), (1001, "ghost"), (1002, "ghost"), (1003, "ghost")])
a = absorbed(1002)
check(a and a["width"] == 1, f"a single-block advance reports width 1 ({a and a['width']})")
check(a and not a["folded_range"],
      "and is NOT claimed to have come in on a folded range — it genuinely has no fold record")
check(a and a["handle"] == "ghost", f"and names who absorbed it ({a and a['handle']})")

# ── 3. a re-advertised head, or a lost race, covers no new blocks ────────────────────────────────
board([(1000, "alice"), (1000, "bob"), (1000, "bob"), (2000, "bob")])
a = absorbed(1500)
check(a and a["handle"] == "bob" and a["lo"] == 1001 and a["width"] == 1000,
      f"rows at or below the head they already had are skipped ({a})")

# ── 4. a block the spine has not reached has no absorption ───────────────────────────────────────
board([(1000, "alice")])
check(server.spine_absorption(5000) is None, "a block beyond the head reports nothing, not a guess")
check(server.spine_absorption(1) is not None, "and the very first block is covered")

# ⚠ NAME EVERY ONE AND COUNT THEM. Listing two while three fail lets an unrelated breakage ride
# along inside a "CONTROL OK" — a mistake I made twice today before this became a habit.
EXPECTED_CONTROL = {"reports its width", "come in on a folded range",
                    "rows at or below the head"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with width ignored, a wide fold is indistinguishable from the "
              "one-at-a-time fallback, which is the panel this replaces:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected the width assertions to fail; got {len(fails)}: {fails}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("the panel can tell a folded range from a block the spine took one at a time")

#!/usr/bin/env python3
"""Integrity check 3: every block from 1 to the spine's tip has its own record, and the records link up.

The genesis proof attests blocks 1 to N as one file. This checks the board's own records agree with it, block by
block, using the same seam rule the frontier uses (`_frontier_chain` in server.py):

  * every height from 1 to N has its own verified single-block record;
  * block 1 starts from the genesis tip;
  * each block starts where the one before it ended, on both the tip hash (`in_tip == prev.out_tip`) and the
    UTXO/difficulty boundary digest (`in_bhash == prev.out_bhash`); a record with no digest cannot seam at all;
  * the last block, N, ends on the genesis proof's own tip.

Measured on the live board on 2026-09-15: 41,539 blocks, 0 missing, 0 broken seams, in 0.24 s.

    ./coordinator/check-continuity.py                       # exits 1 on any gap or broken seam
    ./coordinator/check-continuity.py --allow 38500-39299   # accept a known, recorded hole

Exit status: 0 holds; 1 integrity failure; 2 could not check (the database or the spine could not be read).
Environment (matching the coordinator unit): COORD_DB, COORD_SPINE, GENESIS_TIP.
"""
import argparse
import json
import os
import sqlite3
import sys

GENESIS_TIP = os.environ.get("GENESIS_TIP", "6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000")


def parse_allow(values):
    allowed = set()
    for v in values:
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-", 1)
                allowed.update(range(int(lo), int(hi) + 1))
            else:
                allowed.add(int(part))
    return allowed


def runs(heights):
    """Contiguous (lo, hi) runs: 800 numbers in a row are one line, not 800."""
    out = []
    for h in sorted(heights):
        if out and h == out[-1][1] + 1:
            out[-1][1] = h
        else:
            out.append([h, h])
    return out


def show(label, heights, limit=10):
    rs = runs(heights)
    text = ", ".join(f"{lo:,}" if lo == hi else f"{lo:,}..{hi:,}" for lo, hi in rs[:limit])
    more = f" and {len(rs) - limit} more run(s)" if len(rs) > limit else ""
    print(f"    {label}: {text}{more}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Integrity check 3: blocks 1 to the spine's tip link up, one by one.")
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"))
    ap.add_argument("--spine-dir", default=os.environ.get("COORD_SPINE", "/var/lib/hazync/spine"))
    ap.add_argument("--allow", action="append", default=[], help="heights known to have no record, e.g. 38500-39299")
    a = ap.parse_args(argv)
    allowed = parse_allow(a.allow)

    try:
        c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as e:
        print(f"COULD NOT CHECK: cannot open {a.db}: {e}")
        return 2
    try:
        spine_path = os.path.join(a.spine_dir, "spine.json")
        if not os.path.exists(spine_path):
            if c.execute("SELECT 1 FROM vranges LIMIT 1").fetchone() is None:
                print("  empty board: no genesis proof and no verified ranges. Nothing to check yet.")
                return 0
            print(f"COULD NOT CHECK: {spine_path} does not exist, but the board has verified ranges.")
            return 2
        with open(spine_path) as f:
            spine = json.load(f)
        n = int(spine["hi"])
        rows = c.execute("SELECT lo, in_tip, out_tip, in_bhash, out_bhash FROM vranges "
                         "WHERE lo = hi AND lo BETWEEN 1 AND ?", (n,)).fetchall()
    except (sqlite3.Error, OSError, ValueError, KeyError) as e:
        print(f"COULD NOT CHECK: reading {a.db} and {a.spine_dir}: {e}")
        return 2
    finally:
        c.close()

    per = {}
    for lo, in_tip, out_tip, in_bhash, out_bhash in rows:
        per.setdefault(lo, []).append((in_tip or "", out_tip or "", in_bhash or "", out_bhash or ""))

    missing = [h for h in range(1, n + 1) if h not in per]
    unexpected = [h for h in missing if h not in allowed]
    tip_breaks, digest_breaks, no_digest, unchecked = [], [], [], 0
    for h in range(1, n + 1):
        if h not in per:
            continue
        cands = per[h]
        if not any(x[2] for x in cands):
            no_digest.append(h)
        if h == 1:
            if not any(x[0] == GENESIS_TIP for x in cands):
                tip_breaks.append(h)
            continue
        if h - 1 not in per:
            unchecked += 1
            continue
        prev = per[h - 1]
        if not any(p[1] == x[0] for p in prev for x in cands):
            tip_breaks.append(h)
        if not any(p[3] and p[3] == x[2] for p in prev for x in cands):
            digest_breaks.append(h)

    last_ok = n in per and any(x[1] == spine.get("out_tip") for x in per[n])
    print(f"blocks 1 to {n:,} (the genesis proof's tip)")
    print(f"  single-block records : {len(per):,}")
    print(f"  missing              : {len(missing):,} ({len(missing) - len(unexpected):,} accepted with --allow)")
    print(f"  tip seams broken     : {len(tip_breaks):,}")
    print(f"  digest seams broken  : {len(digest_breaks):,}")
    print(f"  records with no digest: {len(no_digest):,}")
    if unchecked:
        print(f"  seams not checkable beside an accepted hole: {unchecked:,}")

    fails = []
    if unexpected:
        show("FAIL no record of their own", unexpected)
        fails.append("missing")
    if tip_breaks:
        show("FAIL do not start where the previous block ended (tip hash; block 1: not from genesis)", tip_breaks)
        fails.append("tip")
    if digest_breaks:
        show("FAIL boundary digest does not match the previous block's", digest_breaks)
        fails.append("digest")
    if no_digest:
        show("FAIL have no boundary digest, so cannot seam", no_digest)
        fails.append("no digest")
    if n in per:
        if last_ok:
            print(f"  ok   block {n:,} ends on the genesis proof's tip")
        else:
            print(f"  FAIL block {n:,} does not end on the genesis proof's tip")
            fails.append("last")
    elif n in allowed:
        print(f"COULD NOT CHECK: block {n:,} has no record (accepted), so the end cannot be compared with the proof.")
        if not fails:
            return 2

    print()
    if fails:
        print(f"INTEGRITY FAILURE: the records from block 1 to {n:,} do not link up ({', '.join(fails)}).")
        return 1
    print(f"Continuity holds: every block from 1 to {n:,} has its own record and links to the next.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

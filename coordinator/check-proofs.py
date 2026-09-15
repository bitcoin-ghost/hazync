#!/usr/bin/env python3
"""Integrity check 2: every stored proof still hashes to what was accepted, and still verifies to its record.

Every proof is verified once, at submission. The files then sit on one disk for good: served by "Check this block's
own proof", the basis of attribution, and what a spine rebuild would need. A flipped bit, a bad copy or a bad restore
would change nothing visible until someone asked for that proof. This runs nightly and, for every
`proof_<id>.bin` in COORD_PROOFS, checks that:

  1. its sha256 equals `ranges.receipt_sha`, the hash of what the coordinator accepted;
  2. `hazync-host verify-any` still accepts it; and
  3. what it verifies to (lo, hi, in_tip, out_tip, in_bhash, out_bhash) is what its `vranges` record says.

Measured on the coordinator on 2026-09-15: one verify takes 34-54 ms (6 single-block proofs and 3 folds), and there
were 103,992 proof files, so a full pass is about 90 CPU-minutes, about 25 minutes on 4 workers. Hashing 2,000 files
took 2.9 s. The run prints its own total, which is the measurement for later passes.

    ./coordinator/check-proofs.py                 # all of them
    ./coordinator/check-proofs.py --limit 500     # a sample

Exit status: 0 every proof holds; 1 at least one does not; 2 could not check (no database, no proof directory, no
host binary). Verified ranges with no file are counted and listed, not failed: per-block ones are the G1 retention
check's job, and whether a missing fold file matters is not settled.
Environment: COORD_DB, COORD_PROOFS, HAZYNC_HOST, CHECK_PROOFS_WORKERS (4), CHECK_PROOFS_MIN_AGE (600 s).
"""
import argparse
import concurrent.futures
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import time

NAME = re.compile(r"^proof_(\d+(?:-\d+)?)\.bin$")
FIELDS = ("lo", "hi", "in_tip", "out_tip", "in_bhash", "out_bhash")


def range_ok(text):
    """The key=value fields of verify-any's RANGE-OK line, or None."""
    for line in (text or "").splitlines():
        if line.startswith("RANGE-OK "):
            return dict(p.split("=", 1) for p in line.split()[1:] if "=" in p)
    return None


def check_one(path, rid, want_sha, record, host):
    """(id, [problems]). Never raises: anything that goes wrong with one file is a problem with that file."""
    problems = []
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return rid, [f"cannot be read: {e}"]
    if want_sha is None:
        problems.append("has no verified record in `ranges`")
    elif hashlib.sha256(data).hexdigest() != want_sha:
        problems.append("no longer hashes to the receipt the coordinator accepted")
    try:
        p = subprocess.run([host, "verify-any", path], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return rid, problems + ["verify-any did not finish in 300 s"]
    kv = range_ok(p.stdout)
    if p.returncode != 0 or kv is None:
        first = ((p.stderr or "") + "\n" + (p.stdout or "")).strip().splitlines()[:1]
        problems.append(f"verify-any rejects it (exit {p.returncode}): {first[0][:160] if first else ''}")
    elif record is None:
        problems.append("verifies, but has no `vranges` record to compare with")
    else:
        for field in FIELDS:
            if str(kv.get(field, "")) != str(record[field]):
                problems.append(f"verifies to {field}={str(kv.get(field))[:20]}, its record says "
                                f"{str(record[field])[:20]}")
    return rid, problems


def main(argv=None):
    ap = argparse.ArgumentParser(description="Integrity check 2: stored proofs still hash and verify to their records.")
    ap.add_argument("--db", default=os.environ.get("COORD_DB", "/var/lib/hazync/coordinator.db"))
    ap.add_argument("--proofs", default=os.environ.get("COORD_PROOFS", "/var/lib/hazync/proofs"))
    ap.add_argument("--host", default=os.environ.get("HAZYNC_HOST", "/usr/local/bin/hazync-host"))
    ap.add_argument("--workers", type=int, default=int(os.environ.get("CHECK_PROOFS_WORKERS", "4")))
    ap.add_argument("--min-age", type=float, default=float(os.environ.get("CHECK_PROOFS_MIN_AGE", "600")),
                    help="skip files written in the last N seconds (a proof arriving now)")
    ap.add_argument("--limit", type=int, default=0, help="check at most N files (0: all)")
    a = ap.parse_args(argv)

    if not os.path.isdir(a.proofs):
        print(f"COULD NOT CHECK: proof directory {a.proofs} does not exist")
        return 2
    if not (os.path.isfile(a.host) and os.access(a.host, os.X_OK)):
        print(f"COULD NOT CHECK: no executable host binary at {a.host}")
        return 2

    t0 = time.monotonic()
    cutoff = time.time() - a.min_age
    # List the files BEFORE reading the database: the coordinator commits a range and then writes its file, so any
    # file listed here already has its record committed. The other order could report a brand-new proof as orphaned.
    files = []
    for name in sorted(os.listdir(a.proofs)):
        m = NAME.match(name)
        if not m:
            continue
        path = os.path.join(a.proofs, name)
        try:
            if os.path.getmtime(path) > cutoff:
                continue
        except OSError:
            continue
        files.append((path, m.group(1)))
    if a.limit:
        files = files[:a.limit]

    try:
        c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=60)
        c.row_factory = sqlite3.Row
        sha = {r["id"]: r["receipt_sha"] for r in c.execute("SELECT id, receipt_sha FROM ranges WHERE status='verified'")}
        rec = {r["id"]: dict(r) for r in c.execute("SELECT id, lo, hi, in_tip, out_tip, in_bhash, out_bhash FROM vranges")}
        c.close()
    except sqlite3.Error as e:
        print(f"COULD NOT CHECK: cannot read {a.db}: {e}")
        return 2

    total, bad, done = len(files), {}, 0
    print(f"checking {total:,} stored proofs with {a.workers} worker(s)")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futures = [pool.submit(check_one, path, rid, sha.get(rid), rec.get(rid), a.host) for path, rid in files]
        for fut in concurrent.futures.as_completed(futures):
            rid, problems = fut.result()
            done += 1
            if problems:
                bad[rid] = problems
            if done % 10000 == 0:
                print(f"  {done:,}/{total:,} checked, {len(bad):,} with problems, {time.monotonic() - t0:.0f} s")

    have = {rid for _, rid in files}
    no_file = sorted(rid for rid in sha if rid not in have and not os.path.exists(os.path.join(a.proofs, f"proof_{rid}.bin")))
    elapsed = time.monotonic() - t0
    print(f"  checked              : {total:,} in {elapsed:.0f} s ({total / elapsed:.1f} per second)" if elapsed else "")
    print(f"  hold                 : {total - len(bad):,}")
    print(f"  with problems        : {len(bad):,}")
    print(f"  verified with no file: {len(no_file):,} (not a failure; per-block ones are the G1 check's)"
          + (f", e.g. {', '.join(no_file[:5])}" if no_file else ""))
    for rid in sorted(bad)[:25]:
        print(f"  FAIL proof_{rid}.bin: {'; '.join(bad[rid])}")
    if len(bad) > 25:
        print(f"  ... and {len(bad) - 25:,} more")
    print()
    if bad:
        print(f"INTEGRITY FAILURE: {len(bad):,} stored proof(s) no longer hold.")
        return 1
    print(f"Every one of the {total:,} stored proofs still hashes and verifies to its record.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

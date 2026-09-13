#!/usr/bin/env python3
"""/api/vranges must not stampede, and the coordinator must not run out of open files (2026-09-13).

2026-09-13, ~15:21-15:42 UTC: the board stopped accepting proofs. /api/vranges was a plain 10 s TTL cache
around a rebuild that had grown to ~25 s (69k rows, 5.8 MB). Every request arriving during a rebuild missed
and started its own; ~350 threads rebuilt at once, each holding a connection, the -wal and a temp file,
until the process sat at its soft limit of 1024 open files. From then `sqlite3.connect` failed with
"unable to open database file" and every claim and submit timed out behind it.

  1. vranges_cached recomputes on ONE thread: N concurrent callers during a slow rebuild -> one rebuild,
     cold start included, and a stale cache serves the previous index instead of waiting;
  2. the rebuild's connection is closed explicitly, not left to the garbage collector;
  3. raise_open_file_limit() lifts a soft limit of 1024;
  4. /api/state publishes `folds`, the chain-wide fold count, so a page never needs the full index for it.

WHAT THIS DOES NOT COVER: real STARK verification (VERIFY_MODE=mock), and the systemd unit on the box.

  python3 test_vranges_cache.py            # must PASS
  python3 test_vranges_cache.py --control  # the old plain-TTL cache and no limit raise; MUST FAIL

The control does not remove `folds`, so check 4 passes in both modes; checks 1-3 are what it must fail.
"""
import json
import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
import time

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ["VRANGES_CACHE_TTL"] = "0.2"
os.environ["DB_BUSY_TIMEOUT"] = "1"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
server.init_db()

if CONTROL:
    _old = {"t": 0.0, "v": None, "etag": None}

    def _old_vranges_cached():                # the pre-2026-09-13 cache: plain TTL, no close, no single flight
        now = time.time()
        with server._state_lock:
            if _old["v"] is not None and now - _old["t"] < server.VRANGES_TTL:
                return _old["v"], _old["etag"]
        c = server.db()
        blk = server.blocked_pubkeys()
        payload = {"vranges": server.build_vranges(c, blk), "range_size": server.RANGE_SIZE}
        v = json.dumps(payload).encode()
        etag = '"' + hashlib.sha256(v).hexdigest()[:32] + '"'
        with server._state_lock:
            _old["t"] = time.time(); _old["v"] = v; _old["etag"] = etag
        return v, etag
    server.vranges_cached = _old_vranges_cached
    server.raise_open_file_limit = lambda want=65536: None
    print("CONTROL: the old stampeding cache, no limit raise -- the checks below MUST fail")

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


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
                  (rid, lo, hi, f"in{lo}", f"out{hi}", i))
    c.commit()
    c.close()


# four blocks proved singly, folded in pairs, then once more: 3 fold operations over 4 blocks
seed([(1, 1), (2, 2), (3, 3), (4, 4), (1, 2), (3, 4), (1, 4)])

# 1. single flight: N concurrent callers during a slow rebuild -> one rebuild
builds, inflight, peak = [], [0], [0]
lock = threading.Lock()
real_build = server.build_vranges


def slow_build(c, blk):
    with lock:
        builds.append(1)
        inflight[0] += 1
        peak[0] = max(peak[0], inflight[0])
    try:
        time.sleep(0.8)
        return real_build(c, blk)
    finally:
        with lock:
            inflight[0] -= 1


server.build_vranges = slow_build


def burst(n=20):
    out = []
    ts = [threading.Thread(target=lambda: out.append(server.vranges_cached())) for _ in range(n)]
    for th in ts:
        th.start()
    for th in ts:
        th.join(timeout=60)
    return out


out = burst()
check(len(builds) == 1 and len(out) == 20 and len({o[1] for o in out}) == 1,
      f"cold start: 20 concurrent callers -> 1 rebuild, all served one ETag (rebuilds={len(builds)}, served={len(out)})")
check(peak[0] == 1, f"...never more than one rebuild holding a connection at a time (peak {peak[0]})")
time.sleep(0.3)                                   # past the 0.2 s TTL
builds.clear(); peak[0] = 0
t = time.time()
out = burst()
check(len(builds) == 1, f"stale cache: 20 concurrent callers -> 1 rebuild (got {len(builds)})")
check(time.time() - t < 1.5, f"...and the others got the previous index instead of waiting ({time.time() - t:.2f}s)")
server.build_vranges = real_build

# 2. the rebuild's connection is closed explicitly
opened = []
real_db = server.db


def tracking_db():
    c = real_db()
    opened.append(c)                              # holding a reference: only an explicit close() closes it
    return c


server.db = tracking_db
time.sleep(0.3)
server.vranges_cached()
time.sleep(0.05)
server.db = real_db


def is_closed(c):
    try:
        c.execute("SELECT 1")
        return False
    except sqlite3.ProgrammingError:
        return True


check(len(opened) >= 1 and all(is_closed(c) for c in opened),
      f"every connection a rebuild opens is closed when it finishes ({sum(map(is_closed, opened))} of {len(opened)} closed)")

# 3. a soft open-file limit of 1024 is lifted
import resource  # noqa: E402
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if hard == resource.RLIM_INFINITY or hard > 1024:
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, hard))       # what systemd hands a service
    server.raise_open_file_limit()
    now_soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    want = 65536 if hard == resource.RLIM_INFINITY else min(65536, hard)
    check(now_soft == want, f"raise_open_file_limit lifts a soft limit of 1024 to {want} (now {now_soft})")
else:
    print(f"  skip hard open-file limit is {hard}, nothing to raise it to")

# 4. the fold count is in /api/state
st = json.loads(server.state_cached(slim=True))
folds = st["progress"].get("folds")
check(folds == 3, f"progress.folds is the chain-wide fold count (3 fold operations, got {folds!r})")
check(st["progress"].get("folded") == 4, f"...next to progress.folded, the blocks inside folds (got {st['progress'].get('folded')!r})")
print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)

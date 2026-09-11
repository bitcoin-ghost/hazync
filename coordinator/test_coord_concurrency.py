#!/usr/bin/env python3
"""The coordinator must not jam under concurrent load (#265).

2026-09-11: a 10-card fleet plus board viewers jammed the API -- 148 threads, 504s -- because the DB
was in SQLite's rollback journal (readers block writers) and the state cache stampeded once a recompute
outran its TTL.

  1. init_db() puts the DB in WAL, and a writer is NOT blocked by a reader holding an open read
     transaction (in rollback-journal mode it is);
  2. state_cached / the frontier chain recompute on ONE thread: N concurrent callers during a slow
     recompute trigger exactly one, cold start included.

  python3 test_coord_concurrency.py            # must PASS
  python3 test_coord_concurrency.py --control  # rollback journal + the old stampeding cache; MUST FAIL
"""
import json
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
os.environ["STATE_CACHE_TTL"] = "0.2"
os.environ["DB_BUSY_TIMEOUT"] = "1"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
CONTROL = "--control" in sys.argv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
server.init_db()

if CONTROL:
    c = sqlite3.connect(_tmpdb.name)
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    _cache = {}

    def _old_state_cached(slim=False):        # the pre-#265 cache: plain TTL, compute outside the lock
        key = "slim" if slim else "full"
        e = _cache.get(key)
        if e is not None and time.time() - e["t"] < server.STATE_TTL:
            return e["v"]
        v = json.dumps(server.state(slim=slim)).encode()
        _cache[key] = {"t": time.time(), "v": v}
        return v
    server.state_cached = _old_state_cached
    print("CONTROL: rollback journal + the old stampeding cache -- the checks below MUST fail")

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


# 1. WAL: a reader with an open read transaction must not block a writer
mode = sqlite3.connect(_tmpdb.name).execute("PRAGMA journal_mode").fetchone()[0]
check(mode == "wal", f"init_db() leaves the DB in WAL (got {mode})")
reader = sqlite3.connect(_tmpdb.name)
reader.execute("BEGIN")
reader.execute("SELECT count(*) FROM ranges").fetchone()          # holds a read snapshot / SHARED lock
t = time.time()
try:
    w = server.db()
    w.execute("INSERT INTO ranges(id, lo, hi, status) VALUES ('999999', 999999, 999999, 'open')")
    w.commit()
    w.close()
    wrote = True
except sqlite3.OperationalError as e:
    wrote = False
    print(f"       ({e})")
reader.rollback()
reader.close()
check(wrote and time.time() - t < 1.0, f"a writer is not blocked by an open reader ({time.time() - t:.2f}s)")

# 2. single flight: N concurrent callers during a slow recompute -> exactly one recompute
calls = []
real_state = server.state


def slow_state(slim=False):
    calls.append(1)
    time.sleep(0.8)
    return real_state(slim=slim)


server.state = slow_state


def burst(n=20):
    out = []
    ts = [threading.Thread(target=lambda: out.append(server.state_cached(slim=True))) for _ in range(n)]
    for th in ts:
        th.start()
    for th in ts:
        th.join(timeout=30)
    return out


calls.clear()
out = burst()
check(len(calls) == 1 and len(out) == 20 and len(set(out)) == 1,
      f"cold start: 20 concurrent callers -> 1 recompute, all served (recomputes={len(calls)}, served={len(out)})")
time.sleep(0.3)                                   # let the cached value go stale (TTL 0.2 s)
calls.clear()
t = time.time()
out = burst()
check(len(calls) == 1, f"stale cache: 20 concurrent callers -> 1 recompute (got {len(calls)})")
check(time.time() - t < 2.0, f"...and the others got the previous value instead of waiting ({time.time() - t:.2f}s)")
server.state = real_state

# 3. the frontier chain (every worker's /api/meta) is single-flight too
fcalls = []
real_fc = server._frontier_chain          # the uncached walk; frontier_hi goes through _frontier_chain_cached


def slow_fc():
    fcalls.append(1)
    time.sleep(0.5)
    return real_fc()


server._frontier_chain = slow_fc
server._frontier_invalidate()
ts = [threading.Thread(target=server.frontier_hi) for _ in range(15)]
for th in ts:
    th.start()
for th in ts:
    th.join(timeout=30)
check(real_fc is not None and len(fcalls) == 1, f"15 concurrent /api/meta frontier lookups -> 1 walk (got {len(fcalls)})")

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)

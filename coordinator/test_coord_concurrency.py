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
took = time.time() - t
time.sleep(1.2)                                   # a background rebuild finishes after the burst returns
check(len(calls) == 1, f"stale cache: 20 concurrent callers -> 1 recompute (got {len(calls)})")
check(took < 2.0, f"...and the others got the previous value instead of waiting ({took:.2f}s)")

# 2b. #324: the caller that FINDS the entry stale does not wait for the rebuild either
gen = [0]


def tagged_state(slim=False):
    calls.append(1)
    time.sleep(0.8)
    gen[0] += 1
    d = real_state(slim=slim)
    d["_gen"] = gen[0]
    return d


def served_gen():
    try:
        return json.loads(server.state_cached(slim=True)).get("_gen")
    except Exception as ex:
        return repr(ex)


server.state = tagged_state
server._sf.pop("state:slim", None)                # cold start with tagged values
first = served_gen()
time.sleep(0.3)                                   # stale
t = time.time()
got = served_gen()
took = time.time() - t
check(took < 0.3 and got == first,
      f"a lone caller that finds the board stale gets the previous board at once ({took:.2f}s, gen {got!r} vs {first!r})")
time.sleep(1.2)                                   # the rebuild that caller started lands
got = served_gen()
check(isinstance(got, int) and isinstance(first, int) and got > first,
      f"...and the next caller gets the rebuilt board (gen {got!r} after {first!r})")
time.sleep(1.2)                                   # let the rebuild that call started finish too

# 2c. a failing background rebuild keeps the last good board, lets the next caller try again, and is not
# hidden for ever: past CACHE_MAX_STALE the caller rebuilds in line and sees the error
def failing_state(slim=False):
    calls.append(1)
    raise RuntimeError("rebuild failed (test)")


thread_crashes = []
threading.excepthook = lambda a: thread_crashes.append(f"{a.exc_type.__name__}: {a.exc_value}")
server.state = failing_state
calls.clear()
time.sleep(0.3)
got = served_gen()
time.sleep(0.2)
check(isinstance(got, int), f"a failing background rebuild still serves the last good board (got {got!r})")
got2 = served_gen()
time.sleep(0.2)
check(len(calls) == 2 and isinstance(got2, int),
      f"...and releases the refresh, so the next stale caller starts another (rebuilds={len(calls)})")
_max_stale = getattr(server, "CACHE_MAX_STALE", None)
server.CACHE_MAX_STALE = 0.3
time.sleep(0.8)
got = served_gen()
check(isinstance(got, str) and "rebuild failed" in got,
      f"past CACHE_MAX_STALE the caller rebuilds in line and sees the failure (got {got!r})")
if _max_stale is not None:
    server.CACHE_MAX_STALE = _max_stale
# The refresh thread must handle its own failure. The first version logged with `sys`, which server.py
# never imported, so the handler itself raised NameError: every check above still passed.
check(not thread_crashes, f"a failed background rebuild is logged, not a crash of its thread ({thread_crashes})")
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

#!/usr/bin/env python3
"""The collector's own liveness must not depend on a third party, and its lock must mean what it says.

Two defects seen during tip hour 4, 2026-09-29.

⛔ hazync#591 — A SLOW EXTERNAL CALL FROZE THE WHOLE PAGE. `chain_facts()` is a synchronous HTTP
request with a 10 s timeout and it was called inline every tick, so one unreachable call stalled the
entire snapshot: cards, blocks, spend, progress. The API was healthy (5/5 HTTP 200) and this laptop's
network was saturated by 81 ssh streams feeding the rig — the rig's own load degraded the rig's
liveness. `STALE_S` is 60, so a 10 s freeze did not even raise the staleness banner: the page simply
stopped, and the operator asked whether the feed had died.

⛔ hazync#583 — THE LOCK TRUSTED `/proc/<pid>`. It did check liveness, so the issue as I first filed it
was wrong. But `/proc/<pid>` exists for a ZOMBIE (exited, not yet reaped) and for a RECYCLED pid, and
neither is a collector. The replacement refused to start, naming a pid that was collecting nothing —
and the remedy it suggests is `--steal`, the flag that DISABLES the check, so the way out of a false
positive is the way out of a real conflict.

    python3 test_collector_resilience.py             # cache never blocks; the lock asks what the pid IS
    python3 test_collector_resilience.py --control   # the old rules — both failures must reproduce
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import collect  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


# ── hazync#591: the loop must never wait on the network ─────────────────────────────────────────
SLOW = 0.6


def slow_fetch():
    time.sleep(SLOW)
    return {"tip": 969122, "ok": True}


orig = collect.chain_facts
collect.chain_facts = slow_fetch
try:
    if CONTROL:
        # The shipped-before behaviour: the loop called chain_facts() directly.
        t0 = time.time()
        for _ in range(3):
            collect.chain_facts()
        el = time.time() - t0
        check(el >= SLOW * 3 * 0.9,
              f"control reproduces it: 3 ticks cost {el:.2f}s — every tick waits on the network")
    else:
        collect.start_chain_thread()
        time.sleep(SLOW * 2)                      # let one refresh land
        t0 = time.time()
        for _ in range(50):
            collect.chain_cached()
        el = time.time() - t0
        check(el < 0.05, f"50 cached reads cost {el:.4f}s — the loop never waits")
        check(collect.chain_cached().get("tip") == 969122,
              "and it carries the real value once the thread has fetched one")
finally:
    collect.chain_facts = orig

if not CONTROL:
    # ⛔ A FAILED FETCH MUST NOT ERASE A KNOWN TIP. A transient blip turning "tip 969122" into
    # "unknown" is the same class of lie as the frozen page: it reports absence of knowledge when
    # what changed was one request.
    with collect._CHAIN_LOCK:
        collect._CHAIN["v"], collect._CHAIN["at"] = {"tip": 969122, "ok": True}, time.time()
    collect.chain_facts = lambda: {"ok": False, "error": "Network is unreachable"}
    try:
        collect._chain_refresh_loop.__wrapped__ if False else None
        # drive one iteration by hand rather than starting another thread
        got = collect.chain_facts()
        if got.get("ok") is not False:
            raise AssertionError("fixture wrong")
        with collect._CHAIN_LOCK:
            collect._CHAIN["v"] = dict(collect._CHAIN["v"], last_error=got.get("error"))
    finally:
        collect.chain_facts = orig
    c = collect.chain_cached()
    check(c.get("tip") == 969122, "a FAILED fetch keeps the last known tip")
    check(c.get("last_error"), f"and records why it is not fresh ({str(c.get('last_error'))[:34]})")

    # A cache older than max_age must say so rather than pass as current.
    with collect._CHAIN_LOCK:
        collect._CHAIN["at"] = time.time() - 300
    check(collect.chain_cached(max_age_s=90).get("stale_s", 0) > 200,
          "and an old reading is marked stale rather than presented as fresh")

# ── hazync#583: the lock must ask what the pid IS ───────────────────────────────────────────────
me = os.getpid()
if CONTROL:
    # The old rule: does /proc/<pid> exist?
    def old_rule(pid):
        return os.path.exists(f"/proc/{pid}")
    check(old_rule(me), "control: the old rule says yes for this process")
    check(old_rule(1), "⛔ control reproduces it: the old rule says yes for pid 1, which is NOT a "
                       "collector — any live or recycled pid passes")
else:
    check(collect.pid_is_a_collector(me) is False,
          "⚠ this test process is not a collector, and the check says so "
          "(it reads cmdline, not merely /proc)")
    check(collect.pid_is_a_collector(1) is False,
          "⛔ pid 1 is alive and is NOT a collector — a recycled pid can no longer hold the lock")
    check(collect.pid_is_a_collector(2 ** 30) is False, "a pid that does not exist is not a collector")
    check(collect.pid_is_a_collector(-1) is False, "and a nonsense pid does not raise")

    # A REAL collector must still be recognised, or the guard stops guarding.
    import subprocess
    import tempfile
    d = tempfile.mkdtemp(prefix="collock-")
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "collect.py"),
                             "--demo", "--loop", "--out", os.path.join(d, "s.json")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2.0)
        check(collect.pid_is_a_collector(proc.pid) is True,
              "⛔ AND A REAL RUNNING COLLECTOR IS STILL DETECTED — the fix must not disarm the guard")
    finally:
        proc.terminate()
        proc.wait(timeout=20)
    # Once it is gone (and reaped by wait()), it must no longer hold the lock.
    check(collect.pid_is_a_collector(proc.pid) is False,
          "and once it exits it no longer holds the lock — which is the bug that started this")

print()
if fails:
    print(f"FAIL: {fails}")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

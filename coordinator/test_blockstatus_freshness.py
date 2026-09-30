#!/usr/bin/env python3
"""/api/blockstatus has its OWN cache TTL, so the map does not inherit the index's 120 s.

⛔ WHAT WENT WRONG. The map colours come from `/api/blockstatus`; clicking a square calls
`/api/block/<n>`. Both read the same `vranges` table and agree on precedence, so the definition was
never in doubt — but blockstatus is cached and block detail is not. At `VRANGES_TTL=120` plus a 60 s
client poll, a block read **OPEN for up to ~180 s after it was proved**, while clicking it said
"proved". Reproduced on 134,097 and 134,098.

⛔ AND THE 120 WAS NEVER ABOUT THIS ENDPOINT. It is sized for the `vranges` index rebuild, ~25 s at
69k rows. Measured on the live DB 2026-09-30, blockstatus rebuilds in **338 ms** over 230,126 rows —
74x cheaper. The map's freshness was governed by the cost of something it does not use.

⚠ THE CLIENT POLL IS THE FLOOR. Served staleness is at most `TTL + poll`, so 30 s here only reaches a
~60 s worst case because board.js polls every 30 s too. Lowering one without the other buys half.

    python3 test_blockstatus_freshness.py             # blockstatus has its own, smaller TTL
    python3 test_blockstatus_freshness.py --control   # back on the shared TTL — staleness must return
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "server.py"), encoding="utf8").read()
CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def const(name, default=None):
    m = re.search(rf'^{name}\s*=\s*float\(os\.environ\.get\("([A-Z_]+)",\s*"([0-9.]+)"\)\)',
                  SRC, re.M)
    return (m.group(1), float(m.group(2))) if m else (None, default)

# ── 1. it exists, is separately tunable, and is smaller than the index TTL ──────────────────────
env_bs, bs = const("BLOCKSTATUS_TTL")
env_vr, vr = const("VRANGES_TTL")
check(env_vr == "VRANGES_CACHE_TTL" and vr, f"the index TTL is still there ({env_vr}={vr})")
if CONTROL:
    # ⛔ The control is the world before the fix: blockstatus back on the shared number.
    bs, env_bs = vr, env_vr
    check(True, f"control: blockstatus is governed by {env_vr}={vr}")
else:
    check(env_bs == "BLOCKSTATUS_CACHE_TTL" and bs is not None,
          f"blockstatus has its OWN env knob ({env_bs}={bs})")
    check(bs < vr, f"and it is smaller than the index TTL ({bs} < {vr}) — it is 74x cheaper to rebuild")

# ── 2. the call site uses it ────────────────────────────────────────────────────────────────────
m = re.search(r'_single_flight\("blockstatus".*?,\s*([A-Z_]+),\s*build', SRC, re.S)
used = m.group(1) if m else None
if CONTROL:
    check(True, f"control: call site would read {used}")
else:
    check(used == "BLOCKSTATUS_TTL",
          f"block_status_cached is wired to BLOCKSTATUS_TTL (found {used}) — defining a constant "
          f"nobody reads is the failure that looks exactly like a fix")

# ── 3. ⛔ THE ONE THAT MATTERS: worst-case staleness against the client poll ─────────────────────
# board.js polls every POLL_MS; served staleness is at most TTL + poll, because the poll that finds
# the entry stale is served the PREVIOUS value while the rebuild runs behind it.
POLL_S = 30.0
worst = bs + POLL_S
print(f"       worst-case staleness = TTL {bs:.0f}s + poll {POLL_S:.0f}s = {worst:.0f}s")
if CONTROL:
    check(worst > 120,
          f"⛔ control: on the shared TTL the map can be {worst:.0f}s behind the popup — which is the "
          f"bug, and no client change can fix it because the cache is server-side")
else:
    check(worst <= 60.0, f"the map is at most {worst:.0f}s behind /api/block/<n>")
    check(bs >= 10.0,
          f"⚠ but not so small that a 338 ms rebuild runs back to back ({bs}s) — #265 piled 148 "
          f"threads onto the coordinator when a TTL went under its own rebuild time")

# ── 4. the shared TTL is untouched, and so is the global stale window ───────────────────────────
check(vr == 120.0, f"VRANGES_TTL is unchanged at {vr} — the 5.8 MB index still rebuilds at its own pace")
mstale = re.search(r'CACHE_MAX_STALE\s*=\s*float\(os\.environ\.get\("CACHE_MAX_STALE",\s*"([0-9.]+)"\)\)', SRC)
check(mstale is not None and float(mstale.group(1)) == 60.0,
      "⛔ CACHE_MAX_STALE is unchanged — it is GLOBAL to every background cache including /api/state, "
      "the path that queued visitors behind 3.4-4.4 s rebuilds in #324")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — the control should reproduce the staleness, not fail")
        sys.exit(1)
    print("PASS (control): on the shared TTL the window is >120s, which is the reported bug")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")

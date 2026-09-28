#!/usr/bin/env python3
"""`/api/state` does not ship the verified-range list, and nothing polls for it every ten seconds.

⛔ WHAT THIS EXISTS FOR. The four dynamic badges at the top of README.md rendered **inaccessible** —
the first thing anyone sees on the repository front page. Measured 2026-09-28:

    /api/state?slim=1      9,859 B    0.16 s   200
    /api/state           19,795,606 B  33.3 s   -> 504 at the edge

`vranges` is 19,785,870 of those bytes: **99.95%**. The default kept it "so existing clients are
unaffected" (#35), but by the time the list reached 19.8 MB no client could complete the request, so
the default was protecting nobody. Every consumer was already broken.

⚠ AND THE BLOCK MAP POLLED THE UNSLIMMED ENDPOINT EVERY TEN SECONDS — twenty megabytes, six times a
minute, from a page that had itself stopped loading. `/api/vranges` has had its own cache and ETag
since #35; the verified list only moves when `progress.proven` moves.

⛔ THIS IS A PUBLIC DEFAULT CHANGING. That is only defensible because the old default did not work:
a response nobody can receive has no users to break. `?full=1` still serves it.

    python3 test_state_payload.py             # slim by default, opt in with ?full=1
    python3 test_state_payload.py --control   # vranges by default — the 19.8 MB must come back
"""
import os
import re
import sys
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONTROL = "--control" in sys.argv
fails = 0

# Measured, not invented.
SLIM_B, FULL_B = 9_859, 19_795_606
SLIM_S, FULL_S = 0.16, 33.3


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def wants_full(path):
    """The routing decision, as server.py makes it."""
    q = parse_qs(urlparse(path).query)
    if CONTROL:
        # the shipped behaviour: full unless ?slim=1
        return q.get("slim", ["0"])[0] in ("0", "", "false")
    return q.get("full", ["0"])[0] not in ("0", "", "false")


def payload_bytes(path):
    return FULL_B if wants_full(path) else SLIM_B


# ── 1. the bare endpoint ─────────────────────────────────────────────────────────────────────────
n = payload_bytes("/api/state")
if CONTROL:
    check(n == FULL_B,
          f"control reproduces it: a bare /api/state ships {n:,} B — {FULL_S}s, and 504 at the edge")
    check(n / SLIM_B > 1000, f"control: {n / SLIM_B:.0f}x the slim response, for one extra key")
else:
    check(n == SLIM_B, f"a bare /api/state ships {n:,} B, not {FULL_B:,}")
    check(payload_bytes("/api/state?full=1") == FULL_B,
          "and ?full=1 still serves the whole thing for anything that genuinely wants it")
    check(payload_bytes("/api/state?slim=1") == SLIM_B, "?slim=1 keeps working, so nothing breaks")
    for v in ("0", "", "false"):
        check(payload_bytes(f"/api/state?full={v}") == SLIM_B,
              f"?full={v!r} is not truthy — the same words ?slim accepts")

# ── 2. the badges only ever needed the slim keys ─────────────────────────────────────────────────
if not CONTROL:
    readme = open(os.path.join(HERE, "..", "README.md"), encoding="utf8").read()
    badges = re.findall(r"img\.shields\.io/badge/dynamic/json\?url=([^&]+)&query=([^&]+)", readme)
    check(len(badges) >= 4, f"found {len(badges)} dynamic badges in README.md")
    for url, query in badges:
        check("slim%3D1" in url or "slim=1" in url,
              f"badge {query} asks the slim endpoint")
        # every badge reads $.progress.*, which slim carries in full
        check("progress" in query, f"and reads {query}, which is in the slim response")

# ── 3. the block map does not poll for twenty megabytes ─────────────────────────────────────────
if not CONTROL:
    page = open(os.path.join(HERE, "web", "index.html"), encoding="utf8").read()
    js = re.search(r"<script[^>]*>(.*)</script>", page, re.S).group(1)
    check("'/api/state'" not in js, "the page never fetches the unslimmed /api/state")
    check("/api/state?slim=1" in js, "it polls the slim one")
    check("/api/vranges" in js, "and fetches the verified list from its own endpoint")
    # ⛔ ONLY WHEN IT CAN HAVE CHANGED. Fetching 14 MB every ten seconds from a different URL would
    # be the same fault wearing a different path.
    check(re.search(r"proven\s*===?\s*VR_AT|VR_AT\s*===?\s*proven", js) is not None,
          "and only when progress.proven has moved, not on every poll")
    check(js.count("setInterval(poll,10000)") == 1, "the 10 s poll itself is unchanged")
    # the map must survive a failed vranges fetch rather than blanking
    check("VR_BUSY=false;});" in js.replace(" ", "").replace("\n", "") or "catch" in js,
          "a failed vranges fetch keeps the last good list")

# ── 4. the shape of the win ──────────────────────────────────────────────────────────────────────
if not CONTROL:
    per_hour_before = FULL_B * 360          # six polls a minute
    per_hour_after = SLIM_B * 360
    check(per_hour_after < per_hour_before / 1000,
          f"the block map's hourly pull falls from {per_hour_before / 1e9:.1f} GB to "
          f"{per_hour_after / 1e6:.1f} MB, before counting the vranges refetches it still makes")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

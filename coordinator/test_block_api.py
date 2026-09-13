#!/usr/bin/env python3
"""The block map's API: /api/blockstatus, /api/block/<n>, sponsorship, and weak ETags (2026-09-13).

WHY THIS EXISTS. The block map downloaded every verified range with its handle (5.8 MB, 568 KB gzipped)
on every load and every five minutes, and could never be told "unchanged": nginx gzips the response and
turns the ETag weak, and the coordinator compared the raw header with the strong tag. A pop-up for one
block would have needed that whole download too.

  1. _etag_matches accepts W/"x" as well as "x", so revalidation answers 304;
  2. block_status runs agree block for block with the statuses the map built from /api/vranges;
  3. ?prover= lights only the ranges that prover proved, never their folds, and never a blocked key;
  4. block_detail reports proofs, folds, a claim and a status for one block;
  5. sponsorship is closed by default, validates when open, and never shows an unpaid name;
  6. the routes answer over HTTP, including a weak If-None-Match -> 304.

WHAT THIS DOES NOT COVER: real STARK verification (VERIFY_MODE=mock), payments, and the bot, which is
a dry-run skeleton (its queue read is exercised).

  python3 test_block_api.py            # must PASS
  python3 test_block_api.py --control  # strong-only ETag match, unpaid sponsor names shown; MUST FAIL
"""
import json
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpblock = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w")
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["MOD_BLOCK_FILE"] = _tmpblock.name
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ["VRANGES_CACHE_TTL"] = "0"
os.environ.setdefault("TIP_HEIGHT", "1000")
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
import sponsor_bot  # noqa: E402
server.init_db()

if CONTROL:
    server._etag_matches = lambda header, etag: header == etag          # the old exact comparison

    def _any_sponsor(c, n):                                               # names shown whatever the status
        r = c.execute("SELECT name,lo,hi,status FROM sponsorships WHERE lo<=? AND hi>=? LIMIT 1", (n, n)).fetchone()
        return dict(name=r["name"], lo=r["lo"], hi=r["hi"], status=r["status"]) if r else None
    server._public_sponsor = _any_sponsor
    print("CONTROL: strong-only ETags and unpaid sponsor names -- the checks below MUST fail")

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


AA, BB, CC, DD = "aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32


def seed():
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")
    rows = [((1, 1), AA, "alice"), ((2, 2), AA, "alice"), ((3, 3), BB, "bob"), ((4, 4), AA, "alice"),
            ((1, 2), AA, "alice"), ((3, 4), AA, "alice"), ((1, 4), AA, "alice"),
            ((10, 20), BB, "bob"), ((30, 30), CC, "carol")]
    for i, ((lo, hi), pk, handle) in enumerate(rows):
        rid = str(lo) if lo == hi else f"{lo}-{hi}"
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (rid, lo, hi))
        c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
                  " VALUES(?,?,?,?,?,?,?,?,0,'0')", (rid, lo, hi, f"in{lo}", f"out{hi}", pk, handle, i))
    now = time.time()
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at,last_beat)"
              " VALUES('40',40,40,'claimed',?,'dave',?,?)", (DD, now - 90, now - 10))
    c.commit()
    c.close()


seed()
server.spine_head = lambda: {"hi": 2}          # blocks 1..2 inside the genesis proof

print("== weak ETags ==")
check(server._etag_matches('W/"abc"', '"abc"'), 'W/"abc" matches "abc" (what nginx + a browser send)')
check(server._etag_matches('"abc"', '"abc"'), '"abc" matches "abc"')
check(server._etag_matches('"x", W/"abc"', '"abc"'), "a list of tags matches if any does")
check(not server._etag_matches('W/"abd"', '"abc"'), "a different tag does not match")
check(not server._etag_matches(None, '"abc"'), "no header does not match")

print("== /api/blockstatus agrees with what the map built from /api/vranges ==")
bs = server.block_status()
got = {}
for lo, hi, code in bs["runs"]:
    for h in range(lo, hi + 1):
        got[h] = code
c = server.db()
vr = server.build_vranges(c, set())
c.close()
want = {}
for r in vr:                                     # blockmap.js buildModel, verbatim in Python
    code = 4 if r.get("fold") else 3
    for h in range(max(1, r["lo"]), r["hi"] + 1):
        want[h] = max(want.get(h, 0), code)
for h in range(1, 3):
    want[h] = 5
check(got == want, f"runs match the map's statuses block for block ({len(got)} blocks, {len(bs['runs'])} runs)")
check(bs["runs"] == [[1, 2, 5], [3, 4, 4], [10, 20, 3], [30, 30, 3]],
      f"anchored 1-2, folded 3-4, a wide PROVED range 10-20 is not a fold, 30 proven (got {bs['runs']})")
check(all(h not in got for h in (5, 9, 40)), "open blocks and claimed block 40 are in no run (claims ride on /api/state)")

print("== ?prover= ==")
check(server.block_status("bob")["runs"] == [[3, 3, 3], [10, 20, 3]],
      f"bob: the blocks bob proved, not folds (got {server.block_status('bob')['runs']})")
check(server.block_status("alice")["runs"] == [[1, 2, 3], [4, 4, 3]],
      f"alice: her own proofs only; her folds over bob's block 3 do not light it (got {server.block_status('alice')['runs']})")
check({"alice", "bob", "carol"} <= server.known_handles_cached(), "known handles lists every prover")

print("== /api/block/<n> ==")
code, d3 = server.block_detail(3)
check(code == 200 and d3["status"] == "folded", f"block 3 is folded (got {code} {d3.get('status')})")
check([(p["lo"], p["hi"], p["handle"], p["fold"]) for p in d3["proofs"]]
      == [(3, 3, "bob", False), (3, 4, "alice", True), (1, 4, "alice", True)],
      f"block 3 lists its own proof by bob, then alice's folds, narrowest first (got {[(p['lo'], p['hi'], p['handle'], p['fold']) for p in d3['proofs']]})")
_, d1 = server.block_detail(1)
check(d1["status"] == "spined", f"block 1 is anchored (got {d1['status']})")
_, d15 = server.block_detail(15)
check(d15["status"] == "proved" and len(d15["proofs"]) == 1, f"block 15 is proven by one wide proof (got {d15['status']})")
_, d40 = server.block_detail(40)
check(d40["status"] == "claimed" and d40["claim"] and d40["claim"]["handle"] == "dave" and not d40["claim"]["stale"],
      f"block 40 is claimed by dave (got {d40['status']}, {d40['claim']})")
_, d50 = server.block_detail(50)
check(d50["status"] == "open" and d50["proofs"] == [] and d50["claim"] is None, "block 50 is open")
code, dfar = server.block_detail(server.chain_tip() + 1)
check(code == 404, f"a block past the tip is 404 (got {code})")

print("== moderation ==")
with open(_tmpblock.name, "w") as f:
    f.write(BB + "\n")
_, d3m = server.block_detail(3)
check(d3m["proofs"][0]["handle"] == "[removed]", "a blocked prover's proof shows as [removed]")
check("bob" not in server.known_handles_cached(), "a blocked prover is not a known handle, so ?prover=bob is refused")
check(server.block_status("bob")["runs"] == [], "and their blocks never light up")
with open(_tmpblock.name, "w") as f:
    f.write("")

print("== sponsorship ==")
server.SPONSOR_OPEN = False
code, body = server.sponsor_request({"lo": 50, "hi": 60, "name": "John Doe"})
check(code == 503 and body.get("open") is False, f"closed by default: 503 (got {code})")
server.SPONSOR_OPEN = True
code, body = server.sponsor_request({"lo": 50, "hi": 60, "name": "  John   Doe "})
check(code == 202 and body["status"] == "requested", f"open: a valid request is recorded as requested (got {code})")
sid = body.get("id")
bad = [({"lo": 50, "hi": 60, "name": ""}, "an empty name"),
       ({"lo": 50, "hi": 60, "name": "x" * 41}, "a 41-character name"),
       ({"lo": 50, "hi": 60, "name": "evil‮name"}, "a right-to-left override"),
       ({"lo": 60, "hi": 50, "name": "a"}, "hi below lo"),
       ({"lo": 1, "hi": 5000, "name": "a"}, "past the tip"),
       ({"lo": 3, "hi": 3 + server.SPONSOR_MAX_BLOCKS, "name": "a"}, "more than the maximum blocks"),
       ({"lo": "x", "name": "a"}, "a height that is not a number")]
for payload, why in bad:
    code, _ = server.sponsor_request(payload)
    check(code == 400, f"refused: {why} (got {code})")
code, _ = server.sponsor_request({"lo": 1, "hi": 2, "name": "late"})
check(code == 409, f"refused: blocks already anchored (got {code})")
_, d55 = server.block_detail(55)
check(d55["sponsor"] is None, "an unpaid request never shows a name")
c = server.db()
c.execute("UPDATE sponsorships SET status='paid', paid_at=? WHERE id=?", (time.time(), sid))
c.commit()
c.close()
_, d55 = server.block_detail(55)
check(d55["sponsor"] and d55["sponsor"]["name"] == "John Doe", f"a PAID sponsorship shows its name, whitespace tidied (got {d55['sponsor']})")
check(sponsor_bot.plan(sponsor_bot.queue(_tmpdb.name)) == ["sponsorship #%d: would prove blocks 50 to 60 (11 blocks) for John Doe" % sid],
      "the bot's dry run reads the paid queue")
c = server.db()
c.execute("UPDATE sponsorships SET status='requested', paid_at=NULL WHERE id=?", (sid,))
c.commit()
c.close()
server.SPONSOR_OPEN = False

print("== the routes, over HTTP ==")
import threading  # noqa: E402
import urllib.request  # noqa: E402
import urllib.error  # noqa: E402

httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"


def call(path, data=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None), dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw) if raw else None, dict(e.headers)
        except ValueError:
            return e.code, None, dict(e.headers)


code, body, hdr = call("/api/blockstatus")
check(code == 200 and body and body["runs"][0] == [1, 2, 5], f"GET /api/blockstatus answers the runs (got {code})")
etag = hdr.get("ETag")
code, _, _ = call("/api/blockstatus", headers={"If-None-Match": f"W/{etag}"})
check(code == 304, f"...and a weak If-None-Match, as nginx and a browser send it, is 304 (got {code})")
code, _, hdr = call("/api/vranges")
code, _, _ = call("/api/vranges", headers={"If-None-Match": f"W/{hdr.get('ETag')}"})
check(code == 304, f"GET /api/vranges with a weak If-None-Match is 304 too (got {code})")
code, body, _ = call("/api/blockstatus?prover=bob")
check(code == 200 and body["runs"] == [[3, 3, 3], [10, 20, 3]], f"GET /api/blockstatus?prover=bob (got {code})")
code, _, _ = call("/api/blockstatus?prover=nobody")
check(code == 404, f"an unknown prover is 404, so no cache key is made for it (got {code})")
code, body, _ = call("/api/block/3")
check(code == 200 and body["status"] == "folded", f"GET /api/block/3 (got {code})")
code, _, _ = call("/api/block/abc")
check(code == 400, f"GET /api/block/abc is 400 (got {code})")
code, body, _ = call("/api/sponsor")
check(code == 200 and body == {"open": False, "max_blocks": server.SPONSOR_MAX_BLOCKS, "payments": False},
      f"GET /api/sponsor says closed (got {code} {body})")
code, body, _ = call("/api/sponsor", data={"lo": 50, "hi": 50, "name": "x"})
check(code == 503, f"POST /api/sponsor while closed is 503 (got {code})")
httpd.shutdown()

print(f"{'CONTROL: ' if CONTROL else ''}{len(fails)} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)

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
  5. sponsorship is closed by default, validates when open, quotes a minimum only from configured
     price bands, refuses a pledge below it, gives a private status link stored only as a hash, and never
     shows a name unless the status says paid AND what settled covers the minimum;
  6. the routes answer over HTTP, including a weak If-None-Match -> 304.

WHAT THIS DOES NOT COVER: real STARK verification (VERIFY_MODE=mock), payments, and the bot, which is
a dry-run skeleton (its queue read is exercised).

  python3 test_block_api.py            # must PASS
  python3 test_block_api.py --control  # strong-only ETag match, unpaid sponsor names shown; MUST FAIL
"""
import hashlib
import json
import os
import sqlite3
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
os.environ.pop("SPONSOR_PRICE_BANDS", None)          # unset: the default ladder applies
os.environ["SPONSOR_BTC_USD"] = "100000"               # $1 = 1,000 sats
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

print("== sponsorship: price bands ==")
check(server.SPONSOR_PRICE_BANDS_DEFAULT == [(1, 200000, 1), (200001, 400000, 2), (400001, 600000, 3),
                                             (600001, 800000, 4), (800001, 1000000, 5), (1000001, 10 ** 9, 6)],
      "the default ladder is the approved one: $1 a block to 200,000, $2 to 400,000, $3 to 600,000, $4 to 800,000,"
      " $5 to 1,000,000 and $6 above")
check(server.SPONSOR_PRICE_BANDS == server.SPONSOR_PRICE_BANDS_DEFAULT, "with SPONSOR_PRICE_BANDS unset, the default ladder applies")
check(server.SPONSOR_BTC_USD == 100000, "SPONSOR_BTC_USD is read from the environment")
check(server._parse_price_bands("") is None, "SPONSOR_PRICE_BANDS set but empty: unpriced")
for raw, why in (("not json", "unparseable"), ("[[1,10,0]]", "a zero price"), ("[[10,1,5]]", "hi below lo"),
                 ("[[1,10,5],[10,20,5]]", "two bands that overlap"), ('[[1,10,"5"]]', "a price that is text"),
                 ("[[1,10,1.5]]", "a price that is not whole dollars"),
                 ("[]", "an empty list"), ("[[0,10,5]]", "a band starting below block 1")):
    check(server._parse_price_bands(raw) is None, f"bands refused: {why}")
check(server._parse_price_bands("[[11,20,7],[1,10,5]]") == [(1, 10, 5), (11, 20, 7)], "valid bands parse, sorted")
for raw, why in ((None, "unset"), ("", "empty"), ("abc", "not a number"), ("0", "zero"), ("-5", "negative"),
                 ("nan", "not a number (nan)"), ("inf", "infinite")):
    check(server._parse_btc_usd(raw) is None, f"bitcoin price refused: {why}")
check(server._parse_btc_usd("77400") == 77400 and server._parse_btc_usd("77400.5") == 77400.5, "a bitcoin price parses")
server.SPONSOR_PRICE_BANDS = server.SPONSOR_PRICE_BANDS_DEFAULT
check(server.sponsor_min_usd(199999, 200001) == 1 + 1 + 2 and server.sponsor_min_usd(999999, 1000001) == 5 + 5 + 6,
      "the default ladder sums across its bands ($1 + $1 + $2 at 199,999-200,001; $5 + $5 + $6 at 999,999-1,000,001)")
check(server.sponsor_min_usd(400000, 400001) == 2 + 3 and server.sponsor_min_usd(5000000, 5000000) == 6,
      "every block has a price up to the open top band: $2 + $3 at 400,000-400,001, $6 at 5,000,000")
BANDS = server._parse_price_bands("[[1,55,1],[56,1000,3]]")

print("== sponsorship: the quote ==")
# Bundles for blocks 50-55 only, and no legacy witnesses, so `waiting` is known.
_bundles, _no_witness = tempfile.mkdtemp(prefix="bundles_"), tempfile.mkdtemp(prefix="witness_")
_saved_dirs = (server.BRIDGE_DIR, server.WITNESS)
server.BRIDGE_DIR, server.WITNESS = _bundles, _no_witness
for _h in range(50, 56):
    open(os.path.join(_bundles, f"bundle_{_h}.json"), "w").close()
server.SPONSOR_PRICE_BANDS = None
code, q = server.sponsor_quote(50, 60)
check(code == 200 and q["min_usd"] is None and q["min_sats"] is None and q["priced"] is False,
      f"no bands: the quote has no minimum (got {code} {q})")
server.SPONSOR_PRICE_BANDS = BANDS
code, q = server.sponsor_quote(50, 60)
check(code == 200 and q == {"lo": 50, "hi": 60, "blocks": 11, "min_usd": 6 * 1 + 5 * 3, "min_sats": 21000,
                            "btc_usd": 100000, "priced": True, "waiting": 5},
      f"blocks 50-60 across two bands: 6 x $1 + 5 x $3 = $21, 21,000 sats at $100,000; 5 have no bundle yet (got {code} {q})")
check(server.sponsor_quote(50, 55)[1]["waiting"] == 0, "blocks that all have bundles: nothing waits")
server.SPONSOR_BTC_USD = 77400
code, q = server.sponsor_quote(50, 50)
check(code == 200 and q["min_usd"] == 1 and q["min_sats"] == 1292,
      f"sats round UP: $1 at $77,400 is 1,291.99 sats, so 1,292 (got {q})")
server.SPONSOR_BTC_USD = 100000
code, q = server.sponsor_quote(50, 50)
check(q["min_sats"] == 1000, f"an exact conversion does not round up past itself: $1 at $100,000 is 1,000 sats (got {q})")
server.SPONSOR_BTC_USD = None
code, q = server.sponsor_quote(50, 60)
check(code == 200 and q["min_usd"] == 21 and q["min_sats"] is None and q["btc_usd"] is None and q["priced"] is True,
      f"no bitcoin price: the quote gives dollars and no sats (got {q})")
server.SPONSOR_BTC_USD = 100000
server.SPONSOR_PRICE_BANDS = server._parse_price_bands("[[1,55,1]]")
code, q = server.sponsor_quote(50, 60)
check(code == 200 and q["min_sats"] is None, f"a span partly outside every band is unpriced (got {q})")
server.BRIDGE_DIR, server.WITNESS = _saved_dirs
server.SPONSOR_PRICE_BANDS = BANDS
check(server.sponsor_quote(1, 2)[0] == 409, "quote refused: blocks already anchored (same as the request)")
check(server.sponsor_quote(60, 50)[0] == 400, "quote refused: hi below lo")
check(server.sponsor_quote(3, 3 + server.SPONSOR_MAX_BLOCKS)[0] == 400, "quote refused: more than the maximum blocks")
check(server.sponsor_quote("x", None)[0] == 400, "quote refused: a height that is not a number")

print("== sponsorship: the request ==")
server.SPONSOR_OPEN = False
code, body = server.sponsor_request({"lo": 50, "hi": 60, "name": "John Doe", "amount_sats": 5000})
check(code == 503 and body.get("open") is False, f"closed by default: 503 (got {code})")
server.SPONSOR_OPEN = True
server.SPONSOR_PRICE_BANDS = None
code, body = server.sponsor_request({"lo": 50, "hi": 60, "name": "John Doe", "amount_sats": 5000})
check(code == 503 and "No minimum" in body.get("error", ""), f"open but unpriced: 503, nothing recorded (got {code} {body})")
server.SPONSOR_PRICE_BANDS = BANDS
server.SPONSOR_BTC_USD = None
code, body = server.sponsor_request({"lo": 50, "hi": 60, "name": "John Doe", "amount_sats": 10 ** 9})
check(code == 503 and body.get("error") == "No bitcoin price is set to turn the minimum into sats, so these blocks cannot be sponsored yet.",
      f"priced but no bitcoin price: 503, nothing recorded (got {code} {body})")
server.SPONSOR_BTC_USD = 100000
for amount, why in ((20999, "one sat below the minimum"), (None, "no amount"), ("30000", "an amount that is text"),
                    (True, "an amount that is true"), (0, "zero"), (21000.5, "a fraction of a sat")):
    payload = {"lo": 50, "hi": 60, "name": "John Doe"}
    if amount is not None:
        payload["amount_sats"] = amount
    code, body = server.sponsor_request(payload)
    check(code == 400 and body.get("min_sats") == 21000 and body.get("min_usd") == 21,
          f"refused: {why}, and told the minimum in sats and dollars (got {code} {body})")
c = server.db()
check(c.execute("SELECT COUNT(*) FROM sponsorships").fetchone()[0] == 0, "nothing refused was recorded")
c.close()
code, body = server.sponsor_request({"lo": 50, "hi": 60, "name": "  John   Doe ", "amount_sats": 21000})
check(code == 202 and body["status"] == "requested" and body["min_sats"] == 21000 and body["min_usd"] == 21
      and body["pledged_sats"] == 21000
      and body["name"] == "John Doe" and body["blocks"] == 11 and isinstance(body.get("token"), str) and len(body["token"]) >= 32,
      f"exactly the minimum is accepted, with a status link token (got {code} {body})")
sid, token = body.get("id"), body.get("token")
code, body2 = server.sponsor_request({"lo": 12, "hi": 30, "name": "Big Giver", "amount_sats": 99999})
check(code == 409 and "already proven" in body2.get("error", ""),
      f"a span with a proven block in it is refused: 12-20 and 30 are proven (got {code} {body2})")
code, body2 = server.sponsor_request({"lo": 21, "hi": 29, "name": "Big Giver", "amount_sats": 99999})
check(code == 202 and body2["min_sats"] == 9 * 1000 and body2["min_usd"] == 9 and body2["pledged_sats"] == 99999,
      f"more than the minimum is accepted (got {code} {body2})")
sid2, token2 = body2.get("id"), body2.get("token")
check(token != token2, "every sponsorship gets its own link")
# Blocks 21-25 are proven AFTER the request (a sponsorship can only be made while its blocks are open), so
# the status link and the public list have progress to report.
c = server.db()
for h in range(21, 26):
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (str(h), h, h))
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
              " VALUES(?,?,?,?,?,?,?,?,0,'0')", (str(h), h, h, f"in{h}", f"out{h}", "ee" * 32, "erin", 100 + h))
c.commit()
c.close()
bad = [({"lo": 50, "hi": 60, "name": "", "amount_sats": 5000}, "an empty name"),
       ({"lo": 50, "hi": 60, "name": "x" * 41, "amount_sats": 5000}, "a 41-character name"),
       ({"lo": 50, "hi": 60, "name": "evil\u202ename", "amount_sats": 5000}, "a right-to-left override"),
       ({"lo": 60, "hi": 50, "name": "a", "amount_sats": 5000}, "hi below lo"),
       ({"lo": 1, "hi": 5000, "name": "a", "amount_sats": 5000}, "past the tip"),
       ({"lo": 3, "hi": 3 + server.SPONSOR_MAX_BLOCKS, "name": "a", "amount_sats": 10 ** 9}, "more than the maximum blocks"),
       ({"lo": "x", "name": "a", "amount_sats": 5000}, "a height that is not a number")]
for payload, why in bad:
    code, _ = server.sponsor_request(payload)
    check(code == 400, f"refused: {why} (got {code})")
code, _ = server.sponsor_request({"lo": 1, "hi": 2, "name": "late", "amount_sats": 5000})
check(code == 409, f"refused: blocks already anchored (got {code})")

print("== sponsorship: the name rule the site's form copies ==")
clean = server._clean_sponsor_name
check(server.sponsor_info()["name_max"] == server.SPONSOR_NAME_MAX == 40, "GET /api/sponsor tells the form the name limit")
check(clean("\U0001F525" * 40) is not None and clean("\U0001F525" * 41) is None,
      "40 emoji fit and 41 do not: characters are code points, not bytes or UTF-16 units")
check(clean("Melt \U0001FAE0") == "Melt \U0001FAE0",
      "an emoji newer than this Python's Unicode tables is accepted (unassigned is not hidden)")
check(clean("Jo\u200dhn") is None, "a zero-width joiner is refused: it would make 'Jo<ZWJ>hn' look like 'John'")
check(clean("John\t Doe") == "John Doe", "a tab inside a name collapses like any other whitespace")
check(clean("\ufeffJohn") is None, "a byte-order mark is formatting, not whitespace, and is refused")

print("== sponsorship: the private link ==")
c = server.db()
row = c.execute("SELECT token_hash FROM sponsorships WHERE id=?", (sid,)).fetchone()
dump = " ".join(str(v) for r in c.execute("SELECT * FROM sponsorships") for v in tuple(r))
c.close()
check(row["token_hash"] == hashlib.sha256(token.encode()).hexdigest(), "the database keeps the link's sha256")
check(token not in dump and token2 not in dump, "and never the link itself")
raw_db = open(_tmpdb.name, "rb").read() + (open(_tmpdb.name + "-wal", "rb").read() if os.path.exists(_tmpdb.name + "-wal") else b"")
check(token.encode() not in raw_db, "not even in the database file's bytes")
code, st = server.sponsor_status(token)
check(code == 200 and st["id"] == sid and st["name"] == "John Doe" and st["status"] == "requested" and st["public"] is False
      and st["min_sats"] == 21000 and st["min_usd"] == 21 and st["pledged_sats"] == 21000 and st["paid_sats"] is None
      and st["blocks"] == 11
      and st["proven_blocks"] == 0 and st["queue_ahead"] is None,
      f"the link shows the request, not yet public (got {code} {st})")
for t, why in (("A" * 32, "an unknown link"), ("../../etc", "a malformed link"), ("", "an empty link")):
    code, _ = server.sponsor_status(t)
    check(code == 404, f"{why} is 404 (got {code})")

print("== sponsorship: who is public ==")


def set_row(i, **kw):
    c = server.db()
    c.execute("UPDATE sponsorships SET " + ",".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), i))
    c.commit()
    c.close()


def public_names():
    return [r["name"] for r in server.sponsors_public()["sponsorships"]]


_, d55 = server.block_detail(55)
check(d55["sponsor"] is None and public_names() == [], "a requested sponsorship shows no name anywhere")
t1 = time.time()
set_row(sid, status="underpaid", paid_sats=1000, paid_at=t1)
_, d55 = server.block_detail(55)
check(d55["sponsor"] is None and public_names() == [] and server.sponsor_status(token)[1]["public"] is False,
      "an underpaid one shows no name anywhere")
set_row(sid, status="paid", paid_sats=20999)
_, d55 = server.block_detail(55)
check(d55["sponsor"] is None and public_names() == [] and server.bot_queue()[1]["sponsorships"] == [],
      "a row marked paid but one sat short shows no name and is not in the bot's queue")
set_row(sid, status="paid", paid_sats=21000)
_, d55 = server.block_detail(55)
check(d55["sponsor"] and d55["sponsor"]["name"] == "John Doe" and d55["sponsor"].get("id") == sid,
      f"paid the minimum: the block shows the name, whitespace tidied, and the sponsorship's number (got {d55['sponsor']})")
set_row(sid2, status="paid", paid_sats=99999, paid_at=t1 + 5)
lst = server.sponsors_public()
check([r["id"] for r in lst["sponsorships"]] == [sid2, sid], f"the public list is newest payment first (got {[r['id'] for r in lst['sponsorships']]})")
first = lst["sponsorships"][0]
check(first["paid_sats"] == 99999 and first["min_sats"] == 9000 and first["min_usd"] == 9 and first["blocks"] == 9,
      f"the public list shows what was paid (got {first})")
check(first["proven_blocks"] == 5, f"blocks 21-29: 21-25 were proven after the request, 5 of 9 (got {first['proven_blocks']})")
check(first["queue_ahead"] == 1 and lst["sponsorships"][1]["queue_ahead"] == 0,
      "the later payment has one sponsorship ahead of it, the first none")
check(not ({"token_hash", "pledged_sats", "note", "invoice_id", "token"} & set(first)),
      f"the public list never carries the link, pledge, note or invoice (keys {sorted(first)})")
check(lst["open"] is True and lst["priced"] is True, "the list says whether sponsorship is open and priced")
check(server.sponsor_status(token)[1]["public"] is True, "the link says the sponsorship is now public")
check(sponsor_bot.plan(server.bot_queue()[1]["sponsorships"]) == [
      "sponsorship #%d: would prove blocks 50 to 60 (11 blocks) for John Doe" % sid,
      "sponsorship #%d: would prove blocks 21 to 29 (9 blocks) for Big Giver" % sid2],
      "the bot's dry run reads the paid queue, oldest payment first")
set_row(sid2, status="proven", proven_at=t1 + 60)
check(server.sponsors_public()["sponsorships"][0]["queue_ahead"] is None, "a proven sponsorship is not waiting in the queue")
server.SPONSOR_OPEN = False

print("== a database with the first sponsorships table ==")
old = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
oc = sqlite3.connect(old)
oc.executescript("""
  CREATE TABLE sponsorships(id INTEGER PRIMARY KEY AUTOINCREMENT, lo INTEGER NOT NULL, hi INTEGER NOT NULL,
    name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'requested', created_at REAL NOT NULL,
    amount_sats INTEGER, invoice_id TEXT, paid_at REAL, proven_at REAL, note TEXT);
  INSERT INTO sponsorships(lo,hi,name,status,created_at,paid_at) VALUES(50,60,'old paid row','paid',1,1);
""")
oc.commit()
oc.close()
saved_db = server.DB
server.DB = old
try:
    server.init_db()
    started, cols = True, {r[1] for r in server.db().execute("PRAGMA table_info(sponsorships)")}
    old_public = server.sponsors_public()["sponsorships"]
except Exception as e:  # noqa: BLE001
    started, cols, old_public = False, set(), None
    print("   init_db raised:", e)
finally:
    server.DB = saved_db
check(started, "init_db starts on a database with the first sponsorships table")
check({"min_usd", "min_sats", "pledged_sats", "paid_sats", "token_hash"} <= cols, f"and adds the new columns (got {sorted(cols)})")
check(old_public == [], "an old 'paid' row with no amount and no minimum is not public")

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
check(code == 200 and body == {"open": False, "max_blocks": server.SPONSOR_MAX_BLOCKS, "payments": False, "priced": True,
                                "bands": [[1, 55, 1], [56, 1000, 3]], "btc_usd": 100000, "name_max": server.SPONSOR_NAME_MAX},
      f"GET /api/sponsor says closed, priced, the bands, the bitcoin price and the name limit (got {code} {body})")
# 50-60 is paid and HELD by now, so quote blocks that are still open.
code, body, _ = call("/api/sponsor/quote?lo=31&hi=39")
check(code == 200 and body["min_sats"] == 9000 and body["min_usd"] == 9, f"GET /api/sponsor/quote answers while closed (got {code} {body})")
code, body, _ = call("/api/sponsor/quote?lo=50&hi=60")
check(code == 409 and "already sponsored" in body.get("error", ""), f"GET /api/sponsor/quote refuses held blocks (got {code} {body})")
code, body, _ = call("/api/sponsor/quote?lo=abc")
check(code == 400, f"GET /api/sponsor/quote?lo=abc is 400 (got {code})")
code, body, _ = call("/api/sponsor/status/" + token)
check(code == 200 and body["id"] == sid and body["public"] is True, f"GET /api/sponsor/status/<token> (got {code})")
code, body, _ = call("/api/sponsor/status/nope")
check(code == 404, f"GET /api/sponsor/status/nope is 404 (got {code})")
code, body, _ = call("/api/sponsors")
check(code == 200 and [r["id"] for r in body["sponsorships"]] == [sid2, sid], f"GET /api/sponsors lists the public ones (got {code})")
code, body, _ = call("/api/sponsor", data={"lo": 50, "hi": 50, "name": "x"})
check(code == 503, f"POST /api/sponsor while closed is 503 (got {code})")
httpd.shutdown()

print(f"{'CONTROL: ' if CONTROL else ''}{len(fails)} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)

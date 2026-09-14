#!/usr/bin/env python3
"""
Sponsor holds (docs/SPONSORSHIP.md, "Holds"): a sponsorship paid at least its minimum keeps its blocks for the
sponsor proving bot until they are proven.

What is asserted:
  1. A held block is never offered to a normal worker: not by claim()'s scan, not by pick(). Only a `paid` or
     `proving` sponsorship paid at least its minimum holds; `requested`, `underpaid`, and a `paid` row below
     its minimum hold nothing.
  2. Not by claim()'s frontier-blocker re-offer either (#284), which would otherwise hand out a held block that
     is covered but cannot seam.
  3. /api/state says the frontier's next block is held for sponsorship #N, is calm about it for
     SPONSOR_HOLD_ALERT even when the frontier has not moved for hours, then asks for a human; /api/block/<n>
     carries the hold.
  4. A span can only be sponsored while every block in it is open: not proven, not being proven, not held.
  5. A held sponsorship becomes `proven` through submit() once its whole span is covered, and only then; its
     hold ends with it.

WHAT THIS DOES NOT COVER: real STARK verification (VERIFY_MODE=mock), and the bot (test_sponsor_bot.py).

Usage:
  python3 test_sponsor_holds.py            # assertions; exit 0 on success
  python3 test_sponsor_holds.py --control  # holds switched off; these tests MUST fail
"""
import base64
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

os.environ["COORD_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
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

server.init_db()

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


# The bridge serves every height here; witness availability is a separate concern (#261).
server.witness_available = lambda h: True
server.provable_tip = lambda: 1000

# A real signature, as in test_overlap_guard.py: submit() will not accept an unsigned receipt on a board
# that can verify one.
if server.HAVE_ED:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    _sk = Ed25519PrivateKey.generate()
    PUB = _sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    _sign = lambda m: _sk.sign(m).hex()                                            # noqa: E731
else:
    os.environ["COORD_ALLOW_UNSIGNED"] = "1"
    PUB, _sign = "00" * 32, lambda m: "00" * 64                                    # noqa: E731

if CONTROL:
    # Holds switched off: no sponsorship holds anything, and nothing marks one proven.
    server._sponsor_holds = lambda c, lo=None, hi=None: []
    server._sponsor_mark_proven = lambda c, lo, hi, now=None: []
    print("CONTROL: holds switched off -- the checks below MUST fail")


def reset():
    c = server.db()
    for t in ("vranges", "ranges", "sponsorships"):
        c.execute(f"DELETE FROM {t}")
    c.execute("DELETE FROM meta WHERE k='frontier_mark'")
    c.commit()
    c.close()
    server._frontier_invalidate()


def put(c, lo, hi, in_tip, in_b, out_tip, out_b, verified_at=None):
    rid = str(lo) if lo == hi else f"{lo}-{hi}"
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,verified_at) VALUES(?,?,?,'verified',?)",
              (rid, lo, hi, verified_at))
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,"
              "range_work,in_bhash,out_bhash) VALUES(?,?,?,?,?,'','t',0,0,'1',?,?)",
              (rid, lo, hi, in_tip, out_tip, in_b, out_b))


def chain(top):
    """An unbroken, seamable run of single-block proofs 1..top, so the frontier sits at `top`."""
    c = server.db()
    prev_tip, prev_b = server.GENESIS_TIP, "b0"
    for h in range(1, top + 1):
        put(c, h, h, prev_tip, prev_b, f"t{h}", f"b{h}")
        prev_tip, prev_b = f"t{h}", f"b{h}"
    c.commit()
    c.close()
    server._frontier_invalidate()


def sponsor(lo, hi, status="paid", paid=1000, minimum=1000, paid_ago=60):
    now = time.time()
    c = server.db()
    cur = c.execute("INSERT INTO sponsorships(lo,hi,name,status,created_at,min_usd,min_sats,paid_sats,paid_at)"
                    " VALUES(?,?,?,?,?,1,?,?,?)",
                    (lo, hi, f"sponsor {lo}-{hi}", status, now - paid_ago - 10, minimum, paid,
                     None if paid is None else now - paid_ago))
    c.commit()
    sid = cur.lastrowid
    c.close()
    return sid


def claim_live(h):
    now = time.time()
    c = server.db()
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at,last_beat)"
              " VALUES(?,?,?,'claimed',?,'erin',?,?)", (str(h), h, h, "ee" * 32, now - 30, now - 10))
    c.commit()
    c.close()


def status_of(sid):
    c = server.db()
    r = c.execute("SELECT status, proven_at FROM sponsorships WHERE id=?", (sid,)).fetchone()
    c.close()
    return r["status"], r["proven_at"]


def held_now():
    c = server.db()
    _, held = server.coverage_and_held(c, time.time())
    c.close()
    return held


def new_key(tag):
    """A signing key of its own: (pubkey, sign)."""
    if server.HAVE_ED:
        sk = Ed25519PrivateKey.generate()
        pub = sk.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
        return pub, (lambda m, sk=sk: sk.sign(m).hex())
    return f"{tag:02x}" * 32, (lambda m: "00" * 64)


def register(pub, sid):
    """What the sponsor bot does before a pod uses a key: record it against the sponsorship."""
    c = server.db()
    c.execute("INSERT OR REPLACE INTO sponsor_keys(pubkey, sponsorship_id, handle, created_at) VALUES(?,?,?,?)",
              (pub.lower(), sid, f"SPONSOR: sponsor {sid}", time.time()))
    c.commit()
    c.close()


def credited(rid):
    c = server.db()
    r = c.execute("SELECT pubkey FROM vranges WHERE id=?", (rid,)).fetchone()
    c.close()
    return r["pubkey"] if r else None


def submit(lo, hi, key=None):
    """Drive the real submit() end to end. Returns (code, obj)."""
    pub, sign = key or (PUB, _sign)
    rid = str(lo) if lo == hi else f"{lo}-{hi}"
    receipt = f"receipt-for-{rid}-{pub[:8]}".encode()
    return server.submit({"range": rid, "pubkey": pub, "handle": "tester",
                          "sig": sign(receipt), "receipt": base64.b64encode(receipt).decode()})


print("== 1. held blocks are never offered to a normal worker ==")
reset()
chain(10)
A = sponsor(11, 14)                                    # paid at least the minimum: holds
claim_live(15)                                         # a live claim: held the ordinary way
sponsor(16, 17, "requested", paid=None)                # unpaid request: holds nothing
sponsor(18, 18, "underpaid", paid=999)                 # settled below the minimum: kept as a donation, holds nothing
sponsor(19, 19, "paid", paid=999)                      # marked paid by hand below its minimum: holds nothing
sponsor(22, 23, "proving")                             # the bot is on it: still holds
held = held_now()
check(set(range(11, 15)) <= held and {22, 23} <= held,
      f"a paid sponsorship (11-14) and a proving one (22-23) are held (held: {sorted(held)})")
check(not ({16, 17, 18, 19} & held), "requested, underpaid, and paid-below-minimum rows hold nothing")
_, p = server.pick(None)
check(p.get("lo") == 16, f"pick() walks past the held 11-14 and the claimed 15 (suggested {p.get('lo')})")
got = []
for i in range(8):
    code, r = server.claim({"pubkey": f"{i:02x}" * 32, "handle": f"worker{i}", "nonce": f"n{i}"})
    got.append(int(r["range"]) if code == 200 else None)
check(got == [16, 17, 18, 19, 20, 21, 24, 25],
      f"eight claims hand out 16-21 then 24-25, never a held block (got {got})")

print("== 2. a held frontier blocker is not re-offered ==")
reset()
c = server.db()
prev_tip, prev_b = server.GENESIS_TIP, "b0"
for h in range(1, 6):                                  # 1..5 seam
    put(c, h, h, prev_tip, prev_b, f"t{h}", f"b{h}")
    prev_tip, prev_b = f"t{h}", f"b{h}"
put(c, 6, 6, "FORKTIP", "FORKB", "t6", "b6", verified_at=time.time() - 3600)   # right height, wrong predecessor
for h in range(7, 10):
    put(c, h, h, f"t{h-1}", f"b{h-1}", f"t{h}", f"b{h}")
c.commit()
c.close()
server._frontier_invalidate()
check(server._frontier_chain()[0] == 5, "the frontier stops at 5: block 6 is covered but cannot seam")
sponsor(6, 6)
code, r = server.claim({"pubkey": "p" * 64, "handle": "tester", "nonce": "fork"})
check(code == 200 and r.get("range") == "10",
      f"claim() does not re-offer the held blocker 6 and hands out 10 instead (got {code} {r.get('range')})")

print("== 3. the frontier's next block, held ==")
reset()
chain(5)
H = sponsor(6, 8, paid_ago=60)
b = server.state()["blocked"]
check(b["block"] == 6 and b.get("sponsorship") == H, f"the blocker is block 6, held for sponsorship #{H} (got {b})")
check(b["needs_attention"] is False and f"held for sponsorship #{H}" in (b.get("why") or ""),
      f"a fresh hold is not an alarm, and says whose it is (why={b.get('why')!r})")
c = server.db()
c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('frontier_mark',?)", (f"5:{time.time() - 7200}",))
c.commit()
c.close()
b = server.state()["blocked"]
check(b["stalled_for"] > server.CLAIM_TTL and b["needs_attention"] is False,
      f"two hours without the frontier moving is still not 'nobody holds it' while the hold is fresh "
      f"(stalled_for={b['stalled_for']}, why={b.get('why')!r})")
c = server.db()
c.execute("UPDATE sponsorships SET paid_at=? WHERE id=?", (time.time() - server.SPONSOR_HOLD_ALERT - 60, H))
c.commit()
c.close()
b = server.state()["blocked"]
check(b["needs_attention"] is True and "SPONSOR_HOLD_ALERT" in (b.get("why") or ""),
      f"a hold older than SPONSOR_HOLD_ALERT asks for a human (why={b.get('why')!r})")
code, d = server.block_detail(7)
check(code == 200 and (d.get("held") or {}).get("sponsorship") == H, f"/api/block/7 carries the hold (got {d.get('held')})")
check(server.block_detail(9)[1].get("held") is None, "a block outside the hold carries none")

print("== 4. only open blocks can be sponsored ==")
reset()
chain(10)
claim_live(12)
sponsor(14, 15)
sponsor(16, 16, "requested", paid=None)
sponsor(17, 17, "underpaid", paid=999)
server.SPONSOR_OPEN = True
for (lo, hi), want in (((5, 6), "block 5 is already proven"), ((11, 12), "block 12 is being proven"),
                       ((13, 15), "block 14 is already sponsored")):
    code, q = server.sponsor_quote(lo, hi)
    check(code == 409 and want in q.get("error", ""), f"blocks {lo}-{hi} are refused: {want} (got {code} {q})")
for lo, hi in ((11, 11), (13, 13), (16, 16), (17, 17), (18, 20)):
    code, q = server.sponsor_quote(lo, hi)
    check(code == 200, f"blocks {lo}-{hi} are open and can be quoted (got {code} {q})")
c = server.db()
before = c.execute("SELECT COUNT(*) FROM sponsorships").fetchone()[0]
c.close()
code, body = server.sponsor_request({"lo": 9, "hi": 11, "name": "Late", "amount_sats": 10 ** 9})
c = server.db()
after = c.execute("SELECT COUNT(*) FROM sponsorships").fetchone()[0]
c.close()
check(code == 409 and "already proven" in body.get("error", "") and after == before,
      f"a request over a proven block is refused and records nothing (got {code} {body}, rows {before} -> {after})")
code, body = server.sponsor_request({"lo": 13, "hi": 15, "name": "Second", "amount_sats": 10 ** 9})
check(code == 409 and "already sponsored" in body.get("error", ""), f"two sponsors cannot hold the same block (got {code} {body})")

print("== 5. a sponsorship is proven through submit(), once its whole span is covered ==")
reset()
chain(10)
P = sponsor(11, 14, "proving")
Q = sponsor(20, 21, "paid", paid=999)                  # below its minimum: never a hold, never moved
register(PUB, P)                                       # the bot's key for P (section 6 tests anyone else)
for h in (11, 12, 13):
    code, r = submit(h, h)
    check(code == 200, f"block {h} is accepted (got {code} {r.get('note')})")
check(status_of(P)[0] == "proving", "three of four blocks proven: still proving, still held")
check(set(range(11, 15)) <= held_now() or CONTROL, "the unproven block 14 is still held")
code, r = submit(14, 14)
st, proven_at = status_of(P)
check(code == 200 and st == "proven" and proven_at, f"the fourth block makes the sponsorship proven (got {st}, {proven_at})")
check(not (set(range(11, 15)) & held_now()) or not proven_at, "its hold is over")
for h in (20, 21):
    submit(h, h)
check(status_of(Q)[0] == "paid", "a row below its minimum is never moved: it held nothing")

print("== 6. only the sponsor's registered key can prove a held block ==")
reset()
chain(10)
S1 = sponsor(11, 12)
S2 = sponsor(13, 13)
S3 = sponsor(20, 22)
bot1, bot2, bot3, intruder = new_key(1), new_key(2), new_key(3), new_key(9)
register(bot1[0], S1)
register(bot2[0], S2)
register(bot3[0], S3)
code, r = submit(11, 11, intruder)
check(code == 403 and f"sponsorship #{S1}" in (r.get("error") or "") and credited("11") is None,
      f"another prover's proof of held block 11 is refused, and nobody is credited (got {code} {r}, credited {credited('11')})")
code, r = submit(11, 11, bot2)
check(code == 403 and credited("11") is None, f"a key registered for a DIFFERENT sponsorship is refused too (got {code} {r})")
code, r = submit(11, 11, bot1)
check(code == 200 and credited("11") == bot1[0], f"the sponsorship's own key proves block 11 (got {code})")
code, r = submit(12, 12, bot1)
check(code == 200 and status_of(S1)[0] == "proven", f"and block 12, which proves the sponsorship (got {code}, {status_of(S1)[0]})")
code, r = submit(13, 13, intruder)
check(code == 403 and f"sponsorship #{S2}" in (r.get("error") or ""), f"held block 13 is refused to another prover (got {code} {r})")
for h in (20, 21):
    submit(h, h, bot3)
check(status_of(S3)[0] == "paid" and 22 in held_now(), "S3's bot proves 20 and 21; block 22 is still held")
code, r = submit(20, 21, intruder)
check(code == 200, f"a FOLD of held blocks already proven takes nothing, so anyone may submit it (got {code} {r})")
code, r = submit(21, 22, intruder)
check(code == 409 or code == 403, f"a wide range reaching the unproven held block 22 is refused (got {code} {r})")
check(credited("22") is None, "and block 22 is credited to nobody")
code, r = submit(30, 30, intruder)
check(code == 200 and credited("30") == intruder[0], f"blocks that are not held are proven by anyone, as always (got {code})")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — holds switched off and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — holds were switched off and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("paid sponsorships hold their blocks for the bot, open blocks only can be sponsored, and a covered span is proven.")

#!/usr/bin/env python3
"""
The sponsor bot's API on the coordinator (#351): /api/bot/*.

WHY THIS EXISTS. The bot used to write coordinator.db directly, so a compromised bot could rewrite the board's
record. Now it has no database access and these routes are the only way it changes anything. So this test
attacks them: every request here goes over real HTTP to the real request handler, and the bot's key is the only
thing that should get through, only from the coordinator's own box, and only to make changes the coordinator
itself agrees with.

What is asserted:
  1. Authentication: a signed request from loopback is accepted. Refused: no signature; another key; a signed
     request whose body, path or query was changed; a replayed nonce; a stale timestamp; a request carrying
     X-Forwarded-For (a proxied request is never the bot); a peer that is not loopback; and every request while
     no bot key is configured.
  2. `paid` -> `proving` only for a sponsorship that holds (paid at least the minimum); never for `requested`,
     `underpaid` or a `paid` row below its minimum.
  3. A key is registered only for a sponsorship that holds, only with the handle its name gives, never for a
     second sponsorship; the trial key only as "SPONSOR: Hazync trial". A registered key may then use its handle.
  4. The queue lists holds only, oldest payment first, with the heights nobody has proven; block facts give
     coverage by key and time, live claims and holds; reconcile marks proven only fully covered holds.

WHAT THIS DOES NOT COVER: the bot's side (test_sponsor_bot.py drives the bot against this API).

Usage:
  python3 test_sponsor_bot_api.py            # assertions; exit 0 on success
  python3 test_sponsor_bot_api.py --control  # authentication and the transition checks removed; MUST fail
"""
import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

CONTROL = "--control" in sys.argv

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization as ser  # noqa: E402

os.environ["COORD_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

BOT = Ed25519PrivateKey.generate()
OTHER = Ed25519PrivateKey.generate()
_pubfile = os.path.join(tempfile.mkdtemp(prefix="botpub_"), "bot.pub")
with open(_pubfile, "w") as f:
    f.write(BOT.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex())
os.environ["SPONSOR_BOT_PUBKEY_FILE"] = _pubfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.init_db()
DB = os.environ["COORD_DB"]

if CONTROL:
    # No authentication, and the transitions take the caller's word.
    server.bot_auth_refusal = lambda *a, **k: None

    def _proving(body):
        c = server.db()
        c.execute("UPDATE sponsorships SET status='proving' WHERE id=?", (body.get("sponsorship_id"),))
        c.commit()
        c.close()
        return 200, {"ok": True}

    def _key(body):
        c = server.db()
        c.execute("INSERT OR REPLACE INTO sponsor_keys(pubkey, sponsorship_id, handle, created_at) VALUES(?,?,?,?)",
                  (str(body.get("pubkey")).lower(), body.get("sponsorship_id"), body.get("handle"), time.time()))
        c.commit()
        c.close()
        return 200, {"ok": True}
    server.bot_proving, server.bot_register_key = _proving, _key
    print("CONTROL: authentication and the transition checks removed -- the checks below MUST fail")

httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{httpd.server_address[1]}"

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


def call(method, path, body=None, key=BOT, sign_path=None, sign_body=None, ts=None, nonce=None, extra=None, signed=True):
    """(status, json). sign_path/sign_body sign something other than what is sent."""
    raw = b"" if body is None else json.dumps(body).encode()
    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or secrets.token_hex(16)
    headers = {"Content-Type": "application/json"}
    if signed:
        msg = server.bot_message(method, sign_path or path, ts, nonce, raw if sign_body is None else sign_body)
        headers.update({"X-Hazync-Bot-Ts": ts, "X-Hazync-Bot-Nonce": nonce, "X-Hazync-Bot-Sig": key.sign(msg).hex()})
    headers.update(extra or {})
    req = urllib.request.Request(BASE + path, data=raw if method == "POST" else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def q(sql, args=()):
    c = server.db()
    try:
        rows = c.execute(sql, args).fetchall()
        c.commit()
        return rows
    finally:
        c.close()


def sponsor(lo, hi, status="paid", paid=1000, minimum=1000, paid_at=100.0, name="Sponsor"):
    c = server.db()
    cur = c.execute("INSERT INTO sponsorships(lo,hi,name,status,created_at,min_usd,min_sats,paid_sats,paid_at)"
                    " VALUES(?,?,?,?,?,1,?,?,?)", (lo, hi, name, status, 1.0, minimum, paid, paid_at))
    c.commit()
    sid = cur.lastrowid
    c.close()
    return sid


def status_of(sid):
    return q("SELECT status FROM sponsorships WHERE id=?", (sid,))[0]["status"]


def pub(k):
    return k.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()


print("== 1. authentication ==")
code, _ = call("GET", "/api/bot/queue")
check(code == 200, f"a signed request from the coordinator's own box is accepted ({code})")
code, _ = call("GET", "/api/bot/queue", signed=False)
check(code == 401, f"no signature -> 401 ({code})")
code, _ = call("GET", "/api/bot/queue", key=OTHER)
check(code == 403, f"another key's signature -> 403 ({code})")
code, _ = call("POST", "/api/bot/reconcile", {"all": True}, sign_body=b"{}")
check(code == 403, f"a changed body -> 403 ({code})")
code, _ = call("GET", "/api/bot/blocks?h=1", sign_path="/api/bot/queue")
check(code == 403, f"a changed path -> 403 ({code})")
code, _ = call("GET", "/api/bot/blocks?h=2", sign_path="/api/bot/blocks?h=1")
check(code == 403, f"a changed query -> 403 ({code})")
n = secrets.token_hex(16)
first, _ = call("GET", "/api/bot/queue", nonce=n)
again, _ = call("GET", "/api/bot/queue", nonce=n)
check(first == 200 and again == 403, f"the same request again (a replay) -> 403 ({first}, then {again})")
code, _ = call("GET", "/api/bot/queue", ts=int(time.time()) - server.SPONSOR_BOT_SKEW - 30)
check(code == 401, f"a stale timestamp -> 401 ({code})")
code, _ = call("GET", "/api/bot/queue", extra={"X-Forwarded-For": "127.0.0.1"})
check(code == 403, f"a request that came through a proxy (X-Forwarded-For) -> 403, even signed ({code})")


class _H(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


ts, nonce = str(int(time.time())), secrets.token_hex(16)
hdr = _H({"X-Hazync-Bot-Ts": ts, "X-Hazync-Bot-Nonce": nonce,
          "X-Hazync-Bot-Sig": BOT.sign(server.bot_message("GET", "/api/bot/queue", ts, nonce, b"")).hex()})
refused = server.bot_auth_refusal("198.51.100.4", hdr, "GET", "/api/bot/queue", b"")
check(bool(refused) and refused[0] == 403, f"a correctly signed request from any other address -> 403 ({refused})")
saved = server.SPONSOR_BOT_PUBKEY_FILE
server.SPONSOR_BOT_PUBKEY_FILE = ""
code, _ = call("GET", "/api/bot/queue")
server.SPONSOR_BOT_PUBKEY_FILE = saved
check(code == 503, f"no bot key configured -> 503 for everyone ({code})")
code, _ = call("POST", "/api/bot/proving", {"sponsorship_id": 1}, signed=False)
check(code == 401, f"a POST without a signature changes nothing -> 401 ({code})")

print("== 2. paid -> proving only while held ==")
held = sponsor(10, 12)
req = sponsor(20, 20, status="requested", paid=None, paid_at=None)
under = sponsor(30, 30, status="underpaid", paid=999)
short = sponsor(40, 40, status="paid", paid=999)
code, _ = call("POST", "/api/bot/proving", {"sponsorship_id": held})
check(code == 200 and status_of(held) == "proving", f"a held sponsorship moves to proving ({code}, {status_of(held)})")
for sid, what in ((req, "requested"), (under, "underpaid"), (short, "paid below its minimum")):
    code, _ = call("POST", "/api/bot/proving", {"sponsorship_id": sid})
    check(code == 409 and status_of(sid) == ("paid" if sid == short else what),
          f"a {what} sponsorship is refused and unchanged ({code}, {status_of(sid)})")
code, _ = call("POST", "/api/bot/proving", {"sponsorship_id": held})
check(code == 200 and status_of(held) == "proving", "asking again for one already proving is harmless")

print("== 3. keys ==")
named = sponsor(50, 51, name="O'Brien <b>&co")
k1, k2, kt = pub(Ed25519PrivateKey.generate()), pub(Ed25519PrivateKey.generate()), pub(Ed25519PrivateKey.generate())
code, body = call("POST", "/api/bot/key", {"pubkey": k1, "sponsorship_id": named, "handle": "SPONSOR: Someone else"})
check(code == 400 and not server.sponsor_key_registered(k1),
      f"a handle that is not the sponsorship's own is refused ({code}, want {body.get('handle')!r})")
code, _ = call("POST", "/api/bot/key", {"pubkey": k1, "sponsorship_id": named, "handle": "SPONSOR: OBrien bco"})
check(code == 200 and server.sponsor_key_registered(k1), f"the handle its name gives is registered ({code})")
check(not server.handle_refused("SPONSOR: OBrien bco", k1) and server.handle_refused("SPONSOR: OBrien bco", k2),
      "that key may now use the SPONSOR handle, and no other key may")
code, _ = call("POST", "/api/bot/key", {"pubkey": k1, "sponsorship_id": named, "handle": "SPONSOR: OBrien bco"})
check(code == 200 and len(q("SELECT * FROM sponsor_keys WHERE pubkey=?", (k1,))) == 1, "registering again is harmless")
code, _ = call("POST", "/api/bot/key", {"pubkey": k1, "sponsorship_id": held, "handle": "SPONSOR: Sponsor"})
check(code == 409 and q("SELECT sponsorship_id FROM sponsor_keys WHERE pubkey=?", (k1,))[0][0] == named,
      f"a key cannot be moved to a second sponsorship ({code})")
code, _ = call("POST", "/api/bot/key", {"pubkey": k2, "sponsorship_id": under, "handle": "SPONSOR: Sponsor"})
check(code == 409 and not server.sponsor_key_registered(k2), f"no key for a sponsorship that does not hold ({code})")
code, _ = call("POST", "/api/bot/key", {"pubkey": kt, "sponsorship_id": None, "handle": "SPONSOR: Anything"})
check(code == 400 and not server.sponsor_key_registered(kt), f"the trial key only with the trial handle ({code})")
code, _ = call("POST", "/api/bot/key", {"pubkey": kt, "sponsorship_id": None, "handle": "SPONSOR: Hazync trial"})
check(code == 200 and server.sponsor_key_registered(kt), f"the trial key with its handle ({code})")

print("== 4. queue, block facts, reconcile ==")
for t in ("sponsorships", "vranges", "ranges", "sponsor_keys"):
    q(f"DELETE FROM {t}")
a = sponsor(100, 103, paid_at=300)
b = sponsor(200, 201, paid_at=100)
sponsor(300, 300, status="underpaid", paid=1)
now = time.time()
q("INSERT INTO vranges(id,lo,hi,pubkey,handle,ts) VALUES('101',101,101,'kk','h',?)", (now - 50,))
q("INSERT INTO vranges(id,lo,hi,pubkey,handle,ts) VALUES('200-201',200,201,'jj','h',?)", (now - 40,))
q("INSERT INTO ranges(id,lo,hi,status,assignee,claimed_at) VALUES('102',102,102,'claimed','p',?)", (now,))
q("INSERT INTO ranges(id,lo,hi,status,assignee,claimed_at) VALUES('103',103,103,'claimed','p',?)", (now - server.CLAIM_TTL - 60,))
code, body = call("GET", "/api/bot/queue")
got = [(r["id"], r["todo"]) for r in body.get("sponsorships", [])]
check(code == 200 and got == [(b, []), (a, [100, 102, 103])],
      f"holds only, oldest payment first, with the heights nobody has proven ({got})")
code, body = call("GET", "/api/bot/blocks?h=101,102,103,300")
bl = body.get("blocks", {})
check(code == 200 and bl["101"]["covered"] and bl["101"]["proofs"][0]["pubkey"] == "kk" and bl["101"]["held"] == a,
      f"a proven, held block: covered, by which key, held for its sponsorship ({bl.get('101')})")
check(bl["102"]["claimed"] and not bl["103"]["claimed"] and not bl["300"]["covered"] and bl["300"]["held"] is None,
      "a live claim counts, a stale one does not, and an underpaid span holds nothing")
code, body = call("GET", "/api/bot/blocks?h=" + ",".join(str(i) for i in range(server.SPONSOR_BOT_MAX_HEIGHTS + 1)))
check(code == 400, f"too many heights at once -> 400 ({code})")
code, body = call("GET", f"/api/bot/sponsorship/{a}")
check(code == 200 and body["held"] is True and body["name"] == "Sponsor", f"one sponsorship, and whether it holds ({body})")
code, body = call("POST", "/api/bot/reconcile", {})
check(code == 200 and body.get("proven") == [b] and status_of(b) == "proven" and status_of(a) == "paid",
      f"reconcile marks proven only the fully covered hold ({body})")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — authentication and transition checks removed and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the checks were removed and every test still passed.")
    sys.exit(1)
if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("only the bot's key, from the coordinator's own box, makes only the changes the coordinator agrees with.")

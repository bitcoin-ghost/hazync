#!/usr/bin/env python3
"""
Sponsorship payments through BTCPay (docs/SPONSORSHIP.md, "Payments").

The coordinator talks to a FAKE BTCPay Greenfield server here, over real HTTP, shaped from BTCPay's own
controllers and swagger (GreenfieldInvoiceController, GreenfieldStoreRatesController): the store-scoped invoice
routes, `Authorization: token <key>`, decimal amounts as strings, payment statuses Processing/Settled/Invalid.

What is asserted:
  1. Payments are connected only with a URL, a store and a readable key file; then GET /api/sponsor says
     payments: true and the bitcoin price comes from BTCPay's store rate, not SPONSOR_BTC_USD.
  2. A request opens an invoice for the pledge in SATS, authenticated with the key, sends the sponsor back to
     their private link, and answers with the checkout link. The row is `invoiced`.
  3. An open invoice holds its blocks: no claim offers them and no other sponsor can take them. The hold is
     soft: a proof of them is still accepted from any prover. It ends when the invoice expires.
  4. The poller: settled at least the minimum -> paid (public, and a real hold); a payment still confirming
     keeps the hold past expiry; expired with nothing -> expired; expired with too little -> underpaid, and
     the rest arriving later -> paid; a late payment after expiry is CREDITED; a span that others finished
     meanwhile goes straight to proven. Payments are recorded with their ids. A repeated poll changes nothing.
  5. BTCPay down, or refusing the key, leaves nothing recorded and nothing held (503).
  6. Unpaid holds are capped per address and in total (429). The address is the handler's, never the body's.
  7. A hand-marked invoice is left for the operator.

WHAT THIS DOES NOT COVER: a real BTCPay, real bitcoin, or the site's checkout redirect. Those are checked on the
live box once the store has a wallet.

Usage:
  python3 test_sponsor_payments.py            # assertions; exit 0 on success
  python3 test_sponsor_payments.py --control  # payments disconnected; these tests MUST fail
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

CONTROL = "--control" in sys.argv

KEY = "test-greenfield-key-0123456789"
STORE = "StoreXYZ"

# ---- the fake BTCPay -------------------------------------------------------------------------------------------
FAKE = {"up": True, "rate": "100000", "invoices": {}, "seen": [], "next": 1}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        return self.headers.get("Authorization") == f"token {KEY}"

    def do_GET(self):
        u = urlparse(self.path)
        FAKE["seen"].append(("GET", u.path, self.headers.get("Authorization")))
        if not FAKE["up"]:
            return self._send(503, {"message": "down"})
        if not self._auth():
            return self._send(401, {"code": "unauthenticated"})
        base = f"/api/v1/stores/{STORE}"
        if u.path == base + "/rates":
            pairs = parse_qs(u.query).get("currencyPair", [])
            return self._send(200, [{"currencyPair": p, "errors": [], "rate": FAKE["rate"]} for p in pairs])
        if u.path.startswith(base + "/invoices/"):
            rest = u.path[len(base + "/invoices/"):].split("/")
            inv = FAKE["invoices"].get(rest[0])
            if not inv:
                return self._send(404, {"code": "invoice-not-found"})
            if len(rest) == 1:
                return self._send(200, {k: inv[k] for k in ("id", "status", "additionalStatus", "checkoutLink",
                                                            "expirationTime", "amount", "currency")})
            if rest[1:] == ["payment-methods"]:
                by = {}
                for p in inv["payments"]:
                    by.setdefault(p["method"], []).append(
                        {"id": p["id"], "value": p["value"], "status": p["status"], "receivedDate": int(time.time())})
                return self._send(200, [{"paymentMethodId": m, "currency": "BTC", "payments": ps}
                                        for m, ps in by.items()])
        return self._send(404, {"code": "not-found"})

    def do_POST(self):
        u = urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FAKE["seen"].append(("POST", u.path, self.headers.get("Authorization"), body))
        if not FAKE["up"]:
            return self._send(503, {"message": "down"})
        if not self._auth():
            return self._send(401, {"code": "unauthenticated"})
        if u.path == f"/api/v1/stores/{STORE}/invoices":
            iid = f"INV{FAKE['next']}"
            FAKE["next"] += 1
            exp = int(time.time()) + 60 * int(body["checkout"]["expirationMinutes"])
            FAKE["invoices"][iid] = {"id": iid, "status": "New", "additionalStatus": "None", "amount": body["amount"],
                                     "currency": body["currency"], "expirationTime": exp,
                                     "checkoutLink": f"https://pay.example/i/{iid}", "payments": [], "body": body}
            return self._send(200, FAKE["invoices"][iid])
        return self._send(404, {"code": "not-found"})


fake = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=fake.serve_forever, daemon=True).start()

# ---- the coordinator -------------------------------------------------------------------------------------------
os.environ["COORD_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ["VRANGES_CACHE_TTL"] = "0"
os.environ.setdefault("TIP_HEIGHT", "1000")
os.environ.pop("SPONSOR_PRICE_BANDS", None)          # the default ladder: $1 a block up to 200,000
os.environ.pop("SPONSOR_BTC_USD", None)              # the price must come from BTCPay
os.environ["SPONSOR_OPEN"] = "1"
os.environ["SPONSOR_RATE_TTL"] = "0"
os.environ["SPONSOR_OPEN_INVOICES_PER_IP"] = "2"
os.environ["SPONSOR_UNPAID_HOLD_MAX"] = "30"
os.environ["BTCPAY_URL"] = f"http://127.0.0.1:{fake.server_address[1]}"
os.environ["BTCPAY_STORE_ID"] = STORE
_keyfile = tempfile.NamedTemporaryFile("w", suffix=".key", delete=False)
_keyfile.write(KEY + "\n")
_keyfile.close()
os.environ["BTCPAY_API_KEY_FILE"] = _keyfile.name
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.init_db()
server.witness_available = lambda h: True
server.provable_tip = lambda: 1000

if CONTROL:
    # Payments disconnected: requests only record, nothing is invoiced, polled or held.
    server.payments_enabled = lambda: False
    print("CONTROL: payments disconnected -- the checks below MUST fail")

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


if server.HAVE_ED:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    def new_key():
        sk = Ed25519PrivateKey.generate()
        pub = sk.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                           format=serialization.PublicFormat.Raw).hex()
        return pub, (lambda m, sk=sk: sk.sign(m).hex())
else:
    os.environ["COORD_ALLOW_UNSIGNED"] = "1"
    server.COORD_ALLOW_UNSIGNED = True if hasattr(server, "COORD_ALLOW_UNSIGNED") else None

    def new_key():
        return os.urandom(32).hex(), (lambda m: "00" * 64)


def I(iid):
    """The fake's invoice, or a scratch one when there is none (the control opens no invoices), so a control run
    reaches its report instead of dying on the first missing invoice."""
    return FAKE["invoices"].get(iid) or {"payments": [], "status": "", "additionalStatus": ""}


def reset():
    c = server.db()
    for t in ("vranges", "ranges", "sponsorships", "sponsor_payments"):
        c.execute(f"DELETE FROM {t}")
    c.commit()
    c.close()
    server._frontier_invalidate()
    server._open_invoices_by_ip.clear()
    FAKE.update(up=True, invoices={}, seen=[])


def row(sid):
    c = server.db()
    r = c.execute("SELECT * FROM sponsorships WHERE id=?", (sid,)).fetchone()
    c.close()
    return dict(r) if r else None


def held_now(now=None):
    c = server.db()
    _, held = server.coverage_and_held(c, now or time.time())
    c.close()
    return held


def request(lo, hi, sats, ip="198.51.100.7", name="Satoshi"):
    return server.sponsor_request({"lo": lo, "hi": hi, "name": name, "amount_sats": sats, "_client_ip": ip})


def pay(iid, sats, status="Settled", pid=None, method="BTC-CHAIN"):
    I(iid)["payments"].append(
        {"id": pid or f"tx{len(I(iid)['payments'])}-0", "value": f"{sats / 1e8:.8f}",
         "status": status, "method": method})


def invoice_of(sid):
    return (row(sid) or {}).get("invoice_id")


def submit(lo, key):
    pub, sign = key
    receipt = f"receipt-for-{lo}-{pub[:8]}".encode()
    import base64
    return server.submit({"range": str(lo), "pubkey": pub, "handle": "tester", "sig": sign(receipt),
                          "receipt": base64.b64encode(receipt).decode()})


print("== 1. connected: payments on, price from BTCPay ==")
reset()
info = server.sponsor_info()
check(info["payments"] is True, f"GET /api/sponsor says payments: true (got {info['payments']})")
check(info["btc_usd"] == 100000, f"the bitcoin price is BTCPay's store rate (got {info['btc_usd']})")
FAKE["rate"] = "50000"
code, q = server.sponsor_quote(11, 12)
check(code == 200 and q["min_sats"] == 4000 and q["btc_usd"] == 50000,
      f"a quote for 2 blocks at $50,000 is 4,000 sats (got {code} {q.get('min_sats')} at {q.get('btc_usd')})")
FAKE["rate"] = "100000"

print("== 2. a request opens an invoice for the pledge in sats ==")
reset()
code, r = request(11, 13, 5000)
check(code == 202 and r.get("status") == "invoiced" and str(r.get("checkout_url", "")).startswith("https://pay.example/i/"),
      f"202, invoiced, with a checkout link (got {code} {r.get('status')} {r.get('checkout_url')})")
posts = [s for s in FAKE["seen"] if s[0] == "POST"]
body = posts[-1][3] if posts else {}
check(bool(posts) and posts[-1][1] == f"/api/v1/stores/{STORE}/invoices" and posts[-1][2] == f"token {KEY}",
      "the invoice is created on the store's route with the key")
check(body.get("amount") == "5000" and body.get("currency") == "SATS",
      f"priced at the pledge, in SATS (got {body.get('amount')} {body.get('currency')})")
check(str((body.get("checkout") or {}).get("redirectURL", "")).endswith(f"#t={r.get('token')}"),
      "the checkout sends the sponsor back to their private link")
sid = r.get("id")
check((row(sid) or {}).get("status") == "invoiced" and invoice_of(sid) == "INV1", "the row is invoiced with its invoice id")
code, st = server.sponsor_status(r.get("token", "x" * 20))
check(code == 200 and st.get("checkout_url") == r.get("checkout_url"), "the private link can get back to the checkout")
check(sid not in [s["id"] for s in server.sponsors_public()["sponsorships"]], "an unpaid sponsorship is not public")

print("== 3. an open invoice holds its blocks, softly, until it expires ==")
check({11, 12, 13} <= held_now(), f"blocks 11-13 are held while the invoice is open (held: {sorted(held_now())[:10]})")
code, e = request(13, 14, 5000, ip="203.0.113.9")
check(code == 409, f"another sponsor cannot take block 13 while it waits for payment (got {code} {e.get('error')})")
code, sr = server.sponsor_quote(12, 12)
check(code == 409, f"a quote for a block waiting on payment is refused too (got {code})")
k = new_key()
code, sub = submit(12, k)
check(code == 200, f"the hold is soft: any prover's proof of block 12 is still accepted (got {code} {sub.get('error')})")
exp = (row(sid) or {}).get("hold_until") or 0
check(not ({11, 13} & held_now(exp + 1)), "after the invoice expires the blocks are free again")

print("== 4a. paid at least the minimum -> paid, public, a real hold ==")
reset()
code, r = request(21, 22, 3000)
sid, iid = r.get("id"), invoice_of(r.get("id"))
pay(iid, 3000, "Processing", pid="abc123-0")
moved = server.sponsor_payments_poll()
check((row(sid) or {}).get("status") == "invoiced", "a payment still confirming leaves it invoiced, even at the full amount")
far = time.time() + server.SPONSOR_INVOICE_MINUTES * 60 + 600
check((row(sid) or {}).get("hold_until", 0) > far, "and keeps its hold past the invoice's expiry")
for _p in I(iid)["payments"]:
    _p["status"] = "Settled"                          # the same payment confirms
I(iid)["status"] = "Settled"
moved = server.sponsor_payments_poll()
rr = row(sid) or {}
check(moved.get(sid) == "paid" and rr.get("status") == "paid" and rr.get("paid_sats") == 3000 and rr.get("paid_at"),
      f"settled 3,000 against a 2,000 minimum -> paid with paid_sats and paid_at (got {moved} {rr.get('status')} {rr.get('paid_sats')})")
check(sid in [s["id"] for s in server.sponsors_public()["sponsorships"]], "and it is public")
c = server.db()
check(server._hold_refusal(c, 21, 21, k[0]) is not None, "and a hard hold: another key's proof of block 21 is refused")
pr = c.execute("SELECT payment_id, sats, status FROM sponsor_payments WHERE sponsorship_id=?", (sid,)).fetchall()
c.close()
check([tuple(x) for x in pr] == [("abc123-0", 3000, "Settled")], f"the payment is recorded by its id (got {[tuple(x) for x in pr]})")
check(server.sponsor_payments_poll() == {} and (row(sid) or {}).get("status") == "paid", "a repeated poll changes nothing")

print("== 4b. expired with nothing -> expired; a late payment is credited ==")
reset()
code, r = request(31, 31, 1000)
sid, iid = r.get("id"), invoice_of(r.get("id"))
I(iid)["status"] = "Expired"
server.sponsor_payments_poll()
check((row(sid) or {}).get("status") == "expired", f"expired with no payment -> expired (got {(row(sid) or {}).get('status')})")
pay(iid, 1000, "Settled")
I(iid)["additionalStatus"] = "PaidLate"
moved = server.sponsor_payments_poll()
check(moved.get(sid) == "paid", f"paid after expiry -> paid, credited (got {moved})")

print("== 4c. too little -> underpaid, then the rest arrives -> paid ==")
reset()
code, r = request(41, 42, 2000)
sid, iid = r.get("id"), invoice_of(r.get("id"))
pay(iid, 500)
I(iid).update(status="Expired", additionalStatus="PaidPartial")
server.sponsor_payments_poll()
rr = row(sid) or {}
check(rr.get("status") == "underpaid" and rr.get("paid_sats") == 500, f"500 of 2,000 -> underpaid (got {rr.get('status')} {rr.get('paid_sats')})")
check(not ({41, 42} & held_now()) and sid not in [s["id"] for s in server.sponsors_public()["sponsorships"]],
      "underpaid holds nothing and shows no name")
pay(iid, 1500, pid="tx-rest-0")
moved = server.sponsor_payments_poll()
check(moved.get(sid) == "paid" and (row(sid) or {}).get("paid_sats") == 2000, f"the rest arrives -> paid with 2,000 (got {moved})")

print("== 4d. paid late for a span others finished -> proven at once ==")
reset()
code, r = request(51, 51, 1000)
sid, iid = r.get("id"), invoice_of(r.get("id"))
I(iid)["status"] = "Expired"
server.sponsor_payments_poll()
code, sub = submit(51, new_key())
check(code == 200, f"with the invoice expired, a prover proves block 51 (got {code})")
pay(iid, 1000)
moved = server.sponsor_payments_poll()
check((row(sid) or {}).get("status") == "proven", f"the late payment credits it and the finished span is proven (got {(row(sid) or {}).get('status')})")

print("== 5. BTCPay down or refusing the key: nothing recorded, nothing held ==")
reset()
FAKE["up"] = False
code, r = request(61, 62, 2000)
c = server.db()
n = c.execute("SELECT COUNT(*) FROM sponsorships").fetchone()[0]
c.close()
check(code == 503 and n == 0 and not ({61, 62} & held_now()), f"BTCPay down -> 503, no row, no hold (got {code}, {n} rows)")
FAKE["up"] = True
with open(_keyfile.name, "w") as f:
    f.write("wrong-key\n")
code, r = request(61, 62, 2000)
with open(_keyfile.name, "w") as f:
    f.write(KEY + "\n")
check(code == 503, f"a refused key -> 503 (got {code})")
check(all(KEY not in json.dumps(x) for x in (r,)), "the error does not carry the key")

print("== 6. unpaid holds are capped ==")
reset()
a = request(101, 101, 1000, ip="192.0.2.1")[0]
b = request(102, 102, 1000, ip="192.0.2.1")[0]
code, e = request(103, 103, 1000, ip="192.0.2.1")
check((a, b, code) == (202, 202, 429), f"a third open invoice from one address -> 429 (got {a} {b} {code})")
code, e = request(110, 139, 30000, ip="192.0.2.2")
check(code == 429, f"30 more unpaid blocks on top of 2 goes over the cap of 30 -> 429 (got {code})")


class _Req:
    def __init__(self):
        self.client_address = ("192.0.2.50", 1)
        self.headers = {}


body = {"lo": 150, "hi": 150, "name": "x", "amount_sats": 1000, "_client_ip": "10.9.9.9"}
seen = {}
_orig = server.sponsor_request
server.sponsor_request = lambda b: (seen.update(b), (400, {}))[1]
h = server.H.__new__(server.H)
h.path, h.client_address = "/api/sponsor", ("192.0.2.50", 1)
h.headers = {"Content-Length": str(len(json.dumps(body)))}
import io
h.rfile = io.BytesIO(json.dumps(body).encode())
h._send = lambda code, obj=None, **kw: None
server.rate_ok = lambda *a, **k: True
h.do_POST()
server.sponsor_request = _orig
check(seen.get("_client_ip") == "192.0.2.50", f"the handler sets the address itself; a body cannot choose it (got {seen.get('_client_ip')})")

print("== 7. a hand-marked invoice is left for the operator ==")
reset()
code, r = request(201, 201, 1000)
sid, iid = r.get("id"), invoice_of(r.get("id"))
I(iid).update(status="Settled", additionalStatus="Marked")
check(server.sponsor_payments_poll() == {} and (row(sid) or {}).get("status") == "invoiced",
      "marked settled by hand with no payment: not moved")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — payments disconnected and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — payments were disconnected and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("requests open BTCPay invoices that hold their blocks, and the poller credits what settles, late or not.")

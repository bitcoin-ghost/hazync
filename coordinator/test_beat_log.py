#!/usr/bin/env python3
"""Rejected heartbeats are logged with the caller, the reason, and the range's real state.

Drives the REAL HTTP handler on a loopback port, so what is tested is the wiring in do_POST and not a
function nobody calls. Each case is a beat the coordinator must reject, paired with the line an
operator needs to diagnose it -- the questions the first third-party contributor's ten hours of
rejected beats (bip-448, 2026-09-11) left unanswerable.

  python3 test_beat_log.py            # must PASS
  python3 test_beat_log.py --control  # disables the log hook; the tests MUST FAIL
"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ["BEAT_LOG_WINDOW"] = "2"          # short, so the dedup window can be crossed in a test
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))
CONTROL = "--control" in sys.argv

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    MODE = "ed25519"
except Exception:                            # no lib: the server's documented dev mode, stated loudly
    os.environ["COORD_ALLOW_UNSIGNED"] = "1"
    MODE = "unsigned (COORD_ALLOW_UNSIGNED -- no cryptography lib)"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402
server.init_db()
print(f"signature mode: {MODE}")

if CONTROL:
    server.log_beat_rejection = lambda *a, **k: False   # the hook fires, logs nothing
    print("CONTROL: rejection logging disabled -- the checks below MUST fail")


class Key:
    def __init__(self):
        if MODE == "ed25519":
            self.sk = Ed25519PrivateKey.generate()
            self.pk = self.sk.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        else:
            self.sk, self.pk = None, os.urandom(32).hex()

    def sign(self, msg: bytes) -> str:
        return self.sk.sign(msg).hex() if self.sk else "00" * 64


A, B = Key(), Key()
now = time.time()
c = server.db()
c.execute("INSERT INTO ranges(id, lo, hi, status, assignee, claimed_at, last_beat) VALUES (?,?,?,?,?,?,?)",
          ("500", 500, 500, "claimed", A.pk, now - 30, now - 20))
c.execute("INSERT INTO ranges(id, lo, hi, status, assignee, claimed_at) VALUES (?,?,?,?,?,?)",
          ("501", 501, 501, "verified", A.pk, now - 900))
c.execute("INSERT INTO ranges(id, lo, hi, status, assignee, claimed_at) VALUES (?,?,?,?,?,?)",
          ("502", 502, 502, "claimed", A.pk, now - server.CLAIM_MAX - 60))
c.execute("INSERT INTO contributors(pubkey, handle, blocks, first_seen) VALUES (?,?,?,?)",
          (B.pk, "bip-test", 1, now))
c.commit()
c.close()

httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{httpd.server_address[1]}"

# The server prints from its request threads; capture that stdout.
real_stdout, cap = sys.stdout, io.StringIO()


def beat(key, rid, ts=None):
    ts = int(time.time()) if ts is None else ts
    body = {"range": rid, "pubkey": key.pk, "ts": ts, "sig": key.sign(f"{rid}:{ts}".encode())}
    req = urllib.request.Request(URL + "/api/beat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def lines_after(fn):
    start = len(cap.getvalue())
    code = fn()
    time.sleep(0.05)
    return code, [l for l in cap.getvalue()[start:].splitlines() if l.startswith("[beat-rejected]")]


fails = 0


def check(cond, what):
    global fails
    real_stdout.write(("  ok   " if cond else "  FAIL ") + what + "\n")
    fails += 0 if cond else 1


sys.stdout = cap
try:
    code, L = lines_after(lambda: beat(B, "500"))
    check(code == 409, "B beating A's claim is rejected 409")
    check(len(L) == 1 and "state=claimed" in L[0] and f"assignee={A.pk[:16]}" in L[0]
          and f"pubkey={B.pk[:16]}" in L[0] and "handle='bip-test'" in L[0],
          "  ...logged with caller, handle, and who REALLY holds the range")

    code, L = lines_after(lambda: beat(B, "777"))
    check(code == 409 and len(L) == 1 and "state=no-such-range" in L[0],
          "a beat for a range that does not exist says so")

    code, L = lines_after(lambda: beat(A, "501"))
    check(code == 409 and len(L) == 1 and "state=verified" in L[0] and "assignee=caller" in L[0],
          "a beat for an already-verified range says verified, and that it WAS the caller's")

    code, L = lines_after(lambda: beat(A, "502"))
    check(code == 409 and len(L) == 1 and "CLAIM_MAX" in L[0] and "assignee=caller" in L[0],
          "a claim held past CLAIM_MAX is logged as the caller's, with the reason")

    code, L = lines_after(lambda: beat(A, "500", ts=int(time.time()) - 10_000))
    check(code == 400 and len(L) == 1 and "outside" in L[0] and "state=" not in L[0],
          "a non-409 rejection is logged with its reason (no DB lookup)")

    code, L = lines_after(lambda: beat(A, "500"))
    check(code == 200 and L == [], "a GOOD beat logs nothing")

    code, L = lines_after(lambda: beat(B, "500"))
    check(code == 409 and L == [], "an identical rejection inside the window is suppressed")
    time.sleep(2.2)
    code, L = lines_after(lambda: beat(B, "500"))
    check(code == 409 and len(L) == 1 and "(+1 identical since the last line)" in L[0],
          "after the window it logs again, carrying the suppressed count")
finally:
    sys.stdout = real_stdout
    httpd.shutdown()

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)      # the control passes only if the checks caught the broken hook
sys.exit(1 if fails else 0)

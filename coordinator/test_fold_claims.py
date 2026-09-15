#!/usr/bin/env python3
"""
Fold claims (#333): a folder reserves the pair it is about to fold for FOLD_CLAIM_TTL seconds, so the other folders
are not offered it.

Measured before this (2026-09-15): three folders drawing at random from the same 8 candidates threw away 1,809 valid
folds in 14 h, 10-25% of each folder's work, at ~14 s of GPU a fold. This checks both halves of the fix:

  A. `GET /api/foldable` offers up to 32 candidates by default, not 8.
  B. `POST /api/foldclaim` grants, conflicts, caps, expires and releases; a claimed pair is not offered; only a key
     with proven work may claim; a valid fold is still accepted from anyone; the board shows live fold claims; and
     the worker folds UNCLAIMED whenever it cannot get a claim, so a claim can never stop folding.

NOT covered: actual folding (a prove op; CI has no GPU) and real STARK verification (VERIFY_MODE=mock, as in
test_spine_fold.py).

Usage:
  python3 test_fold_claims.py            # assertions; exit 0 on success
  python3 test_fold_claims.py --control  # claims granted but ignored, as before #333: the tests MUST fail
"""
import base64
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:
    sys.exit("test_fold_claims.py needs the `cryptography` package: fold claims are signed, and a test that cannot "
             "sign would only exercise the unsigned development path.")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()
if CONTROL:
    server._CONTROL_FOLD_CLAIMS_IGNORED = True
    print("CONTROL: fold claims are granted but ignored, as before #333 -- the checks below MUST fail")

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


def keypair():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    return sk, pk


SK_A, PK_A = keypair()     # alice: proves the leaves, so she has proven work
SK_B, PK_B = keypair()     # bob: given one verified submission below
SK_C, PK_C = keypair()     # carol: no proven work at all


def submit_as(sk, pk, lo, hi, handle="t"):
    """Drive the real submit() with a mock receipt. Returns (code, obj)."""
    rid = str(lo) if lo == hi else f"{lo}-{hi}"
    receipt = f"receipt-for-{rid}".encode()
    return server.submit({"range": rid, "pubkey": pk, "handle": handle, "sig": sk.sign(receipt).hex(),
                          "receipt": base64.b64encode(receipt).decode()})


def fclaim(sk, pk, result, handle="t", nonce=None, ts=None, msg=None):
    nonce = os.urandom(8).hex() if nonce is None else nonce
    ts = int(time.time()) if ts is None else ts
    m = msg if msg is not None else f"foldclaim:{result}:{nonce}:{ts}".encode()
    return server.fold_claim({"pubkey": pk, "handle": handle, "result": result, "nonce": nonce, "ts": ts,
                              "sig": sk.sign(m).hex()})


def offered():
    return {p["result"] for p in server.foldable(32)}


# ── setup: 80 proven leaves (alice), one verified submission for bob, nothing for carol ───────────────────────────
for i in range(1, 81):
    code, obj = submit_as(SK_A, PK_A, i, i, "alice")
    if code != 200:
        sys.exit(f"setup: could not submit leaf {i}: {code} {obj}")
c = server.db()
c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
          " VALUES('seed',?,'bob','','',1,'seed',0)", (PK_B,))
c.commit()
c.close()

print("== A: more candidates ==")
check(server.FOLDABLE_DEFAULT == 32, "the default offer is 32 candidates")
check(len(server.foldable()) == 32, "with 40 sibling pairs on the board, foldable() offers 32, not 8")

print("== B: who may claim, and what ==")
code, obj = fclaim(SK_C, PK_C, "1-2")
check(code == 403 and obj.get("unproven"), "a key with no proven work is refused a fold claim")
check("1-2" in offered(), "...and the pair stays on offer")
now = int(time.time())
check(fclaim(SK_A, PK_A, "1-2", msg=b"not the claim")[0] == 403, "a claim whose signature does not match is refused")
check(fclaim(SK_A, PK_A, "1-2", ts=now - 10 * server.BEAT_SKEW)[0] == 400, "a stale timestamp is refused")
check(server.fold_claim({"pubkey": PK_A, "result": "1-2", "nonce": "", "ts": now, "sig": "00" * 64})[0] == 400,
      "a claim with no nonce is refused")
check(fclaim(SK_A, PK_A, "1-3")[0] == 400, "a range that is not a sibling fold (width 3) is refused")
check(fclaim(SK_A, PK_A, "2-3")[0] == 400, "an unaligned pair is refused")
check(fclaim(SK_A, PK_A, "5")[0] == 400, "a single block is not a fold")
check(server.live_fold_claims() == {}, "none of the refused claims holds anything")

print("== B: claims ==")
T = int(time.time())
code, obj = fclaim(SK_A, PK_A, "1-2", handle="alice", nonce="n1", ts=T)
check(code == 200 and obj.get("ttl") == server.FOLD_CLAIM_TTL, "a proven key claims an offered pair for FOLD_CLAIM_TTL")
check("1-2" not in offered(), "a claimed pair is no longer offered to anyone")
code, obj = fclaim(SK_B, PK_B, "1-2")
check(code == 409, "another key cannot claim a pair that is held")
check(fclaim(SK_A, PK_A, "1-2", handle="alice", nonce="n1", ts=T)[0] == 200,
      "the holder retrying with the same nonce gets the claim back (#268)")
check(fclaim(SK_A, PK_A, "3-4", handle="alice")[0] == 200, "a key may hold a second claim")
code, obj = fclaim(SK_A, PK_A, "5-6", handle="alice")
check(code == 429, f"a third live claim from the same key is refused (cap {server.FOLD_CLAIM_CAP})")
check("5-6" in offered(), "...and the pair it wanted stays on offer")

try:
    st = server.state(slim=True)
    fc = {x["result"]: x for x in st.get("fold_claims", [])}
    shown = ("1-2" in fc and fc["1-2"]["handle"] == "alice"
             and 0 < fc["1-2"]["expires_in"] <= server.FOLD_CLAIM_TTL and fc["1-2"]["lo"] == 1)
    check(shown, "the board (/api/state) shows live fold claims with holder, span and time left")
except Exception as e:
    check(False, f"the board (/api/state) shows live fold claims: state() raised {e!r}")

code, bd = server.block_detail(3)
fcb = (bd or {}).get("fold_claim") or {}
check(code == 200 and fcb.get("result") == "3-4" and fcb.get("handle") == "alice" and fcb.get("expires_in", 0) > 0,
      "a block inside a claimed pair reports it as fold_claim on /api/block/<n>, for its page")
check((server.block_detail(9)[1] or {}).get("fold_claim") is None, "a block in no claimed pair has no fold_claim")

server._fold_claims["1-2"]["at"] -= server.FOLD_CLAIM_TTL + 1
check("1-2" in offered(), "an expired claim puts its pair back on offer")
check(fclaim(SK_B, PK_B, "1-2", handle="bob")[0] == 200, "...and another key can then claim it")

print("== submit stays open ==")
code, obj = submit_as(SK_C, PK_C, 1, 2, "carol")
check(code == 200 and obj.get("ok"), "a valid fold from a key holding no claim (and no proven work) is still accepted")
check("1-2" not in server._fold_claims, "a verified fold releases its claim at once")
check(fclaim(SK_A, PK_A, "1-2")[0] == 409, "claiming a pair that is already folded is refused")

print("== worker: claim_fold_pair ==")
_ns = {"__name__": "hazynccli", "__file__": os.path.join(os.path.dirname(os.path.abspath(__file__)), "hazync")}
try:
    exec(compile(open(_ns["__file__"]).read(), "hazync", "exec"), _ns)
except SystemExit:
    pass
pick = _ns["claim_fold_pair"]


class _Key:
    """The worker's signing key, as far as claim_fold_pair uses it."""
    def __init__(self, sk=None):
        self.sk = sk

    def sign(self, m):
        return self.sk.sign(m) if self.sk else b"\0" * 64


PAIRS = [{"left": "1", "right": "2", "result": "1-2"}, {"left": "3", "right": "4", "result": "3-4"}]


def fresh():
    _ns["_FOLD_CLAIMS_UNSUPPORTED"] = False


sent = []


def taken_12(path, body):
    sent.append(body)
    if body["result"] == "1-2":
        return {"error": "another worker is folding this pair"}
    return {"ok": True}


for _ in range(10):
    fresh()
    got = pick(PAIRS, _Key(), "pk", "h", post_fn=taken_12)
    if got is None or got["result"] != "3-4":
        break
check(got is not None and got["result"] == "3-4", "a pair another worker holds is skipped for one that is free")

fresh()
check(pick(PAIRS, _Key(), "pk", "h", post_fn=lambda p, b: {"error": "already proven"}) is None,
      "when every candidate is taken or folded, it returns None so the caller asks for a fresh list")

fresh()
calls = []
got = pick(PAIRS, _Key(), "pk", "h", post_fn=lambda p, b: calls.append(b) or {"error": "not found"})
got2 = pick(PAIRS, _Key(), "pk", "h", post_fn=lambda p, b: calls.append(b) or {"error": "not found"})
check(got is not None and got2 is not None and len(calls) == 1,
      "an older coordinator (404) means folding unclaimed, and the worker stops asking")

fresh()
check(pick(PAIRS, _Key(), "pk", "h", post_fn=lambda p, b: {"error": "fold claims are for keys with proven work",
                                                          "unproven": True}) is not None,
      "a key with no proven work folds unclaimed instead of stopping")

fresh()
check(pick(PAIRS, _Key(), "pk", "h", post_fn=lambda p, b: {"error": "this key already holds 2 fold claims"}) is not None,
      "a key at its cap folds unclaimed instead of stopping")


def _unreachable(path, body):
    raise SystemExit("coordinator error 502: Bad Gateway")


fresh()
check(pick(PAIRS, _Key(), "pk", "h", post_fn=_unreachable) is not None,
      "a proxy error on the claim folds unclaimed instead of killing the fold loop")

fresh()
sent.clear()
got = pick([PAIRS[1]], _Key(SK_B), PK_B, "bob", post_fn=taken_12)
b = sent[0] if sent else {}
check(bool(b) and server.verify_sig(PK_B, b.get("sig", ""), f"foldclaim:{b.get('result')}:{b.get('nonce')}:{b.get('ts')}".encode()),
      "the worker signs exactly the message the coordinator checks")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — fold claims ignored and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — fold claims were ignored and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)
if fails:
    print(f"{len(fails)} check(s) FAILED")
    sys.exit(1)
print("fold claims: all checks passed.")

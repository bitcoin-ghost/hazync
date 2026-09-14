#!/usr/bin/env python3
"""
Tests for signed claims (#310): only a claim signed by its key over "claim:<nonce>:<ts>" speaks for that key.

WHY THIS EXISTS. `claim()` took `pubkey` from the body and checked nothing, so anyone could claim under any key.
Once the per-key cap (#319) and the re-take wait (#321) existed, that also meant anyone could fill a real
prover's four slots, and keep blocks from it for an hour, by sending claims with its public key.

What is checked:
- a good signature is accepted and recorded as signed. A signature by another key, over another nonce, with a
  stale timestamp, malformed, or without a nonce is refused -- never treated as an unsigned claim instead;
- unsigned claims under a key cannot use up that key's signed cap, nor keep a block from its signed claims;
- the re-take wait still applies to a key's own signed claims;
- unsigned claims still work (workers up to v0.21.4 sign none), and CLAIM_REQUIRE_SIG=1 refuses them.

Usage:
  python3 test_claim_signed.py            # assertions; exit 0 on success
  python3 test_claim_signed.py --control  # the old server: every claim is unsigned; tests MUST fail
"""
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
# Pinned literals, and every scenario is written against them (see test_claim_grace.py for why).
CAP, GRACE, TTL, WAIT, SKEW = 4, 600, 3600, 3600, 120
os.environ["CLAIM_OPEN_MAX"] = str(CAP)
os.environ["CLAIM_GRACE"] = str(GRACE)
os.environ["CLAIM_TTL"] = str(TTL)
os.environ["CLAIM_RETAKE_WAIT"] = str(WAIT)
os.environ["BEAT_SKEW"] = str(SKEW)
os.environ.pop("CLAIM_REQUIRE_SIG", None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402

server.init_db()
server.provable_tip = lambda: 1_000
server.witness_available = lambda h: True
if CONTROL:
    server._CONTROL_CLAIMS_UNSIGNED = True
    print("CONTROL: every claim is unsigned, as before #310 -- the checks below MUST fail")

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def key():
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


VICTIM_SK, VICTIM = key()
OTHER_SK, OTHER = key()


def signed_claim(sk, pk, nonce=None, ts=None, signed_nonce=None, sig=None):
    nonce = nonce or os.urandom(8).hex()
    ts = int(time.time()) if ts is None else ts
    if sig is None:
        sig = sk.sign(f"claim:{signed_nonce or nonce}:{ts}".encode()).hex()
    return server.claim({"pubkey": pk, "handle": "victim", "nonce": nonce, "ts": ts, "sig": sig})


def unsigned_claim(pk):
    return server.claim({"pubkey": pk, "handle": "anyone"})


def q(sql, args=()):
    c = server.db()
    rows = c.execute(sql, args).fetchall()
    c.commit()
    c.close()
    return rows


def claimed(pk):
    return [(r["id"], r["claim_signed"]) for r in q(
        "SELECT id, claim_signed FROM ranges WHERE assignee=? AND status='claimed' ORDER BY lo", (pk,))]


def reset():
    q("DELETE FROM ranges WHERE status='claimed'")


print("== a signed claim ==")
reset()
code, r = signed_claim(VICTIM_SK, VICTIM)
check(code == 200 and claimed(VICTIM) == [(r.get("range"), 1)], f"a claim signed by its key is accepted and recorded as signed ({code} {claimed(VICTIM)})")

print("== a signature that does not verify is refused, not treated as unsigned ==")
now = int(time.time())
for what, call in (("signed by another key", lambda: signed_claim(OTHER_SK, VICTIM)),
                   ("signed over another nonce", lambda: signed_claim(VICTIM_SK, VICTIM, signed_nonce="not-this-one")),
                   (f"a timestamp {SKEW + 480} s old", lambda: signed_claim(VICTIM_SK, VICTIM, ts=now - SKEW - 480)),
                   ("a malformed signature", lambda: signed_claim(VICTIM_SK, VICTIM, sig="zz")),
                   ("no nonce", lambda: server.claim({"pubkey": VICTIM, "handle": "v", "ts": now,
                                                      "sig": VICTIM_SK.sign(f"claim::{now}".encode()).hex()}))):
    reset()
    code, r = call()
    check(code in (400, 403) and not claimed(VICTIM), f"{what}: refused with {code}, and no block is claimed")

print("== unsigned claims under a key cannot use up its signed slots ==")
reset()
codes = [unsigned_claim(VICTIM)[0] for _ in range(CAP)]
check(codes == [200] * CAP, f"anyone can still send {CAP} unsigned claims under the victim's key ({codes})")
code, r = signed_claim(VICTIM_SK, VICTIM)
check(code == 200, f"the key's own signed claim is still accepted ({code} {r.get('error', '')})")
check(unsigned_claim(VICTIM)[0] == 429, "while a further unsigned claim is capped, among unsigned claims")

print("== nor keep a block from its signed claims ==")
reset()
code, r = unsigned_claim(VICTIM)
b = r.get("range")
q("UPDATE ranges SET claimed_at=? WHERE id=?", (time.time() - GRACE - 60, b))
code, r = signed_claim(VICTIM_SK, VICTIM)
check(code == 200 and r.get("range") == b,
      f"a block an unsigned claim under the key let lapse is offered to the key's signed claim ({b} -> {r.get('range')})")

print("== the re-take wait still holds a key's own signed claims ==")
reset()
code, r = signed_claim(VICTIM_SK, VICTIM)
b = r.get("range")
q("UPDATE ranges SET claimed_at=? WHERE id=?", (time.time() - GRACE - 60, b))
code, r = signed_claim(VICTIM_SK, VICTIM)
check(code == 200 and r.get("range") != b, f"a signed claim does not get back the block its own signed claim let lapse ({b} -> {r.get('range')})")

print("== CLAIM_REQUIRE_SIG ==")
reset()
code, r = unsigned_claim(OTHER)
check(code == 200, f"by default an unsigned claim is still accepted: workers up to v0.21.4 sign none ({code})")
server.CLAIM_REQUIRE_SIG = True
try:
    code_u, r_u = unsigned_claim(OTHER)
    code_s, r_s = signed_claim(OTHER_SK, OTHER)
finally:
    server.CLAIM_REQUIRE_SIG = False
check(code_u == 403 and "signed" in (r_u.get("error") or ""), f"with CLAIM_REQUIRE_SIG=1 an unsigned claim is refused ({code_u} {r_u})")
check(code_s == 200, f"and a signed one is accepted ({code_s})")

if CONTROL:
    if fails:
        print(f"CONTROL OK — every claim unsigned, and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — claims were unsigned and every test still passed.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("only a signed claim speaks for its key; unsigned claims under it cannot touch its cap or its blocks.")

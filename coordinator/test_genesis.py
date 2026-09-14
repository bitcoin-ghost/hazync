#!/usr/bin/env python3
"""
Block 0 is refused everywhere someone might try to prove, submit or sponsor it -- with an explanation.

WHY THIS FILE EXISTS. Genesis is the most famous block in Bitcoin, so people will try it first. It
cannot be proved: it is the in-boundary every Hazync range proof is pinned to, the way Bitcoin Core
writes it into chainparams instead of validating it. Before this, the coordinator accepted a `[0..hi]`
"genesis seed" that skipped the fold rule (a G1 hole), `/api/proof/0` was a bare 404, and
`hazync run 0` set off to prove nothing.

What is asserted, per entry point:
  * submit     -- "0" and "0-N" are 400 with the genesis message, and refused BEFORE any work: no
                  ranges row, no submissions row, no call to verify_receipt.
  * sponsor    -- a span starting at 0 is 400 with the genesis message, naming 1..hi.
  * block API  -- /api/block/0 is status "genesis" with the message; block 1 carries no note.
  * proof API  -- GET /api/proof/0 explains genesis rather than "proof not available".
  * worker CLI -- `hazync run 0` / `0-999` exit with the message and the command to run instead.

NOT covered: real STARK verification (VERIFY_MODE=mock). The refusal runs before verification, so
mock is sufficient to show that verification is never reached.

Usage:
  python3 test_genesis.py            # assertions; exit 0 on success
  python3 test_genesis.py --control  # remove both refusals; these tests MUST then fail
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["COORD_STATE"] = tempfile.mkdtemp(prefix="state_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", HERE)

sys.path.insert(0, HERE)
import server  # noqa: E402

server.init_db()

loader = importlib.machinery.SourceFileLoader("hazync_cli", os.path.join(HERE, "hazync"))
spec = importlib.util.spec_from_loader("hazync_cli", loader)
hz = importlib.util.module_from_spec(spec)
loader.exec_module(hz)

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


if CONTROL:
    # Remove both refusals the way they were absent before. If the assertions still pass, they test nothing.
    server.genesis_refusal = lambda lo, hi, what="Submit": None
    hz._refuse_genesis = lambda lo, hi: (lo, hi)

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

_verify_calls = []
_real_verify = server.verify_receipt


def _counting_verify(receipt, rng):
    _verify_calls.append(rng["id"])
    return _real_verify(receipt, rng)


server.verify_receipt = _counting_verify


def submit(rid):
    receipt = f"receipt-for-{rid}".encode()
    return server.submit({"range": rid, "pubkey": PUB, "handle": "tester",
                          "sig": _sign(receipt), "receipt": base64.b64encode(receipt).decode()})


def rows(sql, *a):
    c = server.db()
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


print("== submit: block 0 is refused before any work ==")
code, obj = submit("0")
check(code == 400, "submitting block 0 is REFUSED (400)")
check("genesis block" in str(obj.get("error", "")) and obj.get("genesis") is True,
      "  ...with the genesis explanation, flagged genesis: true for clients")
check(not rows("SELECT id FROM ranges WHERE id='0'"), "  ...and no ranges row was created for it")
check(not rows("SELECT id FROM submissions WHERE range_id='0'"), "  ...no submission was recorded")
check("0" not in _verify_calls, "  ...and verification was never run")

code, obj = submit("0-3")
check(code == 400 and obj.get("genesis") is True, "a range starting at 0, [0..3], is refused as genesis")
check("blocks 1 to 3 instead" in str(obj.get("error", "")), "  ...and the error says to submit blocks 1 to 3")
check("0-3" not in _verify_calls and not rows("SELECT id FROM ranges WHERE id='0-3'"),
      "  ...again before any row or verification")

check(submit("1")[0] == 200, "block 1 is accepted as normal — the refusal is exactly block 0")

print("== sponsorship: a span starting at 0 explains genesis ==")
code, obj = server._sponsor_span(0, 0)
check(code == 400 and obj.get("genesis") is True, "sponsoring block 0 is refused as genesis")
code, obj = server._sponsor_span(0, 5)
check(code == 400 and "Sponsor blocks 1 to 5 instead" in str(obj.get("error", "")),
      "sponsoring 0..5 is refused and names 1..5 instead")
code, obj = server._sponsor_span(1, 5)
check(not (isinstance(obj, dict) and obj.get("genesis")), "sponsoring 1..5 is not a genesis refusal")

print("== block API: /api/block/0 explains ==")
code, obj = server.block_detail(0)
check(code == 200 and obj.get("status") == "genesis", "block 0's status is 'genesis'")
check("genesis block" in str(obj.get("note", "")), "  ...and it carries the explanation")
code, obj = server.block_detail(1)
check("note" not in obj, "block 1 carries no genesis note")

print("== proof API: GET /api/proof/0 explains ==")
from http.server import ThreadingHTTPServer  # noqa: E402
_srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=_srv.serve_forever, daemon=True).start()
try:
    urllib.request.urlopen(f"http://127.0.0.1:{_srv.server_address[1]}/api/proof/0", timeout=10)
    check(False, "GET /api/proof/0 returned a body — there is no proof of block 0")
except urllib.error.HTTPError as e:
    body = json.loads(e.read() or b"{}")
    check(e.code == 404 and body.get("genesis") is True and "genesis block" in body.get("error", ""),
          "GET /api/proof/0 is 404 WITH the genesis explanation, not 'proof not available'")
finally:
    _srv.shutdown()

print("== worker CLI: `hazync run 0` stops before any proving ==")
for arg, alt in (("0", "hazync run 1"), ("0-1", "hazync run 1"), ("0-999", "hazync run 1-999")):
    try:
        out = hz.parse_range(arg)
        check(False, f"parse_range('{arg}') returned {out} — the worker would set off to prove genesis")
    except SystemExit as e:
        check("genesis block" in str(e) and f"`{alt}`" in str(e),
              f"parse_range('{arg}') exits explaining genesis and suggests `{alt}`")
check(hz.parse_range("1") == (1, 1) and hz.parse_range("5-9") == (5, 9), "blocks from 1 up parse as before")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — removed the genesis refusals and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the refusals were removed and every test still passed.")
    sys.exit(1)
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("block 0 is refused at every entry point, with the reason, before any work is done.")

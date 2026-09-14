#!/usr/bin/env python3
"""
One spelling per range, and a block that already has its own proof is not proved again.

WHY THIS FILE EXISTS. The ranges row, the proof file and the "already proven" check are all keyed by
the range id STRING. Measured 2026-09-14 on a scratch board: alice proved block 5, then bob submitted
block 5 as `5-5`, `05`, `+5` and ` 5` -- every one accepted, verified (up to 120 s of coordinator CPU
each) and stored as another proof file, because each spelling looked like a block nobody had proven.
Only bob's plain `5` was refused. Nothing was overwritten -- alice kept her row, receipt and credit --
but the board could be made to verify and store the same block without limit.

What is asserted:
  * submit    -- a non-canonical id is 400 naming the canonical one, before any row or verification.
  * submit    -- a block with its own proof is 409 saying who proved it and where to check it.
  * repair    -- a block covered only by a WIDE range may still get its own proof (the 30000-30249 case).
  * worker    -- `hazync run N` on a block that already has its own proof stops before proving.

NOT covered: real STARK verification (VERIFY_MODE=mock). Both refusals run before verification.

Usage:
  python3 test_canonical_ids.py            # assertions; exit 0 on success
  python3 test_canonical_ids.py --control  # remove the guards; these tests MUST then fail
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import urllib.error

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
    server.range_id_refusal = lambda rid, lo, hi: None
    hz._own_proof = lambda n: None

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402


def make_key():
    k = Ed25519PrivateKey.generate()
    return k, k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


ALICE, BOB = make_key(), make_key()

_verified = []
_real_verify = server.verify_receipt
server.verify_receipt = lambda receipt, rng: (_verified.append(rng["id"]), _real_verify(receipt, rng))[1]


def submit(who, handle, rid):
    sk, pk = who
    receipt = f"receipt-{rid}-{handle}".encode()
    return server.submit({"range": rid, "pubkey": pk, "handle": handle, "sig": sk.sign(receipt).hex(),
                          "receipt": base64.b64encode(receipt).decode()})


def q(sql, *a):
    c = server.db()
    try:
        return c.execute(sql, a).fetchall()
    finally:
        c.close()


print("== a block with its own proof is not proved again ==")
check(submit(ALICE, "alice", "5")[0] == 200, "alice proves block 5")
code, obj = submit(BOB, "bob", "5")
check(code == 409 and obj.get("already_proven") is True, "bob re-submitting block 5 is refused (409)")
err = str(obj.get("error", ""))
check("already proven" in err and "alice" in err and "/api/proof/5" in err,
      "  ...and the refusal says it is already proven, by whom, and where to check that proof")

print("== one spelling per range ==")
before_rows = len(q("SELECT id FROM vranges WHERE lo<=5 AND hi>=5"))
before_files = sorted(os.listdir(os.environ["COORD_PROOFS"]))
for rid in ("5-5", "05", "+5", " 5", "5 "):
    code, obj = submit(BOB, "bob", rid)
    check(code == 400 and obj.get("canonical") == "5",
          f"'{rid}' is refused (400) and told the canonical id is '5' — got {code}")
check(len(q("SELECT id FROM vranges WHERE lo<=5 AND hi>=5")) == before_rows,
      "  ...none of them added a proof of block 5")
check(sorted(os.listdir(os.environ["COORD_PROOFS"])) == before_files, "  ...or a proof file")
check(not any(r in _verified for r in ("5-5", "05", "+5", " 5", "5 ")), "  ...or cost a verification")
check(BOB[1] not in server.contributions_by_pubkey(), "  ...and bob is credited with nothing")

for rid, canon in (("010-20", "10-20"), ("10-020", "10-20"), ("7-07", "7")):
    code, obj = submit(BOB, "bob", rid)
    check(code == 400 and obj.get("canonical") == canon, f"'{rid}' is refused and named '{canon}'")

print("== repair: a block covered only by a wide range may still get its own proof ==")
_c = server.db()           # a wide range that predates the fold rule, the way 30000-30050 landed
_c.execute("INSERT INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
           " VALUES('10-12',10,12,'in','out',?,'alice',0,0,'0')", (ALICE[1],))
_c.commit(); _c.close()
check(submit(BOB, "bob", "11")[0] == 200, "block 11, inside alice's wide [10..12] but with no proof of its own, is accepted")
check(submit(ALICE, "alice", "11")[0] == 409, "  ...and once it has its own proof, the next one is refused")

print("== worker: `hazync run N` stops before proving a block that has its own proof ==")
_real_get = hz.get


def fake_block(proofs):
    def get(path):
        if path.startswith("/api/block/"):
            return json.dumps({"proofs": proofs}).encode()
        raise AssertionError(f"unexpected GET {path}")
    return get


hz.get = fake_block([{"lo": 5, "hi": 5, "handle": "alice", "proof": "/api/proof/5"}])
check(hz._own_proof(5) == ("alice", "/api/proof/5"), "a single-block proof on the board is found")
hz.get = fake_block([{"lo": 4, "hi": 6, "handle": "alice", "proof": "/api/proof/4-6"}])
check(hz._own_proof(5) is None, "a WIDE range covering the block is not its own proof (repair stays possible)")


def get_404(path):
    raise urllib.error.HTTPError(path, 404, "not found", {}, None)


hz.get = get_404
check(hz._own_proof(5) is None, "an older coordinator without /api/block means 'not known', not a crash")

_host = tempfile.NamedTemporaryFile(delete=False)
hz.HOSTBIN = _host.name
hz.witness_in_window = lambda blk: True
_proved = []
hz.cmd_prove = lambda *a, **k: _proved.append(a)
hz.cmd_submit = lambda *a, **k: None
hz.get = fake_block([{"lo": 5, "hi": 5, "handle": "alice", "proof": "/api/proof/5"}])
try:
    hz.cmd_run(["5"])
    check(False, "`hazync run 5` went on to prove a block alice already proved")
except SystemExit as e:
    check("already proven" in str(e) and "alice" in str(e), "`hazync run 5` exits saying alice already proved it")
check(not _proved, "  ...before any proving started")
hz.get = _real_get

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — removed the guards and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the guards were removed and every test still passed.")
    sys.exit(1)
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("one spelling per range, and a block with its own proof is not proved again.")

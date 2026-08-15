#!/usr/bin/env python3
"""
Tests for the spine and fold paths added in v0.13.0 (#30, #37) — the parts that do not need a GPU.

WHAT THIS DOES NOT COVER, stated first so nothing reads as more assurance than it is:

  * Real STARK verification of a spine. `verify_spine` shells out to the host binary, which is 184 MB
    and not in CI, so these tests run with VERIFY_MODE=mock. They exercise the STORE / MONOTONIC /
    SERVE logic around verification, never verification itself.
  * Actual folding. `fold-range` and `extend-spine` are prove operations. CI has no GPU and, on CPU,
    one fold is ~2-3 minutes. Those are verified by hand against real board receipts and recorded in
    prover/evidence/extend_spine_*.txt.

What IS covered here is the logic that has no excuse for being untested: which pairs get offered for
folding, what a range id is allowed to be, and whether a shorter spine can overwrite a longer one.

The third of those is why this file exists. `parse_range` rejected arbitrary-width ranges — right for
claims, since two claim ids must never partially overlap — but folding produces arbitrary widths by
construction, so a folded [1..2] was refused as "invalid range id" AFTER the fold had been done. That
was found by running it, not by reading it, and nothing in CI would have caught it.

Usage:
  python3 test_spine_fold.py            # assertions; silent success, exit 0
  python3 test_spine_fold.py --control  # break the logic on purpose; the tests MUST fail
"""
import base64
import json
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpspine = tempfile.mkdtemp(prefix="spine_")
_tmpproofs = tempfile.mkdtemp(prefix="proofs_")
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = _tmpspine
os.environ["COORD_PROOFS"] = _tmpproofs
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
# The servable-height scan is cached for 5 minutes in production, which is right there and wrong here:
# this file writes witness files DURING the run, so a cached "nothing available" from before those
# writes would make claim() fail depending on nothing but call ordering. Disable the cache for tests.
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()

fails = []


def check(cond, what):
    if cond:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)


def seed(ranges):
    """Put verified ranges on the board. `ranges` is a list of (lo, hi)."""
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")
    for lo, hi in ranges:
        rid = str(lo) if lo == hi else f"{lo}-{hi}"
        c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (rid, lo, hi))
        c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
                  " VALUES(?,?,?,?,?,'','t',0,0,'0')", (rid, lo, hi, f"in{lo}", f"out{hi}"))
    c.commit()
    c.close()


def pairs():
    return {(p["left"], p["right"]) for p in server.foldable(32)}


# ── foldable(): which pairs are offered ───────────────────────────────────────────────────────────
print("== foldable() ==")

seed([])
check(server.foldable() == [], "an empty board offers nothing to fold")

# Only ALIGNED siblings fold. With 1,2,3 the sole tree pair is (1,2): [2..3] is not aligned to its
# own width, and block 3's sibling (block 4) does not exist yet. The earlier version of this test
# asserted {(1,2),(2,3)} because that is what the buggy implementation did — it was written from the
# code rather than from the property, so it passed against behaviour that did not converge.
seed([(1, 1), (2, 2), (3, 3)])
check(pairs() == {("1", "2")}, "only the aligned sibling pair is offered, not every adjacent pair")

seed([(1, 1), (2, 2), (3, 3), (4, 4)])
check(pairs() == {("1", "2"), ("3", "4")}, "four blocks offer both sibling pairs at the leaf level")

seed([(1, 1), (3, 3)])
check(server.foldable() == [], "non-adjacent ranges are not a foldable pair")

# The one that matters: a pair whose fold ALREADY EXISTS must not be offered again, or workers burn
# GPU time re-folding what the board already has.
seed([(1, 1), (2, 2), (3, 3), (4, 4), (1, 2), (3, 4)])
got = pairs()
if CONTROL:
    # Break exactly that rule — the pre-check that skips already-folded pairs — and prove this test
    # notices. A test that cannot fail here would let duplicate work go unnoticed forever.
    _orig = server.foldable

    def _broken(limit=8):
        with server._lock:
            c = server.db()
            rows = c.execute("SELECT id, lo, hi FROM vranges ORDER BY lo").fetchall()
            c.close()
        starts = {}
        for r in rows:
            starts.setdefault(r["lo"], []).append(r)
        out = []
        for r in rows:
            for s in starts.get(r["hi"] + 1, ()):
                out.append({"left": r["id"], "right": s["id"], "lo": r["lo"], "hi": s["hi"],
                            "result": f"{r['lo']}-{s['hi']}"})
                if len(out) >= limit:
                    return out
        return out
    server.foldable = _broken
    got = pairs()
check(("1", "2") not in got, "a pair whose fold already exists is not offered again")
check(("1-2", "3") not in got,
      "a folded range does NOT pair with a bare neighbour — different widths are not siblings")
check(("1-2", "3-4") in got, "two folded siblings DO fold into their parent")

seed([(i, i) for i in range(1, 12)])
check(len(server.foldable(3)) == 3, "the limit is respected")

# CONVERGENCE. This is the property that was missing and it cost 486 redundant folds on the live
# board before anyone noticed — 581 folds to cover 96 blocks, where a tree needs 95. Offering "any
# adjacent pair whose span does not exist" wanders into every (start, width) combination, because
# each fold creates a new operand. Assert the tree instead: N blocks must fold in exactly N-1 steps
# and terminate.
seed([(i, i) for i in range(1, 17)])
folds = 0
while True:
    ps = server.foldable(32)
    if not ps:
        break
    p = ps[0]
    c = server.db()
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
              " VALUES(?,?,?,'','','','t',0,0,'0')", (p["result"], p["lo"], p["hi"]))
    c.commit(); c.close()
    folds += 1
    if folds > 100:
        break                                   # runaway: the old behaviour never terminated
check(folds == 15, f"16 blocks fold in exactly 15 steps and stop (got {folds})")
c = server.db()
built = {(r["lo"], r["hi"]) for r in c.execute("SELECT lo,hi FROM vranges WHERE hi>lo")}
c.close()
check((1, 16) in built and (1, 8) in built and (9, 16) in built,
      "it builds the aligned tree, up to the full [1..16] root")
check(all(server._tree_node(lo, hi) for lo, hi in built),
      "every range it produced is an aligned power-of-two tree node — no stray widths")


# ── range ids: the collision that broke a real fold ───────────────────────────────────────────────
print("== range ids ==")

check(server.parse_any_range("5") == (5, 5), "a single block is a range")
check(server.parse_any_range("1-2") == (1, 2), "an ARBITRARY-width range is valid for submission")
check(server.parse_any_range("100-299") == (100, 299), "so is a wide one")
check(server.parse_any_range("5-1") is None, "hi < lo is refused")
check(server.parse_any_range("abc") is None, "a non-numeric id is refused")
check(server.parse_any_range("../etc/passwd") is None, "a path is refused (this guards /api/proof/<id>)")
# The distinction that caused the bug: claims stay on the aligned grid, submissions do not.
check(server.parse_range("1-2") is None, "claims still REJECT an unaligned range (no overlapping claim ids)")


# ── spine: store, monotonic, serve ────────────────────────────────────────────────────────────────
print("== spine ==")

check(server.spine_head() is None, "no spine to begin with")

# Sign for real. The coordinator verifies ed25519 over the receipt bytes before it looks at anything
# else, so a test using dummy hex would only ever prove that signature checking works — every spine
# assertion below would pass vacuously on a 403.
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization as _ser
_sk = Ed25519PrivateKey.generate()
_pk = _sk.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()

def submit(hi):
    server._MOCK_HI = hi
    receipt = b"receipt-%d" % hi
    return server.submit_spine({"pubkey": _pk, "sig": _sk.sign(receipt).hex(), "handle": "t",
                                "receipt": base64.b64encode(receipt).decode()})

# The mock verifier reports hi=0; drive it from _MOCK_HI so monotonicity is actually exercised.
_real_verify = server.verify_spine
def _mock_verify(receipt):
    ok, note, meta = _real_verify(receipt)
    if ok:
        meta = dict(meta, hi=getattr(server, "_MOCK_HI", 0))
    return ok, note, meta
server.verify_spine = _mock_verify

code, obj = submit(3)
check(code == 200, "a spine is accepted")
check((server.spine_head() or {}).get("hi") == 3, "and becomes the served head")

code, obj = submit(2)
check(code == 409, "a SHORTER spine is refused (409)")
check((server.spine_head() or {}).get("hi") == 3, "and the head is unchanged")

code, obj = submit(4)
check(code == 200 and (server.spine_head() or {}).get("hi") == 4, "a longer spine advances the head")

check(os.path.exists(os.path.join(_tmpspine, "spine.bin")), "the receipt is written to disk")
check(os.path.exists(os.path.join(_tmpspine, "spine.json")), "and the json that advertises it")

code, _ = server.submit_spine({"pubkey": _pk, "sig": "cd" * 64, "handle": "t"})
check(code == 400, "a submission with no receipt is refused")

code, _ = server.submit_spine({"pubkey": _pk, "sig": "cd" * 64, "handle": "t",
                               "receipt": base64.b64encode(b"receipt-9").decode()})
check(code == 403, "a receipt with a bad signature is refused")

# ── claims survive a long prove (#52) ─────────────────────────────────────────────────────────────
# CLAIM_TTL used to be measured from when a claim was TAKEN, with no heartbeat. Block 741,000 is a
# measured 3,275s = 55 min against a 3600s expiry, so any real block was one step from having its
# claim handed to someone else mid-prove and the same GPU-hours spent twice — silently, since the
# loser's submission is discarded as "already proven".
print("== claim liveness ==")
import time as _t
_ttl = server.CLAIM_TTL
server.CLAIM_TTL = 2
try:
    os.makedirs(server.WITNESS, exist_ok=True)
    for _b in range(1, 6):
        open(os.path.join(server.WITNESS, f"block_{_b}.json"), "w").write("{}")
    c = server.db(); c.execute("DELETE FROM ranges"); c.execute("DELETE FROM vranges"); c.commit(); c.close()
    # A beat is SIGNED and timestamped (audit #5, L-2), so these tests drive the real protocol.
    # They used to beat with literal "aa"*32 / "bb"*32 pubkeys and no signature, which is precisely
    # the hole L-2 closed: pubkeys are public on the board, so anyone could renew anyone else's
    # claim. Real keypairs are needed now because an unsigned beat is refused outright.
    _ska = Ed25519PrivateKey.generate()
    A = _ska.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()
    _skb = Ed25519PrivateKey.generate()
    Bk = _skb.public_key().public_bytes(_ser.Encoding.Raw, _ser.PublicFormat.Raw).hex()

    def _beat(rng, sk, pk):
        # ts is INTEGER unix seconds — the coordinator rebuilds this exact string to verify, so a
        # float here would fail with a misleading "signature does not verify".
        ts = int(_t.time())
        return server.beat({"range": rng, "pubkey": pk, "ts": ts,
                            "sig": sk.sign(f"{rng}:{ts}".encode()).hex()})

    _, r = server.claim({"pubkey": A, "handle": "A"})
    rid = r.get("range")

    # AGE the claim rather than sleeping for it. The property is "a fresh beat holds an OLD claim",
    # and the previous version established the "old" half by sleeping 3x1s against CLAIM_TTL=2 — a
    # margin of about one second. That was a race, and adding the L-2 signature made each beat do
    # ed25519 work and lose it: the last beat landed 3.6s before the claim, so the claim was already
    # expired and B took the block. The test failed for a reason that had nothing to do with the
    # behaviour it names. Setting claimed_at directly removes the wall clock from the assertion while
    # still driving a real signed beat through the real handler.
    _c = server.db()
    _c.execute("UPDATE ranges SET claimed_at=? WHERE id=?", (_t.time() - 600, rid))
    _c.commit(); _c.close()
    check(_beat(rid, _ska, A)[0] == 200, "a signed beat is accepted")
    _, rb = server.claim({"pubkey": Bk, "handle": "B"})
    check(rb.get("range") != rid, "a worker that keeps beating keeps its block past CLAIM_TTL")

    # L-2 itself, in the repo's own suite rather than only in a by-hand check: an unsigned beat must
    # not renew a claim, and B must not be able to renew A's claim by quoting A's public key.
    code, _ = server.beat({"range": rid, "pubkey": A})
    check(code == 401, "an UNSIGNED beat is refused (L-2)")
    _ts = int(_t.time())
    code, _ = server.beat({"range": rid, "pubkey": A, "ts": _ts,
                           "sig": _skb.sign(f"{rid}:{_ts}".encode()).hex()})
    check(code == 403, "a beat signed by someone else's key is refused (L-2)")
    code, _ = server.beat({"range": rid, "pubkey": A, "ts": _ts - 600,
                           "sig": _ska.sign(f"{rid}:{_ts - 600}".encode()).hex()})
    check(code == 400, "a stale beat cannot be replayed (L-2)")

    c = server.db(); c.execute("DELETE FROM ranges"); c.commit(); c.close()
    _, r2 = server.claim({"pubkey": A, "handle": "A"})
    _t.sleep(3)                                   # stops beating: the worker died
    _, rc = server.claim({"pubkey": Bk, "handle": "B"})
    check(rc.get("range") == r2.get("range"), "a worker that stops beating releases it")

    # Signed by B, for a range B does not hold: authentication passes, authorisation does not.
    code, _ = _beat(r2.get("range"), _skb, Bk)
    check(code in (200, 409), "beat by a non-holder does not crash")
finally:
    server.CLAIM_TTL = _ttl

# ── genesis-anchoring label (#59) ────────────────────────────────────────────────────────────────
# "verified" and "genesis-anchored" are different claims, and the API now reports which one a receipt
# is. The risk being tested is NOT that the predicate is complicated — it is two lines — but that the
# label and the frontier rule could disagree, with the dangerous direction being a receipt labelled
# `anchored: true` that the frontier would refuse to build on.
GT = server.GENESIS_TIP
check(server.is_genesis_anchored(GT, 1) is True, "genesis tip at lo=1 IS anchored")
check(server.is_genesis_anchored(GT, 2) is False, "genesis tip at lo=2 is NOT anchored (block 0 is unprovable)")
check(server.is_genesis_anchored("00" * 32, 1) is False, "lo=1 with a non-genesis in-tip is NOT anchored")
check(server.is_genesis_anchored("", 1) is False, "an empty in-tip is NOT anchored")
check(server.is_genesis_anchored(GT, "1") is True, "lo arrives as a string from sqlite and still counts")
check(server.is_genesis_anchored(GT, None) is False, "a missing lo is refused, not crashed")
check(server.is_genesis_anchored(GT, "abc") is False, "an unparseable lo is refused, not crashed")

# The label must agree with the rule that actually governs the frontier. Seed a genesis-anchored
# range and a mid-chain one, and assert the frontier advances over exactly the ranges the label
# calls anchored.
c = server.db()
c.execute("DELETE FROM vranges"); c.execute("DELETE FROM ranges")
for rid, lo, hi, itip, otip in [("1-4", 1, 4, GT, "tipA"), ("5-8", 5, 8, "tipA", "tipB")]:
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status) VALUES(?,?,?,'verified')", (rid, lo, hi))
    c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
              " VALUES(?,?,?,?,?,'','t',0,0,'0')", (rid, lo, hi, itip, otip))
c.commit()
rows = [dict(r) for r in c.execute("SELECT lo,in_tip FROM vranges ORDER BY lo").fetchall()]
c.close()
check(server.is_genesis_anchored(rows[0]["in_tip"], rows[0]["lo"]) is True,
      "the range the frontier starts from is the one labelled anchored")
check(server.is_genesis_anchored(rows[1]["in_tip"], rows[1]["lo"]) is False,
      "a mid-chain continuation is NOT labelled anchored, even though it is verified")

# ── spine_hi is reported in /api/state (#74) ─────────────────────────────────────────────────────
# A stalled spine is invisible from every other signal — proven climbs, frontier climbs, gates stay
# green — so this field is the only place it shows. Assert BOTH states: absent means "no spine at
# all", which must read differently from a stale one.
c = server.db()
c.execute("DELETE FROM vranges"); c.execute("DELETE FROM ranges"); c.commit(); c.close()
import shutil
shutil.rmtree(server.SPINE_DIR, ignore_errors=True)
st = server.state()
check("spine_hi" in st["progress"], "/api/state reports spine_hi at all")
check(st["progress"]["spine_hi"] is None, "no spine -> spine_hi is None, not 0 (absent != stalled at genesis)")

os.makedirs(server.SPINE_DIR, exist_ok=True)
with open(os.path.join(server.SPINE_DIR, "spine.json"), "w") as f:
    json.dump({"lo": 1, "hi": 137, "out_tip": "deadbeef"}, f)
check(server.state()["progress"]["spine_hi"] == 137, "a present spine reports its hi")

# ── peer-aware claim allocation (#69) ────────────────────────────────────────────────────────────
# The property that matters is not "it excludes peer heights" — it is that it FAILS OPEN. A peer being
# unreachable, slow or lying must never stall local proving, because the cost of a duplicate proof is
# wasted GPU time while the cost of blocking is an idle fleet.
c = server.db()
c.execute("DELETE FROM vranges"); c.execute("DELETE FROM ranges"); c.commit(); c.close()

_orig_peers, _orig_cache = server.PEERS, dict(server._peer_cache)
try:
    server.PEERS = []
    server._peer_cache.update(t=0, heights=set())
    check(server.peer_proven_heights() == set(), "no peers configured -> no exclusions, no network call")
    code, obj = server.pick(None)
    check(code == 200 and obj["lo"] == 1, "with no peers, pick() offers block 1 as before")

    # A peer claiming heights 1-3 should push us past them.
    server.PEERS = ["http://peer.invalid"]
    server._peer_cache.update(t=time.time(), heights={1, 2, 3})
    code, obj = server.pick(None)
    check(code == 200 and obj["lo"] == 4, f"peer-proven heights are skipped (got lo={obj.get('lo')})")

    # THE ONE THAT MATTERS: an unreachable peer must not stall us. Force a real fetch against a
    # non-resolving host and assert it returns empty rather than raising or hanging the allocator.
    server._peer_cache.update(t=0, heights=set())
    t0 = time.time()
    check(server.peer_proven_heights() == set(), "an unreachable peer contributes nothing (fails OPEN)")
    check(time.time() - t0 < 30, "an unreachable peer does not hang the allocator")
    code, obj = server.pick(None)
    check(code == 200 and obj["lo"] == 1, "with the peer down, local work continues from block 1")
finally:
    server.PEERS = _orig_peers
    server._peer_cache.update(**_orig_cache)

# ── peer proof sync (#69) ────────────────────────────────────────────────────────────────────────
# The property worth asserting is that adoption goes through the SAME verification a submission does.
# A coordinator that trusted a peer's index would let a hostile peer put anything on the board; one
# that re-verifies can only ever waste bandwidth on junk it rejects.
_orig_peers = server.PEERS
try:
    server.PEERS = []
    check(server.sync_from_peers() == {"adopted": 0, "rejected": 0, "peers": 0},
          "no peers -> sync is a no-op, no network call")

    server.PEERS = ["http://peer.invalid"]
    r = server.sync_from_peers()
    check(r["adopted"] == 0 and r["peers"] == 1,
          "an unreachable peer adopts nothing and does not raise")

    # Adoption must be gated on OUR verification, not the peer's claim. In mock mode verify_receipt
    # returns a fixed shape, so assert the call path rather than the crypto: a range the peer offers
    # is only ever inserted after verify_receipt returns ok.
    import inspect
    src = inspect.getsource(server.sync_from_peers)
    check("verify_receipt(" in src, "sync re-verifies every receipt itself")
    check("if not ok or not meta:" in src, "sync drops anything that fails ITS OWN verification")
    check(src.index("verify_receipt(") < src.index("INSERT OR REPLACE INTO vranges"),
          "verification happens BEFORE the row is written, not after")
finally:
    server.PEERS = _orig_peers

# ---------------------------------------------------------------------------------------------
# Bulk bundle sync (#69) — the endpoint that makes seeding a new coordinator from a peer possible.
#
# Driven over a REAL socket, not by calling the handler's helpers. The value of this endpoint is
# entirely in its streaming behaviour, and streaming is exactly what a unit test of `bulk_plan` cannot
# see: a version that buffers the whole archive in memory passes every pure test and OOMs the box on
# the first 73 GB request.
# ---------------------------------------------------------------------------------------------
import tempfile, tarfile, io as _io, threading as _th, urllib.request as _rq
from http.server import ThreadingHTTPServer

_bdir = tempfile.mkdtemp()
_orig_bridge, _orig_bulk = server.BRIDGE_DIR, server.BULK_MAX
server.BRIDGE_DIR = _bdir
server.BULK_MAX = 50
# heights 100..109, with 105 DELIBERATELY absent — a gap in the bridge's output must be reported, not
# silently skipped, or a syncing peer reads it as the end of the chain.
_present = [h for h in range(100, 110) if h != 105]
for h in _present:
    with open(os.path.join(_bdir, f"bundle_{h}.json"), "w") as f:
        json.dump({"height": h, "pad": "x" * 500}, f)

_srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
_port = _srv.server_address[1]
_th.Thread(target=_srv.serve_forever, daemon=True).start()
try:
    def _get(path):
        # urllib raises on 4xx, and the 4xx responses ARE the behaviour under test here.
        try:
            with _rq.urlopen(f"http://127.0.0.1:{_port}{path}", timeout=20) as r:
                return r.status, r.read()
        except _rq.HTTPError as e:
            return e.code, e.read()

    st, body = _get("/api/witnesses?from=100&count=10")
    check(st == 200, "bulk sync returns 200")

    # Parsing to the end-of-archive marker is the completeness proof — see the endpoint's comment.
    tf = tarfile.open(fileobj=_io.BytesIO(body), mode="r")
    names = tf.getnames()
    check("MANIFEST.json" in names, "the archive carries a manifest")
    man = json.loads(tf.extractfile("MANIFEST.json").read())
    check(man["missing"] == [105], f"the gap at 105 is REPORTED, not skipped (got {man['missing']})")
    check(man["served"] == _present, "the manifest lists exactly what was served")
    check(sorted(n for n in names if n != "MANIFEST.json")
          == sorted(f"bundle_{h}.json" for h in _present),
          "every present height is in the archive and nothing else is")
    # Contents, not just names: an archive of correctly-named empty files would pass everything above.
    got = json.loads(tf.extractfile("bundle_104.json").read())
    check(got["height"] == 104, "the bytes served are the bundle's own bytes")

    st, body = _get("/api/witnesses?from=100&count=999")
    check(st == 400, "a count over BULK_MAX is refused rather than served in part")

    st, body = _get("/api/witnesses?from=-1&count=5")
    check(st == 400, "a negative `from` is a 400, not an empty archive")

    # An EMPTY range must still be a well-formed archive: "nothing here yet" is a legitimate answer and
    # a client must be able to tell it apart from a failure.
    st, body = _get("/api/witnesses?from=9000&count=5")
    check(st == 200, "a range with no bundles is still 200")
    man = json.loads(tarfile.open(fileobj=_io.BytesIO(body), mode="r")
                     .extractfile("MANIFEST.json").read())
    check(man["served"] == [] and man["missing"] == list(range(9000, 9005)),
          "an empty range reports every height as missing")

    # STREAMING, asserted structurally and labelled as such. A version that builds the archive in
    # memory passes every test above and OOMs on the first real 73 GB request, so this cannot be left
    # to the behavioural tests — but nor can it honestly be called a behavioural test itself.
    # `tarfile` mode "w|" is non-seekable stream mode; plain "w" buffers and seeks.
    import inspect as _insp
    _src = _insp.getsource(server.H.do_GET)
    check('mode="w|"' in _src,
          "the bulk archive is written in tarfile STREAM mode, not buffered")
    check("addfile(ti, fh)" in _src,
          "bundles are streamed from an open handle, not read() into memory first")

    # A client that disconnects mid-archive must not take the server down with it. This IS behavioural:
    # a resumable sync disconnects by design, so the broken-pipe path is ordinary operation, not an
    # edge case — and an unhandled traceback per chunk would bury the log.
    import socket as _sock
    _c = _sock.create_connection(("127.0.0.1", _port), timeout=20)
    _c.sendall(b"GET /api/witnesses?from=100&count=10 HTTP/1.0\r\n\r\n")
    _c.recv(64)          # take the first few bytes only
    _c.close()           # ...then walk away mid-stream
    time.sleep(0.2)
    st, _ = _get("/api/witnesses?from=100&count=2")
    check(st == 200, "the server still serves after a client disconnected mid-archive")

    # The single-block endpoint must serve the SAME file the bulk one does; they share bundle_path so
    # they cannot drift, and this asserts that rather than trusting it.
    st, one = _get("/api/witness/104")
    check(st == 200 and json.loads(one)["height"] == 104,
          "the single endpoint serves the same bundle the bulk endpoint does")
finally:
    _srv.shutdown()
    server.BRIDGE_DIR, server.BULK_MAX = _orig_bridge, _orig_bulk

# ---------------------------------------------------------------------------------------------
# Audit #3 F-4 / N-2 — peer-supplied strings and peer-supplied sizes.
# ---------------------------------------------------------------------------------------------
import sync_bundles as _sb

# F-4: the peer's range id reaches a filesystem path. Validate it as a SHAPE before any use.
for _bad in ["../../etc/passwd", "1/../../x", "0-1/../../y", "a", "", "-5", "1-"]:
    check(server.parse_any_range(_bad) is None,
          f"peer id {_bad!r} is refused by parse_any_range")
check(server.parse_any_range("100-199") == (100, 199),
      "a legitimate range id still parses — the validator is not refusing everything")

# Order matters as much as presence: validating AFTER the open() would be no defence at all.
# CODE ONLY — comments are stripped first. The first version of this check compared raw source
# positions and failed, because the comment explaining the fix QUOTES the open() call it protects, so
# `index()` found the comment rather than the code. A source-order assertion that a comment can move
# is not measuring order.
_src = "\n".join(l for l in inspect.getsource(server.sync_from_peers).splitlines()
                 if not l.lstrip().startswith("#"))
check("parse_any_range(rid) is None" in _src, "sync validates the peer's id")
check(_src.index("parse_any_range(rid)") < _src.index("proof_{rid}.bin"),
      "the id is validated BEFORE it is used to build a filesystem path")

# N-2: a tar member declaring an enormous size must be refused, not buffered into memory.
_out = tempfile.mkdtemp()
_buf = _io.BytesIO()
with tarfile.open(fileobj=_buf, mode="w") as _tf:
    _man = json.dumps({"served": [], "missing": []}).encode()
    _ti = tarfile.TarInfo("MANIFEST.json"); _ti.size = len(_man); _tf.addfile(_ti, _io.BytesIO(_man))
    _ti = tarfile.TarInfo("bundle_1.json"); _ti.size = 10; _tf.addfile(_ti, _io.BytesIO(b"0123456789"))
    _big = tarfile.TarInfo("bundle_2.json"); _big.size = _sb.MAX_MEMBER_BYTES + 1
    _tf.addfile(_big, _io.BytesIO(b"\0" * _big.size))
_w, _m = _sb.extract(_buf.getvalue(), _out)
_files = sorted(os.listdir(_out))
check("bundle_2.json" not in _files, "an oversized tar member is refused rather than written")
check("bundle_1.json" in _files,
      "a normal member in the same archive is still written — the cap is not refusing everything")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — broke the already-folded check and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the logic was broken on purpose and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("spine and fold logic behaves (GPU-dependent paths excluded — see the module docstring).")

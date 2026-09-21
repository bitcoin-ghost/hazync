#!/usr/bin/env python3
"""Claim, beat and submit board work from a box that has NO prover and NO GPU.

⛔ WHY THIS IS SEPARATE FROM THE PROVER. The operator's rule for tip-idle board work (2026-09-21) is
that the `G H O S T` key never leaves a box they own. `hazync run --distributed` does claim, prove and
submit in one process, so honouring that rule would mean proving on the trusted box — and the trusted
box has no GPU. Measured on `hazync-proof` (16 cores, no CUDA), block 170, the SMALLEST block on the
chain, five segments at po2 20:

    proved range [170..170] from bridge bundle in 535.3s

8.9 minutes, against a ~7-minute gap between tip blocks. The aggregate has to run on rented GPUs.

⇒ But the key is only needed at the ENDS. A claim is a signed POST; a submit reads a receipt file and
posts it signed; neither touches a prover. So this module runs where the key is, the fleet proves
where the cards are, and the two meet over a receipt:

    trusted box (key, no GPU)            rented GPUs (no key, disposable)
      claim()          -> height
                                     ->  fetch the witness, prove it
      submit(receipt)  <- receipt    <-  receipt comes back
      beat() keeps the claim alive while that happens

⚠ Everything here is lifted from `coordinator/hazync`, which learned each of these the hard way. The
three that a rewrite gets wrong are marked ⛔ below: the reused nonce (#268), the progress-gated beat
(#256), and treating a busy board as idle rather than as failure (#319).

⛔ NOTHING HERE LOGS KEY MATERIAL. The secret is read, used to sign, and never printed — not at debug
level, not in an error path. Public keys and handles are public and are fine to print.

    python3 tip_board.py --selftest            # assertions; exit 0 on success
    python3 tip_board.py --selftest --control  # guards removed; MUST fail
    python3 tip_board.py --whoami              # print the identity's PUBLIC id and handle
"""
import argparse
import base64
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

COORD = os.environ.get("COORD_URL", "https://hazync.org").rstrip("/")
UA = "hazync-tip-board/1"

# ⛔ Point HAZYNC_HOME somewhere else and YOU ARE A DIFFERENT CONTRIBUTOR. Same rule as the CLI, and
# the reason tip-idle work can be run under `G H O S T` without touching the box's own identity.
HOME = pathlib.Path(os.environ.get("HAZYNC_HOME") or os.path.expanduser("~/.hazync"))

# The coordinator's own words for "the board is busy, come back later". None of these is a fault:
#   nothing available — every free block is taken right now
#   already holds     — THIS KEY is at its 4-unfinished-claim cap (#319). Routine for G H O S T,
#                       whose board workers share the same cap as the tip rig.
#   rate limit        — asking too fast
# ⛔ Classifying any of these as an error is what turns a busy board into a dead loop and an alert.
BENIGN = ("nothing available", "already holds", "rate limit")


def _ed():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization as ser
        return Ed25519PrivateKey, ser
    except ImportError:
        raise SystemExit("missing the signing library `cryptography` (pip install cryptography)")


def identity(home=None):
    """(sk, pub_hex, handle) for $HAZYNC_HOME. Never creates a key here — see below.

    ⛔ UNLIKE THE CLI, THIS REFUSES TO GENERATE ONE. `hazync id` creating a key on first use is right
    for a contributor getting started; here it would mean a typo in HAZYNC_HOME silently mints a NEW
    identity and quietly credits the tip rig's work to a stranger nobody can find again. A missing key
    is a configuration error and says so.
    """
    d = pathlib.Path(home) if home else HOME
    Ed25519PrivateKey, ser = _ed()
    key = d / "key.hex"
    if not key.exists():
        raise SystemExit(f"no identity at {key} — point HAZYNC_HOME at the directory holding the key "
                         f"you want the work credited to. This never creates one.")
    sk = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key.read_text().strip()))
    pub = sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()
    handle = (d / "handle").read_text().strip() if (d / "handle").exists() else ("ghost:" + pub[:6])
    return sk, pub, handle


def _post(path, body, timeout=180):
    req = urllib.request.Request(COORD + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:            # the coordinator answers JSON {"error": …} on 4xx/5xx
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"error": f"coordinator error {e.code}: {e.reason}"}


def claim(post_fn=None, ident=None, tries=3, sleep=time.sleep):
    """Ask for one block. Returns {"state": "claimed"|"idle"|"error", …}.

    ⛔ ONE NONCE FOR THE WHOLE RETRY SET (#268). The retries exist because a LOST RESPONSE is
    indistinguishable from a refusal, and the server has usually already committed the claim. Minting
    a fresh nonce per attempt makes the server hand out a SECOND block and orphans the first for an
    hour. The nonce is what lets it hand back the block it already gave.
    """
    post_fn = post_fn or _post
    sk, pub, handle = ident or identity()
    nonce, ts = os.urandom(16).hex(), int(time.time())
    body = {"pubkey": pub, "handle": handle, "nonce": nonce, "ts": ts,
            "sig": sk.sign(f"claim:{nonce}:{ts}".encode()).hex()}
    res = None
    for k in range(tries):
        try:
            res = post_fn("/api/claim", body)       # ⛔ same `body`, same nonce, every attempt
            break
        except Exception as e:                      # a dropped connection is exactly the lost-response case
            res = {"error": f"{type(e).__name__}: {e}"}
            if k == tries - 1:
                break
            sleep(5 * (k + 1))
    if res and res.get("ok"):
        return {"state": "claimed", "range": str(res["range"]),
                "ttl": int(res.get("ttl", 3600)), "handle": handle}
    err = str((res or {}).get("error") or res)
    if any(s in err for s in BENIGN):
        return {"state": "idle", "why": err}
    return {"state": "error", "why": err}


def beat(rng, progress, last_beaten, post_fn=None, ident=None):
    """Keep a claim alive — but ONLY on evidence of progress. Returns the new `last_beaten`.

    ⛔ THE GATE IS THE WHOLE POINT (#256). Beating on a timer alone kept a HUNG prover's claim alive
    for HOURS, so a block nobody was making progress on could not be reclaimed by anyone else. A
    wedged fleet must LOSE its block. `progress` is a monotonic count of finished units (segments,
    joins — whatever the caller counts); the beat is sent only when it has RISEN.
    """
    if progress <= last_beaten:
        return last_beaten
    post_fn = post_fn or _post
    sk, pub, _ = ident or identity()
    ts = int(time.time())
    try:
        post_fn("/api/beat", {"range": str(rng), "pubkey": pub, "ts": ts,
                              "sig": sk.sign(f"{rng}:{ts}".encode()).hex()})
    except Exception:
        pass                                        # a missed beat is survivable; the claim has a TTL
    return progress


def submit(rng, receipt, post_fn=None, ident=None):
    """POST a receipt produced ANYWHERE. Returns (ok, error). Never raises on a rejection.

    ⚠ `receipt` is the bytes of `RCPTDIR/<rng>.bin` as the prover wrote them — this box does not need
    a prover, a GPU or even the bundle to submit them. That is what lets the key stay here.
    ⚠ "already proven" is SUCCESS: another contributor landing the block first is a benign race, not a
    failure, and treating it as one makes an idle loop alert on normal board traffic.
    """
    post_fn = post_fn or _post
    sk, pub, handle = ident or identity()
    if not receipt:
        return False, "empty receipt — nothing was proved"
    res = post_fn("/api/submit", {"range": str(rng), "pubkey": pub, "handle": handle,
                                  "sig": sk.sign(receipt).hex(),
                                  "receipt": base64.b64encode(receipt).decode()})
    if res.get("ok"):
        return True, ""
    err = str(res.get("error") or res)
    if "already proven" in err:
        return True, ""
    return False, err


# The keys a real bridge bundle carries. The FIXTURE shape (block_<h>.json) has none of them:
# bits/coinbase_hex/merkle/nonce/prev/txs and no accumulator state at all.
BUNDLE_KEYS = ("height", "in_roots", "in_leaves", "in_tip", "witness")


def looks_like_bundle(obj):
    """(ok, why). A bundle proves a chain transition; a fixture cannot.

    ⛔ THIS IS THE TRAP #367 SETS. `tip_smoke` proves `block_<h>.json` -- the FIXTURE -- through
    build_full(), and prover/host/src/main.rs says in as many words that that path "serves the FIXTURE
    shape and cannot produce a board block at all (#361)". The two files sit side by side with similar
    names and the same height in them:

        fixture  bits, coinbase_hex, height, merkle, nonce, prev, recent_times, time, txs, version
        bundle   height, in_epoch_start, in_leaves, in_nbits, in_recent, in_roots, in_time, in_tip, witness

    Only the bundle carries the utreexo pre-state (`in_roots`, `in_leaves`, `in_tip`), which is what
    makes the proof commit to the REAL UTXO transition. Submitting a fixture-derived receipt to the
    board wastes a claim and an hour of TTL.
    """
    if not isinstance(obj, dict):
        return False, f"not a JSON object ({type(obj).__name__})"
    missing = [k for k in BUNDLE_KEYS if k not in obj]
    if missing:
        extra = sorted(set(obj) & {"txs", "coinbase_hex", "merkle", "nonce", "prev"})
        hint = f" — this looks like the FIXTURE shape ({', '.join(extra)})" if extra else ""
        return False, f"missing {', '.join(missing)}{hint}"
    return True, ""


def fetch_bundle(height, dest, opener=None):
    """Download the bridge bundle for `height` and REFUSE anything that is not one.

    Returns (ok, why). Writes `dest` only on success, so a rejected download cannot be picked up by a
    later step that merely checks the file exists.
    """
    import urllib.request
    url = witness_url(height)
    try:
        op = opener or (lambda u: urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": UA}), timeout=300))
        raw = op(url).read()
    except Exception as e:                              # a 404 here is "no bundle for that height"
        return False, f"{url}: {type(e).__name__}: {str(e)[:120]}"
    try:
        obj = json.loads(raw)
    except Exception as e:
        return False, f"{url}: not JSON ({type(e).__name__})"
    ok, why = looks_like_bundle(obj)
    if not ok:
        return False, f"{url}: {why}"
    if str(obj.get("height")) != str(height):
        # ⚠ A bundle for the WRONG height verifies fine and proves the wrong block.
        return False, f"{url}: bundle says height {obj.get('height')}, asked for {height}"
    return _write_bundle(raw, dest)


def _write_bundle(raw, dest):
    """Write validated bytes atomically. Split out so the ssh source shares the SAME validation."""
    tmp = f"{dest}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(raw)
    os.replace(tmp, dest)
    return True, ""


def fetch_bundle_ssh(height, dest, host, remote_dir="/var/lib/hazync/tip_bundles", runner=None):
    """Fetch a TIP bundle straight off the bridge host. Returns (ok, why).

    ⛔ WHY NOT SYNC THE TWO SERVERS. The tip bridge writes to its own box and the coordinator serves
    `/api/witness/<h>` from a different box and a different path, so the obvious fix is a server-to-
    server sync. Measured 2026-09-21: neither direction has ssh trust
    (`Permission denied (publickey)` both ways), so that fix starts by adding STANDING TRUST between
    two production boxes — a real security change for a file the driver can simply read itself.

    The driver already has ssh to both. So a tip height is fetched from the bridge host and a board
    height from the coordinator's API, and nothing new is trusted.

    ⚠ The bridge writes bundles ATOMICALLY (`write bundle_<h>.json.tmp` then `rename`,
    main.rs:3914), so there is no partial-file window to guard against here.
    ⚠ Until the walk passes HAZYNC_BRIDGE_EMIT_FROM (967,500; it was at ~861,857 on 2026-09-21) this
    directory is EMPTY BY DESIGN. "not found" is the expected answer, not a fault.
    """
    import subprocess
    remote = f"{remote_dir}/bundle_{int(height)}.json"
    run = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, timeout=600))
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host, f"cat {remote}"])
    if getattr(r, "returncode", 1) != 0:
        err = (getattr(r, "stderr", b"") or b"").decode(errors="replace").strip()[:120]
        return False, f"{host}:{remote}: {err or 'not found (the bridge may not have reached EMIT_FROM yet)'}"
    raw = getattr(r, "stdout", b"") or b""
    try:
        obj = json.loads(raw)
    except Exception as e:
        return False, f"{host}:{remote}: not JSON ({type(e).__name__})"
    ok, why = looks_like_bundle(obj)
    if not ok:
        return False, f"{host}:{remote}: {why}"
    if str(obj.get("height")) != str(height):
        return False, f"{host}:{remote}: bundle says height {obj.get('height')}, asked for {height}"
    return _write_bundle(raw, dest)


def witness_url(height):
    """Where the fleet fetches the bundle for a claimed height.

    ⚠ Measured 2026-09-21: `/api/witness/<h>` serves heights ≤ 418,268 and 404s above. The board
    frontier was 113,536, so every CLAIMABLE height is covered with room to spare — but a caller that
    assumes any height works will get a 404 for a tip block, which is a different job entirely.
    """
    return f"{COORD}/api/witness/{int(height)}"


# ── self-test ────────────────────────────────────────────────────────────────────────────────────
def selftest(control=False):
    import tempfile
    fails = []

    def check(ok, what):
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails.append(what)

    if control:
        # ⛔ The control removes the two guards a rewrite actually gets wrong: it mints a FRESH NONCE
        # per retry (#268) and it beats on every call regardless of progress (#256).
        global claim, beat, looks_like_bundle
        _real_claim = claim

        def claim(post_fn=None, ident=None, tries=3, sleep=time.sleep):      # noqa: F811
            sk, pub, handle = ident or identity()
            res = None
            for k in range(tries):
                nonce, ts = os.urandom(16).hex(), int(time.time())           # ⛔ fresh nonce each try
                body = {"pubkey": pub, "handle": handle, "nonce": nonce, "ts": ts,
                        "sig": sk.sign(f"claim:{nonce}:{ts}".encode()).hex()}
                try:
                    res = post_fn("/api/claim", body); break
                except Exception as e:
                    res = {"error": str(e)}
            if res and res.get("ok"):
                return {"state": "claimed", "range": str(res["range"]), "ttl": 3600, "handle": handle}
            return {"state": "error", "why": str((res or {}).get("error"))}  # ⛔ busy board = error

        def looks_like_bundle(obj):                                          # noqa: F811
            return True, ""                                                # ⛔ accepts a fixture

        def beat(rng, progress, last_beaten, post_fn=None, ident=None):      # noqa: F811
            sk, pub, _ = ident or identity()
            ts = int(time.time())
            post_fn("/api/beat", {"range": str(rng), "pubkey": pub, "ts": ts,
                                  "sig": sk.sign(f"{rng}:{ts}".encode()).hex()})
            return progress                                                  # ⛔ no progress gate

    # A throwaway identity so the test never touches a real key.
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tipboard_"))
    Ed25519PrivateKey, ser = _ed()
    sk0 = Ed25519PrivateKey.generate()
    (tmp / "key.hex").write_text(
        sk0.private_bytes(ser.Encoding.Raw, ser.PrivateFormat.Raw, ser.NoEncryption()).hex())
    (tmp / "handle").write_text("G H O S T")
    ident = identity(tmp)
    check(ident[2] == "G H O S T", f"handle read from disk ({ident[2]!r})")

    check_missing = tmp / "nope"
    try:
        identity(check_missing); ok = False
    except SystemExit:
        ok = True
    check(ok, "⛔ a missing key REFUSES rather than minting a new identity and crediting a stranger")

    # ── the reused nonce (#268) ──────────────────────────────────────────────────────────────────
    seen = []

    def flaky(path, body, **kw):
        seen.append(body.get("nonce"))
        if len(seen) < 3:
            raise ConnectionResetError("response lost")
        return {"ok": True, "range": "113537", "ttl": 3600}

    r = claim(post_fn=flaky, ident=ident, sleep=lambda s: None)
    check(r["state"] == "claimed" and r["range"] == "113537", f"a lost response retries to a claim ({r})")
    check(len(set(seen)) == 1,
          f"⛔ ALL {len(seen)} attempts carried ONE nonce — a fresh nonce per retry makes the server "
          f"hand out a second block and orphans the first for an hour (got {len(set(seen))} distinct)")

    # ── a busy board is idle, not a fault (#319) ─────────────────────────────────────────────────
    for msg in ("nothing available", "this key already holds 4 claimed blocks that are not finished",
                "rate limit exceeded"):
        r = claim(post_fn=lambda p, b, **k: {"error": msg}, ident=ident, tries=1, sleep=lambda s: None)
        check(r["state"] == "idle", f"⇒ idle, not error: {msg[:46]!r} (got {r['state']})")
    r = claim(post_fn=lambda p, b, **k: {"error": "signature does not verify"}, ident=ident,
              tries=1, sleep=lambda s: None)
    check(r["state"] == "error",
          "⚠ but a REAL refusal is still an error — retrying cannot fix a bad signature")

    # ── the progress-gated beat (#256) ───────────────────────────────────────────────────────────
    beats = []

    def recorder(path, body, **kw):
        beats.append(body["range"]); return {"ok": True}

    lb = 0
    lb = beat("113537", 0, lb, post_fn=recorder, ident=ident)
    lb = beat("113537", 0, lb, post_fn=recorder, ident=ident)
    check(not beats, "⛔ NO progress ⇒ NO beat. A hung fleet must LOSE its block, not hold it for hours")
    lb = beat("113537", 5, lb, post_fn=recorder, ident=ident)
    check(len(beats) == 1 and lb == 5, f"progress ⇒ one beat ({len(beats)}, last_beaten={lb})")
    lb = beat("113537", 5, lb, post_fn=recorder, ident=ident)
    check(len(beats) == 1, "the SAME progress does not beat again")

    # ── submit ───────────────────────────────────────────────────────────────────────────────────
    ok, err = submit("113537", b"receipt-bytes", post_fn=lambda p, b, **k: {"ok": True}, ident=ident)
    check(ok, "a verified receipt submits")
    ok, err = submit("113537", b"r", post_fn=lambda p, b, **k: {"error": "already proven by someone"},
                     ident=ident)
    check(ok, "⚠ 'already proven' is SUCCESS — another contributor winning the race is not a failure")
    ok, err = submit("113537", b"r", post_fn=lambda p, b, **k: {"error": "METHOD_ID mismatch"},
                     ident=ident)
    check(not ok and "METHOD_ID" in err, "a real rejection is reported with its reason")
    ok, err = submit("113537", b"", post_fn=lambda p, b, **k: {"ok": True}, ident=ident)
    check(not ok, "⚠ an EMPTY receipt is refused here rather than posted as a proof of nothing")

    # ── the receipt is signed, and the signature is over the RECEIPT ─────────────────────────────
    got = {}
    submit("113537", b"abc", post_fn=lambda p, b, **k: got.update(b) or {"ok": True}, ident=ident)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(got["pubkey"])).verify(
            bytes.fromhex(got["sig"]), b"abc")
        sig_ok = True
    except Exception:
        sig_ok = False
    check(sig_ok, "the submitted signature verifies against the receipt bytes under the public key")
    check(base64.b64decode(got["receipt"]) == b"abc", "the receipt travels base64, unmodified")

    # ── the bundle-vs-fixture trap (#367) ────────────────────────────────────────────────────────
    BUNDLE = {"height": 120000, "in_roots": [], "in_leaves": 1, "in_tip": "aa", "witness": {},
              "in_nbits": 1, "in_time": 1, "in_epoch_start": 1, "in_recent": []}
    FIXTURE = {"height": 120000, "bits": 1, "coinbase_hex": "00", "merkle": "aa", "nonce": 1,
               "prev": "bb", "recent_times": [], "time": 1, "txs": [], "version": 1}
    ok, why = looks_like_bundle(BUNDLE)
    check(ok, "a real bridge bundle is accepted")
    ok, why = looks_like_bundle(FIXTURE)
    check(not ok and "FIXTURE" in why,
          f"⛔ the FIXTURE shape is REFUSED and named as such — proving it cannot produce a board "
          f"block at all (#361) ({why[:72]})")

    import tempfile as _tf, json as _j
    tdir = _tf.mkdtemp(prefix="bundle_")
    dest = os.path.join(tdir, "bundle_120000.json")

    class _Resp:
        def __init__(self, b): self._b = b
        def read(self): return self._b

    ok, why = fetch_bundle(120000, dest, opener=lambda u: _Resp(_j.dumps(BUNDLE).encode()))
    check(ok and os.path.exists(dest), f"a good bundle is fetched and written ({why})")
    os.unlink(dest)
    ok, why = fetch_bundle(120000, dest, opener=lambda u: _Resp(_j.dumps(FIXTURE).encode()))
    check(not ok and not os.path.exists(dest),
          "⛔ a FIXTURE download writes NO FILE — a rejected fetch must not be picked up later by a "
          "step that merely checks the path exists")
    ok, why = fetch_bundle(120000, dest, opener=lambda u: _Resp(_j.dumps(dict(BUNDLE, height=999)).encode()))
    check(not ok and "height 999" in why,
          "⚠ a bundle for the WRONG height is refused — it would verify fine and prove the wrong block")
    ok, why = fetch_bundle(120000, dest, opener=lambda u: _Resp(b"<html>404</html>"))
    check(not ok and "not JSON" in why, "a 404 page is not silently written as a bundle")

    # ── the ssh source for TIP heights ───────────────────────────────────────────────────────────
    class _R:
        def __init__(self, rc, out=b"", err=b""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    dest2 = os.path.join(tdir, "bundle_967500.json")
    ok, why = fetch_bundle_ssh(967500, dest2, "hazync-coord",
                               runner=lambda c: _R(0, _j.dumps(dict(BUNDLE, height=967500)).encode()))
    check(ok and os.path.exists(dest2), f"a tip bundle is fetched straight off the bridge host ({why})")
    os.unlink(dest2)
    ok, why = fetch_bundle_ssh(967500, dest2, "hazync-coord",
                               runner=lambda c: _R(1, b"", b"cat: No such file or directory"))
    check(not ok and not os.path.exists(dest2),
          "⚠ an absent tip bundle is a clean 'not found' and writes nothing — EXPECTED until the walk "
          "passes EMIT_FROM=967,500")
    ok, why = fetch_bundle_ssh(967500, dest2, "hazync-coord",
                               runner=lambda c: _R(0, _j.dumps(FIXTURE).encode()))
    check(not ok and "FIXTURE" in why,
          "⛔ the ssh source runs the SAME shape check — a fixture from the bridge host is refused too")

    print()
    expected = {"ALL", "NO progress", "FIXTURE shape is REFUSED"}
    if control:
        hit = {e for e in expected if any(e in f for f in fails)}
        if hit:
            print("CONTROL OK — the guards were removed and the assertions that detect it failed:")
            for e in sorted(hit):
                print(f"  - {e}")
            return 0
        print("CONTROL FAILED — a per-retry nonce and an ungated beat both went undetected.")
        return 1
    if fails:
        print(f"FAILED {len(fails)}: " + "; ".join(fails))
        return 1
    print("all good")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--control", action="store_true")
    ap.add_argument("--whoami", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest(control=a.control)
    if a.whoami:
        _, pub, handle = identity()
        # PUBLIC key only. The secret is never printed, at any verbosity.
        print(f"  identity : {pub[:10]}…   handle: {handle!r}")
        print(f"  home     : {HOME}")
        print(f"  coord    : {COORD}")
        return 0
    ap.error("--selftest or --whoami")


if __name__ == "__main__":
    sys.exit(main())

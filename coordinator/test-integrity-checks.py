#!/usr/bin/env python3
"""Do the integrity checks still detect what they exist to detect?

check-spine.py, check-continuity.py and check-proofs.py run unattended and push to a phone. The dangerous failure is
not one of them reporting a fault. It is one reporting CLEAN while a fault exists. So each check is run against a good
fixture, where it must pass, and then against the same fixture with ONE fault planted, where it must fail and say what
failed. Every planted fault is a positive control. The alert wrapper is held to the same standard: it must push once,
hourly while a failure lasts, and once on recovery.

Scope, stated plainly: this tests the CHECKERS against synthetic fixtures, with a fake hazync-verify, a fake
bitcoin-cli, a fake hazync-host and a local HTTP server standing in for api.hazync.org and the explorers. It does NOT
verify real proofs: that needs the release binaries and the live board, which CI does not have. Run the checks on the
coordinator for that.

    python3 coordinator/test-integrity-checks.py
"""
import hashlib
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SPINE = os.path.join(HERE, "check-spine.py")
CONT = os.path.join(HERE, "check-continuity.py")
PROOFS = os.path.join(HERE, "check-proofs.py")
WRAP = os.path.join(HERE, "deploy", "hazync-run-check.sh")
T = tempfile.mkdtemp(prefix="hz-integrity-")
fails = []


def check(cond, what, out=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)
        if out:
            print("      " + out.strip()[-1500:].replace("\n", "\n      "))


def run(cmd, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=180)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def script(name, body):
    path = os.path.join(T, name)
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


FAKE_VERIFY = script("fake-verify", """#!/usr/bin/env python3
import os, sys
if int(os.environ.get("FAKE_VERIFY_RC", "0")):
    print("verification FAILED: the seal does not match", file=sys.stderr)
    sys.exit(1)
print(open(os.environ["FAKE_VERIFY_OUT"]).read())
""")

FAKE_CLI = script("fake-bitcoin-cli", """#!/usr/bin/env python3
import json, os, sys
if os.environ.get("FAKE_BTC_DOWN"):
    print("error: Could not connect to the server 127.0.0.1:8332", file=sys.stderr)
    sys.exit(1)
chain = json.load(open(os.environ["FAKE_CHAIN"]))
args = [a for a in sys.argv[1:] if not a.startswith("-")]
if args[0] == "getblockhash":
    if args[1] not in chain["hashes"]:
        print("error code: -8", file=sys.stderr)
        sys.exit(8)
    print(chain["hashes"][args[1]])
elif args[0] == "getblockheader":
    print(json.dumps({"hash": args[1], "chainwork": chain["work"][args[1]]}))
""")

FAKE_HOST = script("fake-host", """#!/usr/bin/env python3
import json, os, sys
path = sys.argv[2]
if open(path, "rb").read().startswith(b"UNVERIFIABLE"):
    print("verification FAILED", file=sys.stderr)
    sys.exit(1)
kv = json.load(open(os.environ["FAKE_HOST_MAP"]))[os.path.basename(path)]
print("RANGE-OK " + " ".join(f"{k}={v}" for k, v in kv.items()) + " anchored=no")
""")

GENESIS = "6fe28c0ab6f1b372c1a6a246ae63f74f931e8365e15a089c68d6190000000000"
TIP = "00000000" + "ab" * 28                          # display order, as hazync-verify and bitcoind print it
TIP_INTERNAL = bytes.fromhex(TIP)[::-1].hex()          # as the coordinator stores it
WORK = 200715149346568
MID = "37987b85" + "0" * 56


def tipn(h):
    return f"{h:08x}" + "cd" * 28


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def spine_fixture(hi=3, ts=None, data=b"genesis-proof-bytes", sha=None, out_tip=TIP_INTERNAL):
    d = os.path.join(T, "spine")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    with open(os.path.join(d, "spine.bin"), "wb") as f:
        f.write(data)
    head = {"lo": 1, "hi": hi, "out_tip": out_tip, "sha256": sha or hashlib.sha256(data).hexdigest(),
            "ts": time.time() if ts is None else ts}
    with open(os.path.join(d, "spine.json"), "w") as f:
        json.dump(head, f)
    return d, head


def verify_out(**over):
    v = {"verified": True, "genesis_anchored": True, "guest_image_id": MID, "height": 3, "tip_hash": TIP,
         "cumulative_work": WORK}
    v.update(over)
    # A fresh file per call. One shared path let a default written AFTER a planted fault overwrite it, and three
    # planted faults then passed as "holds" (caught by this suite's first run, 2026-09-15).
    fd, p = tempfile.mkstemp(prefix="verify-", suffix=".json", dir=T)
    os.close(fd)
    with open(p, "w") as f:
        json.dump(v, f)
    return p


def chain_fixture(block=TIP, work=WORK):
    fd, p = tempfile.mkstemp(prefix="chain-", suffix=".json", dir=T)
    os.close(fd)
    with open(p, "w") as f:
        json.dump({"hashes": {"3": block}, "work": {block: format(work, "064x")}}, f)
    return p


def db_fixture(n=3, extra_lo=None, drop=(), mutate=None):
    """Blocks 1..n seamed from genesis, ending on the spine's tip; a fold 1-2; ranges rows with receipt hashes."""
    p = os.path.join(T, "c.db")
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE ranges(id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, status TEXT, receipt_sha TEXT)")
    c.execute("CREATE TABLE vranges(id TEXT PRIMARY KEY, lo INTEGER, hi INTEGER, in_tip TEXT, out_tip TEXT, pubkey TEXT,"
              " handle TEXT, ts REAL, out_leaves INTEGER, range_work TEXT, in_bhash TEXT, out_bhash TEXT)")
    rows = {}
    for h in range(1, n + 1):
        rows[str(h)] = dict(lo=h, hi=h, in_tip=GENESIS if h == 1 else tipn(h - 1),
                            out_tip=TIP_INTERNAL if h == n else tipn(h),
                            in_bhash=f"b{h - 1:063d}", out_bhash=f"b{h:063d}")
    if n >= 2:
        rows["1-2"] = dict(lo=1, hi=2, in_tip=GENESIS, out_tip=rows["2"]["out_tip"], in_bhash=rows["1"]["in_bhash"],
                           out_bhash=rows["2"]["out_bhash"])
    if extra_lo:
        rows[str(extra_lo)] = dict(lo=extra_lo, hi=extra_lo, in_tip=TIP_INTERNAL, out_tip=tipn(extra_lo),
                                   in_bhash=f"b{extra_lo - 1:063d}", out_bhash=f"b{extra_lo:063d}")
    for k in drop:
        rows.pop(k, None)
    if mutate:
        mutate(rows)
    for rid, r in rows.items():
        c.execute("INSERT INTO vranges VALUES(?,?,?,?,?,'','t',0,0,'0',?,?)",
                  (rid, r["lo"], r["hi"], r["in_tip"], r["out_tip"], r["in_bhash"], r["out_bhash"]))
        c.execute("INSERT INTO ranges VALUES(?,?,?,'verified',?)",
                  (rid, r["lo"], r["hi"], hashlib.sha256(f"proof-{rid}".encode()).hexdigest()))
    c.commit()
    c.close()
    return p, rows


def proofs_fixture(rows):
    d = os.path.join(T, "proofs")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d)
    mapping = {}
    for rid, r in rows.items():
        with open(os.path.join(d, f"proof_{rid}.bin"), "wb") as f:
            f.write(f"proof-{rid}".encode())
        mapping[f"proof_{rid}.bin"] = {k: r[k] for k in ("lo", "hi", "in_tip", "out_tip", "in_bhash", "out_bhash")}
    mp = os.path.join(T, "host-map.json")
    with open(mp, "w") as f:
        json.dump(mapping, f)
    return d, mp


# ── check-spine.py, coordinator mode ─────────────────────────────────────────────────────────────────────────────────
print("== check-spine.py on the coordinator ==")


def spine_local(extra_args=(), env=None, **kw):
    sd, _ = spine_fixture(**kw)
    db, _ = db_fixture()
    e = {"FAKE_VERIFY_OUT": verify_out(), "FAKE_CHAIN": chain_fixture()}
    e.update(env or {})
    return run([sys.executable, SPINE, "--spine-dir", sd, "--db", db, "--verify", FAKE_VERIFY, "--bitcoin-cli",
                FAKE_CLI, "--method-id", MID, *extra_args], e)


rc, out = spine_local()
check(rc == 0 and "holds" in out, "a good genesis proof passes: verifies, canonical guest, height, tip, chainwork", out)
rc, out = spine_local(env={"FAKE_VERIFY_RC": "1"})
check(rc == 1 and "rejected" in out, "a proof hazync-verify rejects FAILS", out)
rc, out = spine_local(env={"FAKE_VERIFY_OUT": verify_out(genesis_anchored=False)})
check(rc == 1 and "genesis-anchored" in out, "a proof that is not genesis-anchored FAILS", out)
rc, out = spine_local(env={"FAKE_VERIFY_OUT": verify_out(guest_image_id="deadbeef" + "0" * 56)})
check(rc == 1 and "not the canonical" in out, "a proof from another guest FAILS", out)
rc, out = spine_local(hi=4)
check(rc == 1 and "head record says 4" in out, "a proof whose height is not the head record's FAILS", out)
rc, out = spine_local(sha="00" * 32)
check(rc == 1 and "do not hash" in out, "bytes that do not hash to the head record FAIL (after retrying)", out)
rc, out = spine_local(out_tip=tipn(9))
check(rc == 1 and "out_tip" in out, "a head record whose out_tip is not the proof's tip FAILS", out)
rc, out = spine_local(env={"FAKE_CHAIN": chain_fixture(block="00000000" + "ee" * 28)})
check(rc == 1 and "is NOT Bitcoin's block" in out, "a tip that is not our node's block at that height FAILS", out)
rc, out = spine_local(env={"FAKE_CHAIN": chain_fixture(work=WORK + 1)})
check(rc == 1 and "chainwork" in out, "cumulative work that is not the node's chainwork FAILS", out)
rc, out = spine_local(env={"FAKE_BTC_DOWN": "1"})
check(rc == 2 and "COULD NOT CHECK" in out, "an unreachable node is COULD NOT CHECK (exit 2), never a pass", out)
rc, out = spine_local(extra_args=["--verify", os.path.join(T, "no-such-verifier")])
check(rc == 2 and "could not run" in out, "a missing hazync-verify is COULD NOT CHECK, never a pass", out)
sd, _ = spine_fixture()
db, _ = db_fixture()
rc, out = run([sys.executable, SPINE, "--spine-dir", sd, "--db", db, "--verify", FAKE_VERIFY, "--bitcoin-cli", FAKE_CLI,
               "--repo", os.path.join(T, "no-repo")], {"FAKE_VERIFY_OUT": verify_out(), "FAKE_CHAIN": chain_fixture()})
check(rc == 2 and "guest id" in out, "no canonical METHOD_ID to compare with is COULD NOT CHECK", out)

# Stall: old head AND a proof waiting at hi+1 fails; old head with nothing waiting does not.
old = time.time() - 3 * 3600
sd, _ = spine_fixture(ts=old)
db, _ = db_fixture(extra_lo=4)
rc, out = run([sys.executable, SPINE, "--spine-dir", sd, "--db", db, "--verify", FAKE_VERIFY, "--bitcoin-cli", FAKE_CLI,
               "--method-id", MID], {"FAKE_VERIFY_OUT": verify_out(), "FAKE_CHAIN": chain_fixture()})
check(rc == 1 and "has not advanced" in out, "a spine stalled for 3 h with block 4 proven and waiting FAILS", out)
db, _ = db_fixture()
rc, out = run([sys.executable, SPINE, "--spine-dir", sd, "--db", db, "--verify", FAKE_VERIFY, "--bitcoin-cli", FAKE_CLI,
               "--method-id", MID], {"FAKE_VERIFY_OUT": verify_out(), "FAKE_CHAIN": chain_fixture()})
check(rc == 0 and "nothing proven starts at block 4" in out, "an old spine with nothing waiting above it passes", out)

# ── check-spine.py, web box mode ─────────────────────────────────────────────────────────────────────────────────────
print("== check-spine.py from the web box ==")
SITE = {}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code, body = SITE.get(self.path, (404, b"not found"))
        self.send_response(code)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
PROOF = b"genesis-proof-bytes"


def site(proof=PROOF, head_sha=None, ref=TIP, ref2=TIP, ref_code=200, ref2_code=200):
    SITE.clear()
    head = {"lo": 1, "hi": 3, "out_tip": TIP_INTERNAL, "sha256": head_sha or hashlib.sha256(PROOF).hexdigest()}
    SITE["/api/spine"] = (200, json.dumps(head).encode())
    SITE["/api/spine/proof"] = (200, proof)
    SITE["/ref1/block-height/3"] = (ref_code, ref.encode())
    SITE["/ref2/block-height/3"] = (ref2_code, ref2.encode())


def spine_remote(refs=("/ref1", "/ref2")):
    args = [sys.executable, SPINE, "--url", BASE, "--verify", FAKE_VERIFY]
    for r in refs:
        args += ["--reference", BASE + r]
    return run(args, {"FAKE_VERIFY_OUT": verify_out()})


site()
rc, out = spine_remote()
check(rc == 0 and "according to" in out, "the public copy verifies and its tip is the explorer's block", out)
site(proof=b"something else entirely")
rc, out = spine_remote()
check(rc == 1 and "do not hash" in out, "served bytes that do not hash to the served head record FAIL", out)
site(ref="00000000" + "ee" * 28)
rc, out = spine_remote()
check(rc == 1 and "is NOT Bitcoin's block" in out, "a tip the explorer does not have at that height FAILS", out)
site(ref_code=503)
rc, out = spine_remote()
check(rc == 0 and "/ref2" in out, "the first explorer down, the second answers: still checked, and passes", out)
site(ref_code=503, ref2_code=503)
rc, out = spine_remote()
check(rc == 2 and "no explorer answered" in out, "every explorer down is COULD NOT CHECK, never a pass", out)
site()
rc, out = spine_remote(refs=())
check(rc == 2, "no explorer configured is COULD NOT CHECK, never a pass", out)
srv.shutdown()

# ── check-continuity.py ─────────────────────────────────────────────────────────────────────────────────────────────
print("== check-continuity.py ==")


def continuity(allow=None, **kw):
    sd, _ = spine_fixture()
    db, _ = db_fixture(**kw)
    return run([sys.executable, CONT, "--db", db, "--spine-dir", sd] + (["--allow", allow] if allow else []))


rc, out = continuity()
check(rc == 0 and "Continuity holds" in out, "blocks 1-3 seamed from genesis to the spine's tip pass", out)
rc, out = continuity(mutate=lambda r: r["2"].update(in_tip=tipn(7)))
check(rc == 1 and "tip hash" in out and "2" in out, "a broken tip seam at block 2 FAILS", out)
rc, out = continuity(mutate=lambda r: r["3"].update(in_bhash="x" * 64))
check(rc == 1 and "boundary digest does not match" in out, "a broken boundary-digest seam at block 3 FAILS", out)
rc, out = continuity(mutate=lambda r: r["2"].update(in_bhash=""))
check(rc == 1 and "no boundary digest" in out, "a record with no boundary digest FAILS", out)
rc, out = continuity(mutate=lambda r: r["1"].update(in_tip=tipn(0)))
check(rc == 1 and "not from genesis" in out, "block 1 not starting from the genesis tip FAILS", out)
rc, out = continuity(mutate=lambda r: r["3"].update(out_tip=tipn(3)))
check(rc == 1 and "does not end on the genesis proof's tip" in out, "a last block that does not end on the spine FAILS",
      out)
rc, out = continuity(drop=("2",))
check(rc == 1 and "no record of their own" in out, "a block with no record of its own FAILS", out)
rc, out = continuity(drop=("2",), allow="2")
check(rc == 0, "a known hole accepted with --allow passes", out)
rc, out = continuity(drop=("2",), mutate=lambda r: r["3"].update(in_bhash="x" * 64), allow="2")
check(rc == 0 or "boundary digest" not in out, "(beside an accepted hole a seam cannot be checked, and is not faked)",
      out)
emptyd = os.path.join(T, "empty-spine")
os.makedirs(emptyd, exist_ok=True)
db, _ = db_fixture(n=0, drop=("1-2",))
rc, out = run([sys.executable, CONT, "--db", db, "--spine-dir", emptyd])
check(rc == 0 and "empty board" in out, "an empty board with no spine is vacuous, not a failure", out)
db, _ = db_fixture()
rc, out = run([sys.executable, CONT, "--db", db, "--spine-dir", emptyd])
check(rc == 2 and "COULD NOT CHECK" in out, "verified ranges but no spine file is COULD NOT CHECK", out)

# ── check-proofs.py ─────────────────────────────────────────────────────────────────────────────────────────────────
print("== check-proofs.py ==")


def proofs(mutate_files=None, mutate_map=None, host=FAKE_HOST, extra=()):
    db, rows = db_fixture()
    d, mp = proofs_fixture(rows)
    if mutate_files:
        mutate_files(d, db)
    if mutate_map:
        m = json.load(open(mp))
        mutate_map(m)
        json.dump(m, open(mp, "w"))
    return run([sys.executable, PROOFS, "--db", db, "--proofs", d, "--host", host, "--min-age", "0", *extra],
               {"FAKE_HOST_MAP": mp})


rc, out = proofs()
check(rc == 0 and "Every one of the 4 stored proofs" in out, "4 stored proofs (3 blocks and a fold) that hold pass", out)


def corrupt(d, db):
    with open(os.path.join(d, "proof_2.bin"), "wb") as f:
        f.write(b"proof-2 with a flipped bit")


rc, out = proofs(mutate_files=corrupt)
check(rc == 1 and "proof_2.bin" in out and "no longer hashes" in out, "a proof whose bytes changed FAILS", out)


def unverifiable(d, db):
    data = b"UNVERIFIABLE proof-3"
    with open(os.path.join(d, "proof_3.bin"), "wb") as f:
        f.write(data)
    c = sqlite3.connect(db)
    c.execute("UPDATE ranges SET receipt_sha=? WHERE id='3'", (hashlib.sha256(data).hexdigest(),))
    c.commit()
    c.close()


rc, out = proofs(mutate_files=unverifiable)
check(rc == 1 and "proof_3.bin" in out and "rejects" in out, "a proof that hashes right but no longer verifies FAILS",
      out)
rc, out = proofs(mutate_map=lambda m: m["proof_1-2.bin"].update(hi=3))
check(rc == 1 and "proof_1-2.bin" in out and "hi=3" in out, "a fold that verifies to a different range FAILS", out)


def orphan(d, db):
    with open(os.path.join(d, "proof_9.bin"), "wb") as f:
        f.write(b"proof-9")


rc, out = proofs(mutate_files=orphan, mutate_map=lambda m: m.update({"proof_9.bin": m["proof_1.bin"]}))
check(rc == 1 and "proof_9.bin" in out and "no verified record" in out, "a stored proof with no verified record FAILS",
      out)
rc, out = proofs(host=os.path.join(T, "no-such-host"))
check(rc == 2 and "COULD NOT CHECK" in out, "no host binary is COULD NOT CHECK, never a pass", out)
db, rows = db_fixture()
d, mp = proofs_fixture(rows)
rc, out = run([sys.executable, PROOFS, "--db", db, "--proofs", d, "--host", FAKE_HOST], {"FAKE_HOST_MAP": mp})
check(rc == 0 and "checking 0 stored proofs" in out, "files younger than --min-age (600 s) are left for the next night",
      out)

# ── repair-reclaimed-ranges.py (#339) ───────────────────────────────────────────────────────────────────────────────
print("== repair-reclaimed-ranges.py ==")
REPAIR = os.path.join(HERE, "repair-reclaimed-ranges.py")


def reclaimed_fixture():
    """Blocks 1..3 on the chain and block 5 off it, all verified by key pA; then four rows overwritten by a claim the
    way #339 did it. Row 2 is the clean case. Rows 1, 3 and 5 each plant one reason the repair must leave a row alone."""
    db, rows = db_fixture(extra_lo=5)
    d, mp = proofs_fixture(rows)
    c = sqlite3.connect(db)
    for col in ("assignee TEXT", "handle TEXT", "claimed_at REAL", "verified_at REAL", "last_beat REAL",
                "claim_nonce TEXT", "claim_signed INTEGER"):
        c.execute(f"ALTER TABLE ranges ADD COLUMN {col}")
    c.execute("CREATE TABLE submissions(id INTEGER PRIMARY KEY AUTOINCREMENT, range_id TEXT, pubkey TEXT, handle TEXT,"
              " receipt_sha TEXT, sig TEXT, verified INTEGER, note TEXT, ts REAL)")
    c.execute("UPDATE vranges SET pubkey='pA', handle='prover-a', ts=1000")
    c.execute("UPDATE ranges SET assignee='pA', handle='prover-a', verified_at=1000")
    for rid in rows:
        c.execute("INSERT INTO submissions(range_id,pubkey,handle,receipt_sha,sig,verified,note,ts)"
                  " VALUES(?,'pA','prover-a',?,'',1,'VERIFIED',1000)",
                  (rid, hashlib.sha256(f"proof-{rid}".encode()).hexdigest()))
    now = time.time()
    for rid, age in (("1", 86400), ("2", 86400), ("3", 60), ("5", 86400)):   # 3: claimed a minute ago
        c.execute("UPDATE ranges SET status='claimed', assignee='pB', handle='racer', receipt_sha=NULL,"
                  " verified_at=NULL, claimed_at=?, claim_nonce='n' WHERE id=?", (now - age, rid))
    c.commit()
    c.close()
    with open(os.path.join(d, "proof_1.bin"), "wb") as f:                     # 1: no longer what was accepted
        f.write(b"proof-1 with a flipped bit")
    return db, d, mp                                                          # 5: above the frontier (3)


def ranges_row(db, rid):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    r = dict(c.execute("SELECT * FROM ranges WHERE id=?", (rid,)).fetchone())
    c.close()
    return r


db, d, mp = reclaimed_fixture()
rc, out = run([sys.executable, PROOFS, "--db", db, "--proofs", d, "--host", FAKE_HOST, "--min-age", "0"],
              {"FAKE_HOST_MAP": mp})
check(rc == 1 and "proof_2.bin: has no verified record" in out, "check-proofs reports the overwritten row", out)
rc, out = run([sys.executable, REPAIR, "--db", db, "--proofs", d, "--frontier", "3"])
check(rc == 0 and "would restore 2" in out and ranges_row(db, "2")["status"] == "claimed",
      "a dry run names row 2 and changes nothing", out)
rc, out = run([sys.executable, REPAIR, "--db", db, "--proofs", d, "--frontier", "3", "--apply"])
r2 = ranges_row(db, "2")
check(rc == 0 and r2["status"] == "verified" and r2["receipt_sha"] == hashlib.sha256(b"proof-2").hexdigest()
      and r2["verified_at"] == 1000 and r2["assignee"] == "pA" and r2["handle"] == "prover-a"
      and r2["claim_nonce"] is None, "--apply restores row 2 from its verified submission: status, hash, time, prover", out)
check(ranges_row(db, "1")["status"] == "claimed" and "leave   1:" in out and "does not hash" in out,
      "a row whose proof file no longer matches is left alone", out)
check(ranges_row(db, "3")["status"] == "claimed" and "leave   3:" in out and "may still be live" in out,
      "a row claimed a minute ago is left alone", out)
check(ranges_row(db, "5")["status"] == "claimed" and "leave   5:" in out and "above the frontier" in out,
      "a row above the frontier (possibly a #281 re-offer) is left alone", out)
rc, out = run([sys.executable, PROOFS, "--db", db, "--proofs", d, "--host", FAKE_HOST, "--min-age", "0"],
              {"FAKE_HOST_MAP": mp})
check("proof_2.bin" not in out and "proof_3.bin: has no verified record" in out,
      "after the repair check-proofs no longer reports row 2, and still reports the rows left alone", out)
rc, out = run([sys.executable, REPAIR, "--db", db, "--proofs", d, "--frontier", "3", "--apply"])
check(rc == 0 and "restore 2" not in out and "0 row(s) restored" in out, "running it again restores nothing more", out)
rc, out = run([sys.executable, REPAIR, "--db", os.path.join(T, "no.db"), "--proofs", d, "--frontier", "3"])
check(rc == 2 and "COULD NOT RUN" in out, "no database is COULD NOT RUN, never 'nothing to do'", out)

# ── hazync-run-check.sh ─────────────────────────────────────────────────────────────────────────────────────────────
print("== hazync-run-check.sh ==")
STATE = os.path.join(T, "state")
ALERTS = os.path.join(T, "alerts.log")
FAKE_ALERT = script("fake-alert", '#!/bin/sh\nprintf "%s\\n" "$1" >> "$ALERTS"\n[ -z "$FAKE_ALERT_FAIL" ]\n')
PASSES = script("passes", "#!/bin/sh\necho 'The genesis proof holds.'\nexit 0\n")
BREAKS = script("breaks", "#!/bin/sh\necho '  FAIL the tip is NOT Bitcoin'\"'\"'s block'\nexit 1\n")
CANNOT = script("cannot", "#!/bin/sh\necho '  COULD NOT CHECK our node did not answer'\nexit 2\n")
WENV = {"CHECK_STATE_DIR": STATE, "HAZYNC_ALERT": FAKE_ALERT, "ALERTS": ALERTS, "CHECK_FAILS_BEFORE_ALERT": "2",
        "CHECK_REALERT_SECS": "3600"}


def alerts():
    return open(ALERTS).read().splitlines() if os.path.exists(ALERTS) else []


def wrap(cmd, name="demo", env=None):
    e = dict(WENV)
    e.update(env or {})
    return run(["bash", WRAP, name, cmd], e)


rc, _ = wrap(BREAKS)
check(rc == 0 and alerts() == [], "one failed run below the threshold (2) pushes nothing")
rc, _ = wrap(BREAKS)
check(rc == 0 and len(alerts()) == 1 and "FAILED" in alerts()[0], "the second failed run in a row pushes once")
rc, _ = wrap(BREAKS)
check(len(alerts()) == 1, "a third failed run inside the hour is not re-sent")
with open(os.path.join(STATE, "demo")) as f:
    n, _last = f.read().split()
with open(os.path.join(STATE, "demo"), "w") as f:
    f.write(f"{n} {int(time.time()) - 4000}\n")
wrap(BREAKS)
check(len(alerts()) == 2, "still failing an hour after the last push: pushed again")
wrap(PASSES)
check(len(alerts()) == 3 and "RECOVERED" in alerts()[2], "the first passing run after a push says RECOVERED")
wrap(PASSES)
check(len(alerts()) == 3, "passing again says nothing more")
wrap(CANNOT, name="cannot")
wrap(CANNOT, name="cannot")
check(len(alerts()) == 4 and "could not run" in alerts()[3], "a check that cannot run (exit 2) is pushed as could not run")
wrap(PASSES, name="quiet")
check(len(alerts()) == 4, "a check that never failed never pushes a recovery")
rc, _ = wrap(BREAKS, name="nosend", env={"FAKE_ALERT_FAIL": "1", "CHECK_FAILS_BEFORE_ALERT": "1"})
check(rc == 1, "a due push that cannot be sent exits 1, so the unit's OnFailure= alert is the fallback")

shutil.rmtree(T, ignore_errors=True)
print()
if fails:
    print(f"{len(fails)} check(s) FAILED: the integrity checks cannot be trusted to detect these.")
    sys.exit(1)
print("integrity checks: every planted fault was detected, and the alert wrapper pushes once, hourly and on recovery.")

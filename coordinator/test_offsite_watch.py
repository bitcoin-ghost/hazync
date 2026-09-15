#!/usr/bin/env python3
"""Backup notifications say something when they should, once, and the daily summary tells the truth.

deploy/hazync-offsite-watch.py pushes to the phone about the offsite copies in R2. What must hold:

  1. a healthy box sends nothing;
  2. Litestream stopping pushes ONE high-priority alert, repeats only after OFFSITE_REALERT_SECS, and
     sends one low-priority RECOVERED when it runs again;
  3. a stale ledger copy (older than OFFSITE_LAG_ALERT_SECS) and an unlistable R2 each alert;
  4. Litestream WARN/ERROR log lines push at default priority, INFO lines never, and a burst inside
     an hour is held and sent together, not dropped;
  5. the daily summary says "all good" at low priority only when receipts are all in R2, the hourly
     mirror ran clean, the ledger copy is fresh and a real restore of it passes; a missing receipt,
     a restore that yields garbage, a failed restore, or a failed mirror each make it high priority
     and name the problem.

Runs with a fake box (systemctl / journalctl / litestream answers), a fake R2 listing and real
SQLite files. No network, no boto3, no root.

  python3 test_offsite_watch.py            # must PASS
  python3 test_offsite_watch.py --control  # the watcher sees no problems and every summary check says ok; MUST FAIL
"""
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


W = load("offsite_watch", os.path.join(HERE, "deploy", "hazync-offsite-watch.py"))
M = load("offsite_proofs", os.path.join(HERE, "deploy", "hazync-offsite-proofs.py"))

tmp = tempfile.mkdtemp(prefix="offwatch_")
W.STATE_DIR = os.path.join(tmp, "state")
W.DRILL_DIR = os.path.join(tmp, "drill")
W.DRILL_USER = None
W.DB = os.path.join(tmp, "live.db")
W.PROOFS = os.path.join(tmp, "proofs")
W.REPO = os.path.join(tmp, "repo")
W.KEYS = os.path.join(tmp, "keys")
W.LAG_ALERT, W.REALERT, W.LOG_REALERT, W.GRACE = 900, 21600, 3600, 1800
W.SPINE, W.SPINE_LAG_ALERT = os.path.join(tmp, "spine"), 3600
W.VERIFY = os.path.join(tmp, "fake-verify")
with open(W.VERIFY, "w") as f:
    f.write('#!/bin/sh\nif grep -q FORGED "$1"; then echo "VERIFICATION FAILED: the proof is not valid"; exit 1; fi\n'
            'echo ">>> SNARK RANGE PROOF [1..1000] VERIFIED — genesis-anchored"\n')
os.chmod(W.VERIFY, 0o755)


def spine_json(data, hi=1000, ts=None):
    return json.dumps({"lo": 1, "hi": hi, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                       "ts": time.time() if ts is None else ts}).encode()


SPINE_BYTES = b"spine proof up to 1000"
SPINE_JSON = spine_json(SPINE_BYTES)
os.makedirs(W.SPINE)
with open(os.path.join(W.SPINE, "spine.bin"), "wb") as f:
    f.write(SPINE_BYTES)
with open(os.path.join(W.SPINE, "spine.json"), "wb") as f:
    f.write(SPINE_JSON)
GOOD_SPINE = {"spine-37987b85/spine_1-1000.bin": SPINE_BYTES, "spine-37987b85/spine_1-1000.json": SPINE_JSON}

# sponsor identities and a throwaway operator key (real gpg: a missing gpg fails the checks, never skips them)
import subprocess
GH = tempfile.mkdtemp(prefix="gw")          # short path: gpg-agent's socket path has a length limit


def g(*args, data=None):
    return subprocess.run(["gpg", "--batch", "--no-tty", "--homedir", GH, "--pinentry-mode", "loopback",
                           "--passphrase", ""] + list(args), input=data, capture_output=True)


def new_key(uid):
    g("--quick-gen-key", uid, "ed25519", "sign", "1d")
    fprs = [ln.split(":")[9] for ln in g("--with-colons", "--list-keys", uid).stdout.decode().splitlines()
            if ln.startswith("fpr")]
    fpr = fprs[0] if fprs else "0" * 40
    g("--quick-add-key", fpr, "cv25519", "encr", "1d")
    pub = os.path.join(tmp, fpr[-8:] + ".pub.asc")
    with open(pub, "wb") as f:
        f.write(g("--armor", "--export", fpr).stdout)
    return fpr, pub


FPR, PUB = new_key("hazync backup test <backup@test.invalid>")
FPR2, PUB2 = new_key("someone else <other@test.invalid>")
W.SPONSOR_IDENTITIES = os.path.join(tmp, "identities")
os.makedirs(os.path.join(W.SPONSOR_IDENTITIES, "trial"))
HOURS_AGO = time.time() - 3 * 3600
for rel, body in (("trial/key.hex", "ab" * 32), ("trial/handle", "SPONSOR: Hazync trial\n")):
    p = os.path.join(W.SPONSOR_IDENTITIES, rel)
    with open(p, "w") as f:
        f.write(body)
    os.utime(p, (HOURS_AGO, HOURS_AGO))
W.SPONSOR_PUBKEY, W.SPONSOR_RECIPIENT, W.KEYS_GRACE, W.KEY_EXPIRY_WARN_DAYS = PUB, FPR, 7200, 0
ID_TAR, _, _kname = M.identities_object(W.SPONSOR_IDENTITIES)
KEYS_NAME = "sponsor-keys/" + _kname


def encrypt_to(fpr):
    return g("--trust-model", "always", "--recipient", fpr, "--encrypt", data=ID_TAR).stdout


GOOD_KEYS = {KEYS_NAME: encrypt_to(FPR)}
os.makedirs(W.PROOFS)
os.makedirs(os.path.join(W.REPO, "reproduce"))
with open(os.path.join(W.REPO, "reproduce", "METHOD_ID"), "w") as f:
    f.write("37987b85" + "0" * 56 + "\n")
with open(W.KEYS, "w") as f:
    f.write("kid secret https://example.invalid\n")

if CONTROL:
    W.watch_problems = lambda run, now: {}
    W.proofs_section = lambda now, client=None, mirror=None: (True, "Proofs in R2: ok")
    W.restore_drill = lambda run, now: (True, "Restore drill: ok")
    W.mirror_section = lambda run: (True, "Hourly mirror: ok")
    W.spine_section = lambda now, client=None, mirror=None: (True, "Spine in R2: ok")
    W.keys_section = lambda now, client=None, mirror=None: (True, "Sponsor keys in R2: ok")
    print("CONTROL: the watcher sees no problems and every summary check says ok -- the checks below MUST fail")

NOW = time.time()
c = sqlite3.connect(W.DB)
c.execute("CREATE TABLE submissions(ts REAL)")
c.execute("CREATE TABLE vranges(id TEXT)")
c.executemany("INSERT INTO submissions VALUES (?)", [(NOW - i,) for i in range(50)])
c.executemany("INSERT INTO vranges VALUES (?)", [(str(i),) for i in range(40)])
c.commit()
c.close()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class Box:
    def __init__(self):
        self.active, self.lag, self.ltx_rc, self.journal, self.drill = "active", 3, 0, [], "good"
        self.mirror_result = "success"
        self.mirror_log = ("[offsite-proofs] done: 12 uploaded, 0 failed, 0.00 GB in 0.1 min\n"
                           "[offsite-proofs] done: 3 uploaded, 0 failed, 0.00 GB in 0.1 min\n")
        self.now = NOW

    def run(self, cmd, timeout=600):
        if cmd[:2] == ["systemctl", "is-active"]:
            return (0 if self.active == "active" else 3), self.active + "\n"
        if cmd[:2] == ["litestream", "ltx"]:
            if self.ltx_rc:
                return self.ltx_rc, "level=ERROR msg=\"list\" error=\"AccessDenied: Access Denied\"\n"
            return 0, ("level  min_txid          max_txid          size   created\n"
                       f"0      00000000000000c3  00000000000000c3  23506  {iso(self.now - self.lag - 30)}\n"
                       f"1      00000000000000c4  00000000000000c7  5744   {iso(self.now - self.lag)}\n")
        if cmd[0] == "journalctl" and "litestream" in cmd:
            if any(a.startswith("--cursor-file=") for a in cmd):
                out, self.journal = "\n".join(self.journal), []
                return 0, out
            return 0, ""
        if cmd[:2] == ["systemctl", "show"]:
            return 0, f"Result={self.mirror_result}\nExecMainExitTimestamp=Tue 2026-09-15 06:23:40 UTC\n"
        if cmd[0] == "journalctl":
            return 0, self.mirror_log
        if "restore" in cmd:
            out = cmd[cmd.index("-o") + 1]
            if self.drill == "good":
                shutil.copy(W.DB, out)
                return 0, ""
            if self.drill == "garbage":
                with open(out, "wb") as f:
                    f.write(b"this is not a sqlite database " * 200)
                return 0, ""
            return 1, "error: cannot restore: no snapshots available\n"
        raise AssertionError(f"unexpected command {cmd}")


sent = []


def send(title, body, priority="high", tags=""):
    sent.append((title, priority, body))


fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    fails += 0 if cond else 1


box = Box()


def tick(t):
    box.now = t
    W.watch(box.run, send, t)


# 1. healthy
tick(NOW)
check(sent == [], f"a healthy box sends nothing (sent {len(sent)})")

# 2. Litestream stops, repeats only after the window, recovers once
box.active = "inactive"
tick(NOW + 600)
check(len(sent) == 1 and sent[0][1] == "high" and "not running" in sent[0][0] and "NOT being copied" in sent[0][2],
      f"Litestream stopping pushes one high-priority alert ({[s[0] for s in sent]})")
tick(NOW + 1200)
check(len(sent) == 1, "...not again ten minutes later")
tick(NOW + 600 + W.REALERT)
check(len(sent) == 2 and "Ongoing for" in sent[-1][2], "...but again after OFFSITE_REALERT_SECS, saying how long")
box.active = "active"
tick(NOW + 1200 + W.REALERT)
check(len(sent) == 3 and sent[-1][0].startswith("Hazync backups: RECOVERED") and sent[-1][1] == "low",
      "running again sends one low-priority RECOVERED")
tick(NOW + 1800 + W.REALERT)
check(len(sent) == 3, "...and then stays quiet")

# 3. a stale ledger copy, and an R2 listing that fails
sent.clear()
T = NOW + 100000
box.lag = 20 * 60
tick(T)
check(len(sent) == 1 and "stale" in sent[0][0] and "20 min old" in sent[0][2] and sent[0][1] == "high",
      f"a ledger copy 20 min behind alerts ({[s[0] for s in sent]})")
box.lag = 3
tick(T + 600)
check(len(sent) == 2 and "RECOVERED" in sent[-1][0], "...and recovers when it catches up")
box.ltx_rc = 1
tick(T + 1200)
check(len(sent) == 3 and "cannot list" in sent[-1][0] and "AccessDenied" in sent[-1][2],
      "R2 refusing the listing alerts, with the error")
box.ltx_rc = 0
tick(T + 1800)

# 4. log warnings: INFO ignored, WARN pushed at default priority, a burst held then sent
sent.clear()
box.journal = ['time=x level=INFO msg="compaction complete"', 'time=x level=WARN msg="upload retry" error=timeout']
tick(T + 2400)
check(len(sent) == 1 and sent[0][1] == "default" and "upload retry" in sent[0][2] and "compaction" not in sent[0][2],
      f"a Litestream WARN line pushes at default priority, INFO lines are left out ({[s[0] for s in sent]})")
box.journal = ['time=y level=ERROR msg="second problem"']
tick(T + 3000)
check(len(sent) == 1, "a second warning inside the hour is held, not pushed")
tick(T + 2400 + W.LOG_REALERT + 60)
check(len(sent) == 2 and "second problem" in sent[-1][2], "...and is sent once the hour has passed, not dropped")

# 5. the daily summary
old = time.time() - 7200
for n in ("proof_1.bin", "proof_2.bin"):
    p = os.path.join(W.PROOFS, n)
    with open(p, "wb") as f:
        f.write(b"receipt " + n.encode())
    os.utime(p, (old, old))


class FakeS3:
    def __init__(self, names, spine=None, keys=None):
        self.data = dict(GOOD_SPINE if spine is None else spine)
        self.data.update(GOOD_KEYS if keys is None else keys)
        self.objects = {f"proofs-37987b85/{n}": len(b"receipt " + n.encode()) for n in names}
        self.objects.update({k: len(v) for k, v in self.data.items()})

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.data[Key])}

    def get_paginator(self, _):
        s3 = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": v} for k, v in s3.objects.items() if k.startswith(Prefix)]}
        return P()


def daily(s3):
    sent.clear()
    now = time.time()
    box.now = now
    W.summary(box.run, send, now=now, client=s3, mirror=M)
    return sent[-1] if sent else ("", "", "")


t, prio, body = daily(FakeS3(["proof_1.bin", "proof_2.bin"]))
check(t == "Hazync backups: all good" and prio == "low", f"everything backed up: 'all good' at low priority ({t}, {prio})")
check("Proofs in R2: 2 of 2" in body and "15 proofs uploaded in 24 h" in body and "Restore drill: ok" in body
      and "integrity ok" in body and "50 submissions" in body, "...and the body carries the numbers")
t, prio, body = daily(FakeS3(["proof_1.bin"]))
check(prio == "high" and "1 problem" in t and "MISSING from R2" in body and "proof_2.bin" in body,
      f"a receipt missing from R2 makes it a high-priority problem naming it ({t})")
box.drill = "garbage"
t, prio, body = daily(FakeS3(["proof_1.bin", "proof_2.bin"]))
check(prio == "high" and "not a usable ledger" in body, f"a restore that yields garbage is a problem ({t})")
box.drill = "fail"
t, prio, body = daily(FakeS3(["proof_1.bin", "proof_2.bin"]))
check(prio == "high" and "exited 1" in body and "no snapshots" in body, "a failed restore is a problem, with the error")
box.drill, box.mirror_result = "good", "exit-code"
t, prio, body = daily(FakeS3(["proof_1.bin", "proof_2.bin"]))
check(prio == "high" and "exit-code" in body, "a failed hourly mirror is a problem")
check(not os.path.exists(W.DRILL_DIR), "the restore drill leaves no copy of the ledger behind")

# 6. the shipped grace outlasts a whole hourly mirror cycle (2026-09-15). `copy` skips receipts younger
#    than 120 s, so one written just before or during a run is uploaded by the NEXT run, up to an hour
#    later. At 1800 s the 07:00 UTC summary called 68 such receipts missing; every one was uploaded at :23.
saved = os.environ.pop("OFFSITE_PROOF_GRACE_SECS", None)
DEFAULT = load("offsite_watch_default", os.path.join(HERE, "deploy", "hazync-offsite-watch.py")).GRACE
if saved is not None:
    os.environ["OFFSITE_PROOF_GRACE_SECS"] = saved
HOURLY_CYCLE = 3600 + 120 + 120          # timer period + RandomizedDelaySec + copy's --min-age default
check(DEFAULT > HOURLY_CYCLE, f"the default grace ({DEFAULT} s) is longer than one hourly mirror cycle ({HOURLY_CYCLE} s)")
W.GRACE, box.mirror_result = DEFAULT, "success"
for n, age in (("proof_3.bin", 40 * 60), ("proof_4.bin", DEFAULT + 3600)):
    p = os.path.join(W.PROOFS, n)
    with open(p, "wb") as f:
        f.write(b"receipt " + n.encode())
    os.utime(p, (time.time() - age, time.time() - age))
t, prio, body = daily(FakeS3(["proof_1.bin", "proof_2.bin", "proof_4.bin"]))
check(t == "Hazync backups: all good" and "go in the next hourly run" in body,
      f"a receipt 40 min old that the next hourly run will upload is not called missing ({t})")
t, prio, body = daily(FakeS3(["proof_1.bin", "proof_2.bin", "proof_3.bin"]))
check(prio == "high" and "MISSING from R2" in body and "proof_4.bin" in body,
      f"...but one that has missed a whole cycle still is ({t})")

# 7. the spine copy (2026-09-15: the spine had no copy off the box). The summary downloads the newest
#    copy, checks it against its own sha256, verifies it, and compares it with the live spine.
ALL = ["proof_1.bin", "proof_2.bin", "proof_3.bin", "proof_4.bin"]
t, prio, body = daily(FakeS3(ALL))
check(t == "Hazync backups: all good" and "Spine in R2: [1..1,000] of live [1..1,000]" in body and "VERIFIED" in body,
      f"a verified, current spine copy is all good, and the body says so ({t})")
t, prio, body = daily(FakeS3(ALL, spine={}))
check(prio == "high" and "NO copy" in body, f"no spine copy in R2 is a high-priority problem ({t})")
with open(os.path.join(W.SPINE, "spine.json"), "wb") as f:
    f.write(spine_json(SPINE_BYTES, hi=1500, ts=time.time() + 2 * 3600))
t, prio, body = daily(FakeS3(ALL))
check(prio == "high" and "behind the live spine" in body, f"a copy two hours behind the live spine is a problem ({t})")
with open(os.path.join(W.SPINE, "spine.json"), "wb") as f:
    f.write(SPINE_JSON)
FORGED = b"FORGED spine up to 1000"
t, prio, body = daily(FakeS3(ALL, spine={"spine-37987b85/spine_1-1000.bin": FORGED,
                                         "spine-37987b85/spine_1-1000.json": spine_json(FORGED)}))
check(prio == "high" and "hazync-verify rejected it" in body,
      f"a copy that matches its own sha256 but fails hazync-verify is a problem ({t})")
t, prio, body = daily(FakeS3(ALL, spine={"spine-37987b85/spine_1-1000.bin": b"truncated",
                                         "spine-37987b85/spine_1-1000.json": SPINE_JSON}))
check(prio == "high" and "do not match the sha256" in body, f"a copy whose bytes disagree with its sha256 is a problem ({t})")
real_verify, W.VERIFY = W.VERIFY, os.path.join(tmp, "no-such-verify")
t, prio, body = daily(FakeS3(ALL))
check(prio == "high" and "cannot verify it" in body, f"no hazync-verify on the box is a problem, not a silent pass ({t})")
W.VERIFY = real_verify

# 8. the sponsor keys: the current identities have a copy in R2, encrypted to the pinned operator key
t, prio, body = daily(FakeS3(ALL))
check(t == "Hazync backups: all good" and "Sponsor keys in R2: 2 identity file(s), current copy in R2" in body,
      f"a current copy encrypted to the operator's key is all good ({t})")
t, prio, body = daily(FakeS3(ALL, keys={}))
check(prio == "high" and "have no copy in R2" in body, f"identities unchanged for hours with no copy in R2 are a problem ({t})")
fresh = os.path.join(W.SPONSOR_IDENTITIES, "trial", "handle")
os.utime(fresh, None)
t, prio, body = daily(FakeS3(ALL, keys={}))
check(t == "Hazync backups: all good" and "goes in the next hourly run" in body,
      f"identities changed minutes ago are waiting for the next hourly run, not missing ({t})")
os.utime(fresh, (HOURS_AGO, HOURS_AGO))
t, prio, body = daily(FakeS3(ALL, keys={KEYS_NAME: encrypt_to(FPR2)}))
check(prio == "high" and "not encrypted to" in body, f"a copy encrypted to some other key is a problem ({t})")
W.SPONSOR_PUBKEY = PUB2
t, prio, body = daily(FakeS3(ALL))
check(prio == "high" and "is not in" in body, f"a public key file that is not the pinned fingerprint is a problem ({t})")
W.SPONSOR_PUBKEY, W.KEY_EXPIRY_WARN_DAYS = PUB, 60
t, prio, body = daily(FakeS3(ALL))
check(prio == "high" and "expires in" in body, f"an encryption key expiring within 60 days is a problem ({t})")
W.KEY_EXPIRY_WARN_DAYS = 0
subprocess.run(["gpgconf", "--homedir", GH, "--kill", "all"], capture_output=True)
shutil.rmtree(GH, ignore_errors=True)

print(f"{'CONTROL: ' if CONTROL else ''}{fails} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)

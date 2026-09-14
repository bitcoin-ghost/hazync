#!/usr/bin/env python3
"""
Handles beginning "SPONSOR" belong to the sponsor proving bot's registered keys.

WHY THIS EXISTS. The bot submits a sponsorship's blocks as "SPONSOR: <sponsor name>". Without a rule any
worker could take that handle and put a fake sponsor on the board, or put a real sponsor's name on work
they never paid for. So the coordinator refuses a handle whose folded form starts "sponsor" unless its key
is in `sponsor_keys`, everywhere a handle is written: submit, spine, rotate and claim.

WHAT THIS DOES NOT COVER: look-alike letters from other scripts (a Cyrillic о is not folded), and whether a
registered key uses the name of its own sponsorship (the bot's job, test_sponsor_bot.py).

Usage:
  python3 test_sponsor_identity.py            # assertions; exit 0 on success
  python3 test_sponsor_identity.py --control  # the old handle rules; these tests MUST fail
"""
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv

os.environ["COORD_DB"] = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization as ser
except ImportError:
    print("SKIP: `cryptography` not installed -- rotation signatures cannot be exercised")
    sys.exit(0)

server.init_db()
server.witness_available = lambda h: True
server.provable_tip = lambda: 1000
if CONTROL:
    server.handle_refused = lambda handle, pubkey: server.handle_reserved(handle)
    server.handle_cap = lambda pubkey: server.MAX_HANDLE
    print("CONTROL: the old handle rules -- the checks below MUST fail")

RESERVED = "that handle is reserved — please pick another"
fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


class Key:
    def __init__(self):
        self.sk = Ed25519PrivateKey.generate()
        self.pub = self.sk.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()

    def sign(self, msg):
        return self.sk.sign(msg).hex()


def register(k, sid, handle):
    c = server.db()
    c.execute("INSERT INTO sponsor_keys(pubkey, sponsorship_id, handle, created_at) VALUES(?,?,?,?)",
              (k.pub, sid, handle, time.time()))
    c.commit()
    c.close()


def contributor_handle(pub):
    c = server.db()
    r = c.execute("SELECT handle FROM contributors WHERE pubkey=?", (pub,)).fetchone()
    c.close()
    return r["handle"] if r else None


def claim(k, handle, nonce):
    return server.claim({"pubkey": k.pub, "handle": handle, "nonce": nonce})


def submit(k, handle):
    # A receipt that fails later checks: only whether the HANDLE is refused is looked at.
    return server.submit({"range": "5", "pubkey": k.pub, "handle": handle, "sig": "ab" * 64, "receipt": "!!"})


def spine(k, handle):
    return server.submit_spine({"pubkey": k.pub, "sig": "cd" * 64, "handle": handle, "receipt": "!!"})


def rotate(old, new, handle):
    ts = time.time()
    msg = server.rotate_message(old.pub, new.pub, ts)
    return server.rotate({"old_pubkey": old.pub, "new_pubkey": new.pub, "sig_old": old.sign(msg),
                          "sig_new": new.sign(msg), "ts": ts, "handle": handle})


def refused(res):
    code, body = res
    return code == 400 and body.get("error") == RESERVED


print("== an unregistered key may not use a sponsor handle ==")
stranger = Key()
check(refused(submit(stranger, "SPONSOR: Alice")), "submit refuses 'SPONSOR: Alice' from an unregistered key")
check(refused(claim(stranger, "SPONSOR: Alice", "n1")), "claim refuses it too, before writing contributors")
check(contributor_handle(stranger.pub) is None, "the refused claim wrote no contributors row")
check(refused(spine(stranger, "SPONSOR: Alice")), "spine refuses it")
check(refused(rotate(Key(), stranger, "SPONSOR: Alice")), "rotate refuses it for the new key")
for i, variant in enumerate(("SPONSOR : Alice", "s.p.o.n.s.o.r alice", "Sp0nsor: Alice", "5PONSOR Alice",
                             "ＳＰＯＮＳＯＲ: Alice", "sponsorALICE", " sponsor", "Sponsorship fan")):
    check(refused(claim(stranger, variant, f"v{i}")), f"a variant that folds to 'sponsor...' is refused: {variant!r}")
for i, fine in enumerate(("Response", "ghost:ab12cd", "spons", "tester", "My sponsor")):
    code, body = claim(Key(), fine, f"f{i}")
    check(not refused((code, body)), f"an ordinary handle is still allowed: {fine!r} ({code})")
check(refused(claim(Key(), "hazync", "h1")), "the existing reserved list still applies")

print("== a registered key may ==")
bot = Key()
name40 = "N" * 40
register(bot, 7, "SPONSOR: " + name40)
code, body = submit(bot, "SPONSOR: Alice")
check(not refused((code, body)), f"submit lets a registered key through the handle check ({code} {body.get('error')})")
code, body = spine(bot, "SPONSOR: Alice")
check(not refused((code, body)), f"spine does too ({code} {body.get('error')})")
code, body = claim(bot, "SPONSOR: " + name40, "b1")
check(code == 200, f"claim accepts it ({code} {body})")
got = contributor_handle(bot.pub)
check(got == "SPONSOR: " + name40 and len(got) == 49, f"a 40-character sponsor name is kept whole at 49 ({got and len(got)})")
new_bot = Key()
register(new_bot, 8, "SPONSOR: Bob")
code, body = rotate(Key(), new_bot, "SPONSOR: Bob")
check(code == 200, f"rotate to a registered key with its sponsor handle is allowed ({code} {body})")

print("== ordinary handles keep their cap ==")
k = Key()
code, _ = claim(k, "y" * 60, "o1")
got = contributor_handle(k.pub)
check(code == 200 and got == "y" * 48, f"an ordinary handle is still capped at 48 ({got and len(got)})")
check(server.MAX_HANDLE == 48 and server.handle_cap(stranger.pub) == 48 and server.handle_cap(bot.pub) == 49,
      "the cap is 48, and 49 only for a registered sponsor key")

print(f"{'CONTROL: ' if CONTROL else ''}{len(fails)} failure(s)")
if CONTROL:
    sys.exit(0 if fails else 1)
sys.exit(1 if fails else 0)

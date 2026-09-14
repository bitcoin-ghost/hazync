#!/usr/bin/env python3
"""
Tests for CLAIM_OPEN_MAX: one key holds at most a few live claims at once.

WHY THIS EXISTS. Measured on the live board 2026-09-14: `ghost:dda215` held 26 live claims (34 at one look,
60 taken in its busiest ten minutes). It never beat and never submitted, and re-claimed in bursts as
CLAIM_GRACE (#296) released them. Another contributor's worker on the same IP proved 9,116 of the 9,133
blocks it had claimed, and the frontier waited more than an hour behind one of them (67,532). The most any
proving key held at once was 1: a worker proves one block at a time.

What is counted is LIVE claims, by the same rule that decides a block is held (LIVE_CLAIM_SQL). A proven
claim, a never-beaten claim past the grace, and a claim whose beats stopped past CLAIM_TTL all free their
slot; a slow prover that beats keeps its blocks. A retry of a claim the key already made (#268) is never
refused.

WHAT THIS DOES NOT COVER: no HTTP, no proving. claim() is called the way the handler calls it.

Usage:
  python3 test_claim_cap.py            # assertions; exit 0 on success
  python3 test_claim_cap.py --control  # no cap (CLAIM_OPEN_MAX=0); tests MUST fail
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
# Pinned, and every scenario below is written against these literals, not the live constants: a control that
# moves the scenarios along with the constant it changes cannot fail (see test_claim_grace.py).
CAP, GRACE, TTL = 4, 600, 3600
os.environ["CLAIM_OPEN_MAX"] = "0" if CONTROL else str(CAP)
os.environ["CLAIM_GRACE"] = str(GRACE)
os.environ["CLAIM_TTL"] = str(TTL)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()
server.provable_tip = lambda: 1_000
server.witness_available = lambda h: True

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def claim(pk, nonce=None):
    body = {"pubkey": pk, "handle": "w" + pk[:4]}
    if nonce:
        body["nonce"] = nonce
    return server.claim(body)


def q(sql, args=()):
    c = server.db()
    rows = c.execute(sql, args).fetchall()
    c.commit()
    c.close()
    return rows


def claimed(pk):
    return [r["id"] for r in q("SELECT id FROM ranges WHERE assignee=? AND status='claimed' ORDER BY lo", (pk,))]


AA, BB, CC, DD, EE = ("aa" * 32, "bb" * 32, "cc" * 32, "dd" * 32, "ee" * 32)
NOW = time.time()

print(f"== a key gets {CAP} claims, then is refused ==")
got = [claim(AA) for _ in range(CAP)]
check(all(code == 200 for code, _ in got) and len({r["range"] for _, r in got}) == CAP,
      f"a key gets {CAP} claims, each a different block ({[r.get('range') for _, r in got]})")
code, r = claim(AA)
check(code == 429 and r.get("open_claims") == CAP and r.get("max") == CAP and "at most" in (r.get("error") or ""),
      f"claim number {CAP + 1} from the same key is refused with 429, and says why ({code} {r})")
check(len(claimed(AA)) == CAP, f"and no block is written for it ({len(claimed(AA))} claimed rows)")
code, r = claim(BB)
check(code == 200, f"the cap is per key: another key still gets a block ({code} {r})")

print("== a retry of a claim the key already made is never refused ==")
for _ in range(CAP - 1):
    claim(CC)
code1, first = claim(CC, nonce="retry-me")
code2, again = claim(CC, nonce="retry-me")
check(code1 == 200 and code2 == 200 and again.get("range") == first.get("range"),
      f"at the cap, a retry with the same nonce gets the block it was already given ({code2} {again})")

print("== a slot frees when a claim is proven or lapses ==")
q("UPDATE ranges SET status='verified', verified_at=? WHERE id=?", (NOW, claimed(AA)[0]))
code, r = claim(AA)
check(code == 200, f"once one of its blocks is proven, the key may claim again ({code})")
q("UPDATE ranges SET claimed_at=? WHERE id=?", (NOW - GRACE - 60, claimed(AA)[0]))
code, r = claim(AA)
check(code == 200, f"a never-beaten claim past the {GRACE} s grace no longer counts ({code})")
code, r = claim(AA)
check(code == 429, f"and the key is back at the cap ({code})")

print("== a slow prover that beats keeps its blocks, and its slots ==")
for _ in range(CAP):
    claim(DD)
q("UPDATE ranges SET claimed_at=?, last_beat=? WHERE assignee=? AND status='claimed'", (NOW - GRACE - 900, NOW - 30, DD))
code, r = claim(DD)
check(code == 429, f"{CAP} claims older than the grace but beating 30 s ago still count ({code})")
q("UPDATE ranges SET claimed_at=?, last_beat=? WHERE id=?", (NOW - TTL - 300, NOW - TTL - 60, claimed(DD)[0]))
code, r = claim(DD)
check(code == 200, f"a claim whose beats stopped past CLAIM_TTL frees its slot ({code})")

print("== the live shape: a key holding 26 never-beaten claims ==")
c = server.db()
for i, blk in enumerate(range(900, 926)):
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at) VALUES(?,?,?,'claimed',?,?,?)",
              (str(blk), blk, blk, EE, "ghost:ee", NOW - 60 - i))
c.commit()
c.close()
code, r = claim(EE)
check(code == 429 and r.get("open_claims") == 26, f"it gets no more blocks ({code} {r.get('open_claims')})")
code, r = claim(BB)
check(code == 200, f"while every other key still claims ({code})")
q("UPDATE ranges SET claimed_at=? WHERE assignee=?", (NOW - GRACE - 120, EE))
code, r = claim(EE)
check(code == 200, f"once its claims pass the grace it may claim again, up to the cap ({code})")

if not CONTROL:
    print("== CLAIM_OPEN_MAX=0 means no limit ==")
    server.CLAIM_OPEN_MAX = 0
    codes = [claim(AA)[0] for _ in range(CAP + 2)]
    server.CLAIM_OPEN_MAX = CAP
    check(all(code == 200 for code in codes), f"with the cap set to 0 a key claims freely ({codes})")

if CONTROL:
    if fails:
        print(f"CONTROL OK — no cap, and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the cap was off and every test still passed.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print(f"one key holds at most {CAP} live claims; a retry, a proof or a lapse frees its place.")

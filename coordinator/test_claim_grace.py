#!/usr/bin/env python3
"""
Tests for CLAIM_GRACE: a claim nobody is working on is released in minutes, not an hour (#296).

WHY THIS EXISTS. Measured on the live board 2026-09-13: 237 blocks held by two contributors who were
not proving them, while the most productive prover on the board held TWO claims — because a healthy
claim turns over in seconds. `ghost:dda215` held 58-61 with zero heartbeats, zero submissions ever,
and zero vranges rows: it claimed blocks it never proved, and others proved them free-running.

Liveness is `COALESCE(last_beat, claimed_at)`, which is right — workers predating the beat send none,
and falling back to claimed_at keeps them working (#251). But it gives a claim that has NEVER beaten
the full CLAIM_TTL, identical to one being actively proved. The timeout could not tell "working" from
"gone".

⚠ THE TEST IS PROGRESS, NOT SPEED, and that is the whole design. A slow prover beats — the beat is
progress-gated and segment 1 lands in about 4 s even on a 1,012-segment block — so it keeps the full
hour however long it takes. Only a claim with no progress at all is released early. Getting this
backwards would punish exactly the careful, slow workers this project wants.

⚠ RELEASING A CLAIM CANCELS NOTHING. Submission is free-running, so a worker that finishes after its
claim lapsed still submits successfully. The cost of being wrong is duplicate work, not lost work.

WHAT THIS DOES NOT COVER: no proving, no GPU. Rows are written the way the handlers write them.

Usage:
  python3 test_claim_grace.py            # assertions; exit 0 on success
  python3 test_claim_grace.py --control  # restore the old "an hour for everyone"; tests MUST fail
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
# ⛔ PIN THE GRACE, and build every age below from this literal rather than from server.CLAIM_GRACE.
# The first version of this file derived the ages from the live constant, so the control — which
# changes that constant — moved the scenarios with it: the rows landed past CLAIM_TTL and were
# released by the ORDINARY timeout, and the control passed for a reason that had nothing to do with
# the grace. A control that cannot fail is worse than no control.
REF = 600
os.environ["CLAIM_GRACE"] = "3600" if CONTROL else str(REF)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  — import-safe; the HTTP server only starts under __main__

server.init_db()

fails = []
def check(ok, what):
    if ok:
        print(f"  ok   {what}")
    else:
        print(f"  FAIL {what}")
        fails.append(what)

AA, BB = "aa" * 32, "bb" * 32
NOW = time.time()

def claim_row(block, pk, age_s, beat_age_s=None):
    """A claimed range, claimed `age_s` ago, optionally last beating `beat_age_s` ago."""
    c = server.db()
    c.execute("INSERT OR REPLACE INTO ranges(id,lo,hi,status,assignee,handle,claimed_at,last_beat)"
              " VALUES(?,?,?,'claimed',?,?,?,?)",
              (str(block), block, block, pk, "w", NOW - age_s,
               None if beat_age_s is None else NOW - beat_age_s))
    c.commit(); c.close()

def held():
    c = server.db()
    _, h = server.coverage_and_held(c, NOW)
    c.close()
    return h

G = REF                      # the grace the scenarios are written against, NOT the live value
T = server.CLAIM_TTL
print(f"  (scenarios written against {G}s; live CLAIM_GRACE={server.CLAIM_GRACE}s, CLAIM_TTL={T}s)")

# 1. Never beaten, older than the grace -> RELEASED. This is the whole point.
claim_row(1000, AA, age_s=G + 60)
check(1000 not in held(),
      f"a claim with NO heartbeat, {G + 60}s old, is released (grace {G}s)")

# 2. Never beaten but still inside the grace -> HELD. A worker fetching a witness on a slow link has
#    not failed; it simply has not started yet.
claim_row(1001, AA, age_s=max(1, G // 2))
check(1001 in held(), "a fresh claim with no heartbeat yet is still held — it may be starting up")

# 3. ⛔ BEATEN keeps the FULL TTL, however old. The test is progress, not speed: a prove that takes an
#    hour and beats throughout must not lose its block to a grace meant for absent workers.
claim_row(1002, BB, age_s=T - 120, beat_age_s=30)
check(1002 in held(),
      f"a claim {T - 120}s old that IS beating keeps its block — slow is not the same as absent")

# 4. Beaten, but the beats stopped long ago -> released by the ordinary TTL, as before.
claim_row(1003, BB, age_s=T + 600, beat_age_s=T + 120)
check(1003 not in held(), "a claim whose beats stopped past CLAIM_TTL is released as it always was")

# 5. A claim that beat ONCE and then went quiet, older than the grace, still gets the full TTL. The
#    grace deliberately keys on "has it EVER beaten", not "has it beaten recently" — one beat is
#    evidence a real prove started, and cutting it short at 10 minutes would kill long proves.
claim_row(1004, BB, age_s=G + 300, beat_age_s=G + 240)
check(1004 in held(), "one heartbeat is enough to earn the full TTL, even if it was a while ago")

# 6. The hard ceiling is untouched.
claim_row(1005, BB, age_s=server.CLAIM_MAX + 60, beat_age_s=10)
check(1005 not in held(), "CLAIM_MAX still releases a wedged-but-beating claim")

# 7. The live shape that motivated this: many never-beaten claims by one contributor.
for i, blk in enumerate(range(2000, 2050)):
    claim_row(blk, AA, age_s=G + 100 + i)
h = held()
stuck = [b for b in range(2000, 2050) if b in h]
check(not stuck, f"50 never-beaten claims by one worker are all released (still held: {len(stuck)})")

# Sponsor holds keep blocks out of the same claim path this file tests, so they run from here: the two CI
# steps already on this file cover them, with no workflow change (the coord token has no `workflow` scope).
import subprocess  # noqa: E402
_holds = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sponsor_holds.py")]
                        + (["--control"] if CONTROL else []))
if not CONTROL:
    check(_holds.returncode == 0, "sponsor holds keep blocks from normal workers (test_sponsor_holds.py)")
# An unpaid invoice holds blocks through this claim path too, so the payments test runs from here (and its control).
_pay = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_sponsor_payments.py")]
                      + (["--control"] if CONTROL else []))
if not CONTROL:
    check(_pay.returncode == 0, "BTCPay invoices hold their blocks and settle into paid sponsorships (test_sponsor_payments.py)")
elif _pay.returncode != 0:
    print("CONTROL FAILED — test_sponsor_payments.py --control passed with payments disconnected.")
    sys.exit(1)
# The per-key claim cap uses the same liveness as the grace, so it runs from here too (and its control).
_cap = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_claim_cap.py")]
                      + (["--control"] if CONTROL else []))
if not CONTROL:
    check(_cap.returncode == 0, "one key holds at most CLAIM_OPEN_MAX live claims (test_claim_cap.py)")
elif _cap.returncode != 0:
    print("CONTROL FAILED — test_claim_cap.py --control passed with the cap off.")
    sys.exit(1)
# So does the re-take wait: a key does not get back a block its own never-beaten claim let lapse.
_retake = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_claim_retake.py")]
                         + (["--control"] if CONTROL else []))
if not CONTROL:
    check(_retake.returncode == 0, "a key does not re-take a block it let lapse unworked (test_claim_retake.py)")
elif _retake.returncode != 0:
    print("CONTROL FAILED — test_claim_retake.py --control passed with the wait off.")
    sys.exit(1)
# And signed claims (#310): unsigned claims under a key cannot use up its cap or its re-take wait.
_signed = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_claim_signed.py")]
                         + (["--control"] if CONTROL else []))
if not CONTROL:
    check(_signed.returncode == 0, "only a signed claim speaks for its key (test_claim_signed.py)")
elif _signed.returncode != 0:
    print("CONTROL FAILED — test_claim_signed.py --control passed with every claim unsigned.")
    sys.exit(1)

if CONTROL:
    if fails:
        print(f"CONTROL OK — grace set to CLAIM_TTL and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the grace was disabled and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("a claim nobody is working on is released in minutes; one that is beating keeps its hour.")

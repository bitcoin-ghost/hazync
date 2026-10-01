#!/usr/bin/env python3
"""A pod's own fetch from /api/witness is measured, and a non-transfer is not read as fast (#598).

⛔ WHY. `/api/witness/<h>` already serves the tip bundle — measured 2026-10-01, BYTE-IDENTICAL to
the one tip hour 5 proved (sha256 `0c54f7ef…` on both sides), in **1.38 s** against **25.0 s** for
the two-leg ssh path the run actually used. So an aggregate that fetched its own bundle would take
the orchestrator's home uplink out of the path and staging would stop costing 25–73 s.

⚠ BUT 1.38 s WAS TO A LAPTOP. Nobody has timed the POD → API leg, and this project has been wrong
before about a transfer it reasoned about instead of timing: lever 2 was predicted to save ~36 s
from a plausible model and measured NEGATIVE. ⇒ #598 step 2 is gated on this one number, and the
next run can take it for free from a card that is rented and not yet proving.

⛔ THE FAILURE THIS GUARDS is a probe that reports a non-transfer as a fast one. A 200 with zero
bytes, a 404, a connection refused — each would look like "the pod fetched it instantly" to a naive
reader of `time_total`, and would argue for a rewrite of the staging path on no evidence at all.

    python3 test_pod_api_probe.py             # a real transfer measured; anything else UNMEASURED
    python3 test_pod_api_probe.py --control   # trust time_total alone — a 0-byte 200 reads as fast
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_smoke  # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class Card:
    def __init__(self, cid="w7"):
        self.cid = cid


class SSH:
    """Returns whatever curl -w would have printed, without going near the network."""
    def __init__(self, out):
        self.out = out
        self.bodies = []

    def run(self, card, body, **kw):
        self.bodies.append(body)
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def probe(out):
    ssh = SSH(out)
    if CONTROL:
        # ⛔ THE NAIVE VERSION: believe time_total, whatever else the transfer did.
        for ln in str(out or "").splitlines():
            if ln.startswith("APIFETCH|"):
                p = ln.split("|")
                return float(p[3]), int(p[2]), 0.0
        return None, None, None
    return tip_smoke.probe_api_fetch(ssh, Card(), 969306)


REAL = "APIFETCH|200|26893180|1.380|19487812"

# ── 1. a real transfer is measured ──────────────────────────────────────────────────────────────
secs, nbytes, mbit = probe(REAL)
check(secs == 1.38 and nbytes == 26893180,
      f"a real 26.9 MB fetch is measured ({secs}s, {nbytes} bytes)")
if not CONTROL:
    check(mbit is not None and abs(mbit - 155.9) < 1.0,
          f"and Mbit/s comes from curl's speed_download, not from size/time ({mbit})")

# ── 2. ⛔ THE ONES THAT MATTER: a non-transfer must read as UNMEASURED, never as fast ────────────
cases = [
    ("APIFETCH|200|0|0.004|0",            "a 200 with ZERO bytes"),
    ("APIFETCH|404|34|0.120|283",          "a 404 (the height is not served)"),
    ("APIFETCH|000|0|0.000|0",             "a connection that never completed"),
    ("",                                   "no output at all"),
    ("curl: (7) Failed to connect",        "curl's error text instead of a measurement"),
]
for out, what in cases:
    s2, b2, m2 = probe(out)
    if CONTROL:
        if out.startswith("APIFETCH|200|0"):
            check(s2 is not None,
                  f"⛔ control: {what} reads as a 0.004s fetch — which would argue for rewriting "
                  f"the staging path on no evidence")
    else:
        check(s2 is None and b2 is None,
              f"{what} is UNMEASURED, not fast ({s2})")

# ── 3. an ssh failure is not a fast fetch either ─────────────────────────────────────────────────
if not CONTROL:
    s3, _, _ = tip_smoke.probe_api_fetch(SSH(RuntimeError("ssh died")), Card(), 1)
    check(s3 is None, f"an ssh exception is UNMEASURED ({s3})")

    # ── 4. the command itself: POSIX, no body kept, the right URL ───────────────────────────────
    ssh = SSH(REAL)
    tip_smoke.probe_api_fetch(ssh, Card(), 969306)
    body = ssh.bodies[0]
    check("/api/witness/969306" in body, "it asks for the height it was given")
    check("-o /dev/null" in body,
          "⚠ and discards the 30 MB body — the figure wanted is the TRANSFER, not a staged file")
    check("--max-time" in body, "with a timeout, so a dead endpoint cannot hang the run")
    check("TIMEFORMAT" not in body and "time (" not in body,
          "⛔ and no bash-isms: ssh.run wraps every body in `sh -c`, which is DASH on these pods")

# ── 5. ⛔ is it CALLED? a probe nothing invokes measures nothing ─────────────────────────────────
SRC = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
if not CONTROL:
    check(SRC.count("probe_api_fetch(ssh, order[-1]") == 1,
          "it is called on order[-1] — a WORKER, never order[0] which is the aggregate")
    check("_api_probe_done" in SRC and SRC.count("_api_probe_done = True") == 1,
          "⚠ once per run: the answer does not change block to block")
    check('"pod_api"' in SRC, "and the result lands in the stage ledger beside the two legs")
    check(SRC.index("_api_probe_done = False") < SRC.index("nonlocal _api_probe_done"),
          "the flag is initialised before the closure declares it nonlocal")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — trusting time_total must misread a 0-byte 200")
        sys.exit(1)
    print("PASS (control): time_total alone reads a non-transfer as an instant fetch")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")

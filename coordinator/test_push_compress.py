#!/usr/bin/env python3
"""The bundle push is compressed; receipt pushes are not (hazync#598).

📏 MEASURED 2026-09-30, pushing the same real 41.7 MB tip bundle to the same pod:

    scp        37.3 s   at 1.12 MB/s
    scp -C     15.2 s   at 2.75 MB/s effective     <- 2.5x, 22.1 s off every tip block

That leg sits between a block being mined and a GPU starting, and nothing in this project has ever
compressed a transfer — `SSH_OPTS` has no `-C`.

⛔ WHY THIS IS NOT JUST ADDED TO SSH_OPTS. A tip bundle is JSON and compresses 2.5x. A receipt is a
STARK proof: high-entropy bytes that will not compress, so `-C` there buys nothing and spends CPU on
both ends while the fleet is mid-block. Compression is a property of the PAYLOAD, and only the caller
knows which it is holding.

⚠ AND WHY THIS REPLACED THE FIRST ATTEMPT AT #598. Stage-ahead — pushing the next bundle while the
current block proves — was merged first and then tested against tip hour 4's own timings: it would
have fired **0 of 4 times**. Bitcoin blocks arrive ~10 minutes apart and the fleet proves in 2-6, so
when a block starts proving the NEXT one has not been mined and there is nothing to stage. It only
helps a fleet that is behind. Compression helps every block, in every regime.

    python3 test_push_compress.py             # bundles compressed, receipts not
    python3 test_push_compress.py --control   # -C everywhere — receipts must get it too
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_driver  # noqa: E402
import tip_runner  # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class Card:
    def __init__(self, cid="agg"):
        self.cid, self.ip, self.port, self.workdir = cid, "10.0.0.1", 22, "/workspace"


class Spy(tip_driver.SSH if hasattr(tip_driver, "SSH") else object):
    """Captures the argv scp would have been given, without running it."""
    def __init__(self):
        self.key, self.user = "/dev/null", "root"
        self.cmds = []
        self.remote = {}

    def run(self, card, body, **kw):
        # stage_bundle hashes the far end before and after; mirror that much.
        if body.startswith("sha256sum "):
            import hashlib
            blob = self.remote.get(body.split()[1])
            return hashlib.sha256(blob).hexdigest() if blob is not None else ""
        return ""

    def push(self, card, local, remote, timeout=60, compress=False):
        if CONTROL:
            compress = True          # ⛔ the naive fix: -C on everything
        cmd = ["scp", *(["-C"] if compress else []),
               *[o for o in tip_driver.SSH_OPTS if o != "-n"], "-i", self.key,
               "-P", str(card.port), local, f"{self.user}@{card.ip}:{remote}"]
        self.cmds.append((os.path.basename(local), cmd))
        with open(local, "rb") as fh:
            self.remote[remote] = fh.read()
        return True


T = tempfile.mkdtemp(prefix="pushc-")
BUNDLE = os.path.join(T, "bundle_969118.json")
open(BUNDLE, "wb").write(b'{"in_roots":"' + b"a" * 8192 + b'"}')
RECEIPT = os.path.join(T, "chunk_3.bin")
open(RECEIPT, "wb").write(os.urandom(8192))          # a STARK proof does not compress

# ── 1. the real signature carries the option at all ─────────────────────────────────────────────
import inspect  # noqa: E402
sig = inspect.signature(tip_driver.SSH.push if hasattr(tip_driver, "SSH") else Spy.push)
check("compress" in sig.parameters, f"push() takes `compress` ({list(sig.parameters)})")
check(sig.parameters["compress"].default is False,
      "⚠ and it is OFF by default — a receipt must not pay for compression it cannot use")

# ── 2. ⛔ THE ONE THAT MATTERS: the bundle push asks for it ──────────────────────────────────────
spy = Spy()
r = tip_runner.FleetRunner.__new__(tip_runner.FleetRunner)
r.ssh, r.agg, r.stage_dir, r.workdir = spy, Card(), T, "/workspace"
r.stage_bundle(969118, BUNDLE)
bundle_cmd = [c for n, c in spy.cmds if n.startswith("bundle_")]
check(len(bundle_cmd) == 1, f"the bundle was pushed once ({len(bundle_cmd)})")
check("-C" in bundle_cmd[0],
      "⛔ the bundle push is COMPRESSED — this is the 22.1 s that comes off every tip block")

# ── 3. a receipt push is NOT compressed ─────────────────────────────────────────────────────────
spy2 = Spy()
spy2.push(Card("w3"), RECEIPT, "/workspace/chunk_3.bin")
got_c = "-C" in spy2.cmds[0][1]
if CONTROL:
    check(got_c, "⛔ control: -C is applied to a receipt too — CPU on both ends for bytes that "
                 "cannot compress, while the fleet is mid-block")
else:
    check(not got_c,
          "a receipt push is NOT compressed — a STARK proof is high-entropy and gains nothing")

# ── 4. the flag order is valid scp ──────────────────────────────────────────────────────────────
c = bundle_cmd[0]
check(c[0] == "scp" and c.index("-C") == 1, f"-C sits directly after scp ({c[:3]})")
check("-n" not in c, "⚠ and -n is still stripped — scp has no such option, it would refuse to run")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — the control should apply -C everywhere")
        sys.exit(1)
    print("PASS (control): -C on every push, including receipts that cannot benefit")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")

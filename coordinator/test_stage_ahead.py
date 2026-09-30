#!/usr/bin/env python3
"""A bundle already on the aggregate is not pushed again, and identity is decided by HASH (#598).

📏 WHY. Staging cost 49-164 s on every tip block of tip hour 4 — 14 % of the run — BEFORE a single GPU
started. Measured 2026-09-30 over a real 41.7 MB bundle, the two legs are nothing alike:

    bridge host  -> orchestrator    4.2 s   at 9.85 MB/s
    orchestrator -> aggregate      37.3 s   at 1.12 MB/s      <- 8.8x, and it is on the clock

⛔ The original issue guessed the opposite cause: "~30 MB crosses a 0.4 MB/s uplink twice". Bundles are
41.7 MB, and the link is ASYMMETRIC — the downlink is 25x what that number assumed. Only the push is
slow, so the fix is to do it EARLY rather than to stop the orchestrator holding the bytes.

⇒ `stage_bundle` is therefore idempotent: callable while the previous block proves, and again on the
normal path, where it must cost ~nothing the second time. That second call is what this pins.

⛔ AND IDENTITY IS BY HASH, NOT SIZE OR NAME. A same-sized file is not the same file. This project has
the scar twice over: a piped two-hop transfer dropped 13 of 18 entries and exited 0, and an `scp` that
exited 0 once staged 21 of 22 chunks, leaving seg-serve panicking with nothing useful in any log.

    python3 test_stage_ahead.py             # a staged bundle is skipped; a wrong one is re-pushed
    python3 test_stage_ahead.py --control   # identity by SIZE — a corrupted bundle must slip through
"""
import hashlib
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_runner  # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def _digest(blob):
    """sha256 of the bytes — or, under --control, of the LENGTH alone: the bug being reproduced."""
    return hashlib.sha256(b"x" * len(blob) if CONTROL else blob).hexdigest()


if CONTROL:
    # ⛔ Make the LOCAL side size-based too, so the two agree and a same-sized impostor is accepted.
    tip_runner._sha256_file = lambda path, chunk=1 << 20: _digest(open(path, "rb").read())


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class Card:
    def __init__(self, cid="agg"):
        self.cid, self.ip, self.port, self.workdir = cid, "10.0.0.1", 22, "/workspace"


class FakeSSH:
    """A remote /workspace, plus a record of what was actually transferred.

    `remote` maps path -> bytes. `pushes` counts real transfers, which is the number under test:
    the whole point is that the second call moves NOTHING.
    """
    def __init__(self):
        self.remote = {}
        self.pushes = []

    def run(self, card, body, **kw):
        # Only the two shapes stage_bundle asks for.
        if body.startswith("sha256sum "):
            path = body.split()[1]
            blob = self.remote.get(path)
            if blob is None:
                return ""
            # ⚠ Under --control BOTH ends are made to compare by SIZE (see the patch below), which
            # is what "identity by size" actually means — modelling it on one side only would just
            # break every comparison rather than reproduce the bug.
            return _digest(blob)
        if body.startswith("stat -c%s "):
            path = body.split()[2]
            return str(len(self.remote.get(path, b"")))
        return ""

    def push(self, card, local, remote, **kw):
        with open(local, "rb") as fh:
            self.remote[remote] = fh.read()
        self.pushes.append((remote, len(self.remote[remote])))
        return True


def runner_with(ssh, stage_dir):
    r = tip_runner.FleetRunner.__new__(tip_runner.FleetRunner)
    r.ssh, r.agg, r.stage_dir, r.workdir = ssh, Card(), stage_dir, "/workspace"
    return r


T = tempfile.mkdtemp(prefix="stageahead-")
BUNDLE = os.path.join(T, "bundle_969118.json")
with open(BUNDLE, "wb") as fh:
    fh.write(b'{"in_roots": "' + b"a" * 4096 + b'"}')
LOCAL_SUM = hashlib.sha256(open(BUNDLE, "rb").read()).hexdigest()

ssh = FakeSSH()
r = runner_with(ssh, T)

# ── 1. a cold push happens and is verified ──────────────────────────────────────────────────────
ok, why = r.stage_bundle(969118, BUNDLE)
check(ok, f"a bundle not yet on the aggregate is pushed ({why})")
check(len(ssh.pushes) == 1, f"exactly one transfer ({len(ssh.pushes)})")
check(ssh.remote.get("/workspace/bundle_969118.json") is not None, "and it landed at the right path")

# ── 2. ⛔ THE ONE THAT MATTERS: the second call moves nothing ────────────────────────────────────
ok, why = r.stage_bundle(969118, BUNDLE)
check(ok, f"a bundle already there is accepted ({why})")
check(len(ssh.pushes) == 1,
      f"⛔ and NOTHING is transferred the second time ({len(ssh.pushes)} push(es) total) — this is "
      f"the 37.3 s that comes off the clock")
check("already" in why, f"it says so plainly ({why!r})")

# ── 3. the stage ledger records the skip as a real, zero-cost leg ────────────────────────────────
rows = [x for x in __import__("tip_stage").read(os.path.join(T, "stage_ledger.jsonl"))
        if x["leg"] == "push"]
check(len(rows) == 2, f"both calls are in the ledger ({len(rows)}) — a skip is evidence, not silence")
check(rows[1]["seconds"] == 0.0 and "staged ahead" in rows[1].get("note", ""),
      "the second is recorded at 0.0 s and named as staged ahead")

# ── 4. ⛔ A DIFFERENT BUNDLE OF THE SAME SIZE MUST BE RE-PUSHED ──────────────────────────────────
# The failure this guards: a stale or truncated file at the far end that happens to match on length.
corrupt = b'{"in_roots": "' + b"b" * 4096 + b'"}'
assert len(corrupt) == os.path.getsize(BUNDLE)
ssh.remote["/workspace/bundle_969118.json"] = corrupt
before = len(ssh.pushes)
ok, why = r.stage_bundle(969118, BUNDLE)
if CONTROL:
    check(len(ssh.pushes) == before,
          "⛔ control: identity by SIZE keeps the corrupted bundle — the aggregate would execute it")
    check(ok, "control: and reports success")
else:
    check(len(ssh.pushes) == before + 1,
          "⛔ a same-sized but DIFFERENT bundle is re-pushed — size is not identity")
    check(ok and ssh.remote["/workspace/bundle_969118.json"] != corrupt,
          "and the correct bytes end up there")

# ── 5. a push that does not land is reported, never assumed ─────────────────────────────────────
if not CONTROL:
    class Dropping(FakeSSH):
        def push(self, card, local, remote, **kw):
            self.pushes.append((remote, 0))     # scp exits 0 having moved nothing
            return True
    d = Dropping()
    ok, why = runner_with(d, T).stage_bundle(969119, BUNDLE)
    check(not ok and "did not land intact" in why,
          f"⛔ an scp that exits 0 without moving the file is caught by the far-end hash ({why[:60]})")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — the control should let the corrupted bundle through")
        sys.exit(1)
    print("PASS (control): identity by size accepts a corrupted bundle the aggregate would execute")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")

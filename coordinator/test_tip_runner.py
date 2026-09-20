#!/usr/bin/env python3
"""Tests for tip_runner.py — the concrete fleet runner (Phase 5 ⑧).

This is the layer where a mistake is invisible: every method issues a remote command and believes what
comes back. The cases below pin the places where "it returned something" is not "it worked", and the
two liveness traps that have each wrecked a run.

A fake `ssh` records commands and returns whatever the test dictates, so the whole runner is exercised
without a card.

  python3 test_tip_runner.py            # assertions; exit 0 on success
  python3 test_tip_runner.py --control  # the far-side staging check is removed; MUST fail
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_driver as td   # noqa: E402
import tip_runner as trn  # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class FakeSSH:
    """Records every command; answers from `replies` (a list of (substring, response) rules)."""

    def __init__(self, rules=(), fetch_ok=True, push_ok=True):
        self.rules, self.cmds = list(rules), []
        self.fetch_ok, self.push_ok = fetch_ok, push_ok
        self.pushed = []

    def run(self, card, body, env=None, timeout=None):
        self.cmds.append((card.cid, body))
        for sub, resp in self.rules:
            if sub in body:
                return resp
        return "OK\n"

    def fetch(self, card, remote, local):
        if not self.fetch_ok:
            return False
        open(local, "w").write("receipt")
        return True

    def push(self, card, local, remote):
        if self.push_ok:
            self.pushed.append(remote)
        return self.push_ok

    def kill_provers(self, card):
        self.cmds.append((card.cid, "KILL"))
        return True


A = td.Card("a", "10.0.0.1", 22)
AGG = td.Card("agg", "10.0.0.9", 22)
CARDS = {0: A, 1: td.Card("b", "10.0.0.2", 22)}


def runner(ssh, **kw):
    return trn.FleetRunner(ssh, AGG, stage_dir=tempfile.mkdtemp(prefix="stage_"), **kw)


# ── 1. phase 0: the clear must actually remove the things that make a pod lie ─────────────────────
ssh = FakeSSH(rules=[("LEFT:", "LEFT:0 REDIRS:0\n")])
r = runner(ssh)
out = r.clear_and_check(CARDS)
body = ssh.cmds[0][1]
check(set(out.keys()) == set(CARDS.values()), "clear_and_check is keyed by the card objects")
check(out[A] == "LEFT:0 REDIRS:0", "and reports what the pod said")
check("chunk_*.bin" in body and "re*" in body,
      "the clear removes stale chunk receipts AND reassignment directories")
check("LEFT:$LEFT REDIRS:$REDIRS" in body, "and reports what is LEFT rather than assuming success")

ssh = FakeSSH(rules=[("LEFT:", None)])          # ssh failed
r = runner(ssh)
check(r.clear_and_check(CARDS)[A] is None,
      "a pod we could not reach reports None, not a cheerful clean")

# ── 2. phase 1: a prove must survive the ssh session that starts it ───────────────────────────────
ssh = FakeSSH()
r = runner(ssh)
r.launch_all(CARDS, block="966256", chunks=2)
launch = [b for _, b in ssh.cmds if "pod-prove.sh" in b][0]
for need in ("nohup", "setsid", "< /dev/null", "disown"):
    check(need in launch, f"the launch uses {need} — without it the prove dies with the ssh session")
check("HAZYNC_LIFTX_HINT=1" in launch,
      "LIFTX_HINT is set — without it the guest dies with DeserializeUnexpectedEnd, which reads as a "
      "corrupt fixture")
check("HAZYNC_CHUNKS=2" in launch, "the chunk count is passed")

# ── 3. the aggregator does not dial itself ────────────────────────────────────────────────────────
ssh = FakeSSH()
r = runner(ssh)
r.arm_auto_attach({0: A, 1: AGG})
attached = {cid for cid, b in ssh.cmds if "autoattach.sh" in b}
check("a" in attached, "a worker card is armed to attach")
check("agg" not in attached, "the aggregator is NOT armed to dial itself — it serves")

# ── 4. staging: BOTH hops confirmed ───────────────────────────────────────────────────────────────
if CONTROL:
    # THE FAR-SIDE CHECK REMOVED: believe the push. This is the silent scp drop, reintroduced.
    trn.FleetRunner.stage_receipt = lambda self, card, chunk, reassigned=(): (
        self.ssh.fetch(card, "r", os.path.join(self.stage_dir, f"chunk_{chunk}.bin"))
        and self.ssh.push(self.agg, os.path.join(self.stage_dir, f"chunk_{chunk}.bin"), "x"))

ssh = FakeSSH(rules=[("test -s", "Y\n")])
r = runner(ssh)
check(r.stage_receipt(A, 0), "a receipt that lands and is confirmed stages")

ssh = FakeSSH(rules=[("test -s", "\n")])        # far side says it is NOT there
r = runner(ssh)
check(not r.stage_receipt(A, 0),
      "⛔ a push the far side cannot confirm is NOT staged — the silent scp drop")

ssh = FakeSSH(fetch_ok=False)
r = runner(ssh)
check(not r.stage_receipt(A, 0), "a receipt that never left the card is not staged")

# ── 5. counting: unreadable is not zero and not N ─────────────────────────────────────────────────
ssh = FakeSSH(rules=[("wc -l", "27\n")])
check(runner(ssh).staged_count() == 27, "a readable count is returned")
ssh = FakeSSH(rules=[("wc -l", None)])
check(runner(ssh).staged_count() == 0,
      "an unreadable count returns 0, which FAILS the gate — the safe direction")
ssh = FakeSSH(rules=[("wc -l", "rubbish\n")])
check(runner(ssh).staged_count() == 0, "unparseable output is not silently treated as a count")

# ── 6. aggregate liveness: the two traps ──────────────────────────────────────────────────────────
alive = "execution 19.2 s\nALIVE:1\nJOINS:joins 412/584\n"
st = runner(FakeSSH(rules=[("agg.log", alive)])).aggregate_status()
check(st["alive"], "a live aggregate is seen — `comm` truncates to hazync-host-cud and that is matched")
check(st["joins"] == "joins 412/584",
      "the join-tree progress is captured — it exists nowhere else once the pod is gone")

dead = "panicked at ...\nALIVE:0\nJOINS:\n"
st = runner(FakeSSH(rules=[("agg.log", dead)])).aggregate_status()
check(not st["alive"], "a dead aggregate reads as dead")

ver = ("VERIFIED against METHOD_ID\n"
       "digest 84e6643e531700811ee873ac5a4f5879a1424ecec813297948fee96f96ab84ff\n"
       "ALIVE:1\nJOINS:joins 584/584\n")
st = runner(FakeSSH(rules=[("agg.log", ver)])).aggregate_status()
check(st["verified"] and st["digest"].startswith("84e6643e"),
      f"a verified aggregate yields its digest ({(st['digest'] or '')[:8]})")

# ⛔ An ssh we could not make is NOT a dead aggregate. Saying "dead" here aborts a healthy run.
st = runner(FakeSSH(rules=[("agg.log", None)])).aggregate_status()
check(st["alive"] and not st["verified"] and st["unreachable"],
      "an unreachable aggregator is reported unreachable, NOT dead")

# ── 7. ⛔ /dev/tcp IS A BASH FEATURE, AND /bin/sh IS dash ──────────────────────────────────────────
# Found by the first live run. Under dash the dial test reports "cannot open /dev/tcp/...: No such
# file", so it NEVER succeeds: every worker loops its full 600 s and never attaches, even when the
# aggregate is perfectly reachable. An entire fleet looks armed and proves nothing, with no error.
# run_continuous.sh used bash here; changing it to sh silently broke attachment.
ssh = FakeSSH()
r = runner(ssh, agg_dial=58231)
r.arm_auto_attach({0: A})
aa = "\n".join(b for _, b in ssh.cmds)
check("timeout 3 bash -c" in aa, "the dial test runs under bash, not sh")
check("sh -c \"</dev/tcp" not in aa.replace('bash -c "</dev/tcp', ''),
      "no /dev/tcp test is left running under sh")
check("setsid bash ./autoattach.sh" in aa,
      "the attach script itself is run under bash — it uses bash-only parameter expansion")
check("#!/bin/bash" in aa, "and carries a bash shebang")

# ⛔ WORKERS DIAL THE PUBLISHED PORT, NOT THE BOUND ONE.
check(f"{A.ip}" not in aa or "10.0.0.9:58231" in aa,
      "workers dial the aggregator's published address")

# ── 8. ⛔ scp DOES NOT PRESERVE THE EXECUTABLE BIT ─────────────────────────────────────────────────
# Measured on 23 live cards 2026-09-20: pod-prove.sh staged by scp landed 0644, and EVERY card died with
#   setsid: failed to execute ./pod-prove.sh: Permission denied
# giving a zero-byte prove.log, an idle GPU, and a run that sat in its poll loop believing the fleet was
# merely slow. chmod is one syscall; do it rather than trust whoever staged the file.
ssh = FakeSSH()
runner(ssh).launch_all({0: A}, block="965500", chunks=1)
launch = [b for _, b in ssh.cmds if "pod-prove.sh" in b][0]
check("chmod +x /workspace/pod-prove.sh" in launch,
      "every launch chmods pod-prove.sh first — scp does not preserve the bit")
check(launch.index("chmod +x") < launch.index("setsid"),
      "and does it BEFORE trying to execute it")

# ── 9. phase 0 and phase 1 must touch the fleet IN PARALLEL ───────────────────────────────────────
# Serial, 23 cards is 23 sequential ssh round-trips before the clock starts, and one slow pod holds up
# every other. Asserted by timing a deliberately slow fake.
import threading, time as _t  # noqa: E402

class SlowSSH(FakeSSH):
    def run(self, card, body, env=None, timeout=None):
        _t.sleep(0.20)
        return super().run(card, body, env, timeout)

many = {i: td.Card(f"c{i}", f"10.0.0.{i}", 22) for i in range(12)}
sl = SlowSSH(rules=[("LEFT:", "LEFT:0 REDIRS:0\n")])
t0 = _t.time(); runner(sl).clear_and_check(many); clear_s = _t.time() - t0
check(clear_s < 12 * 0.20 * 0.5,
      f"clear_and_check fans out: 12 cards x 0.20 s took {clear_s:.2f} s, not {12*0.20:.2f} s")

sl2 = SlowSSH()
t0 = _t.time(); runner(sl2).launch_all(many, block="965500", chunks=12); launch_s = _t.time() - t0
check(launch_s < 12 * 0.20 * 0.5,
      f"launch_all fans out: {launch_s:.2f} s, not {12*0.20:.2f} s")

EXPECTED_CONTROL_FAILURES = {
    "⛔ a push the far side cannot confirm is NOT staged — the silent scp drop",
}

print()
if CONTROL:
    got = set(fails)
    if EXPECTED_CONTROL_FAILURES <= got:
        print("CONTROL OK — the far-side staging check was removed and the assertion that detects it "
              "failed, as it must:")
        for f in sorted(EXPECTED_CONTROL_FAILURES & got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — an unconfirmed push went undetected.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got):
        print(f"  should have failed and did not: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

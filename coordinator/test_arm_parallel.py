#!/usr/bin/env python3
"""Arming is ONE ssh per card, all cards at once, and a failure is not silent (hazync#584).

📏 WHY. Measured across all 21 blocks of tip hour 4, 2026-09-29, on a 38-card fleet:

    arm    1828 s   41 %   <- more than all proving and folding combined
    work   1503 s   34 %
    stage   629 s   14 %
    idle    447 s   10 %

Arming was a serial loop issuing TWO ssh round trips per worker — 37 x 2 = 74 cold handshakes to pods
on three continents, once PER BLOCK. It cost 86-96 s on every block regardless of size; on a board
block doing 5-18 s of real work that is ~85 % of the block, with 37 GPUs idle throughout.

⛔ AND A FAILURE WAS SILENT. `ssh.run` returns None on failure and both calls ignored it, so a card
that never armed simply never attached — and the symptom downstream is an aggregate sitting at
0/N segments with nothing saying why.

    python3 test_arm_parallel.py             # one call per card, concurrent, failures reported
    python3 test_arm_parallel.py --control   # the old shape: 2 calls per card, serial, silent
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_runner  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


class Card:
    def __init__(self, cid):
        self.cid = cid
        self.ip = "10.0.0.1"
    def __repr__(self):
        return self.cid


class FakeSSH:
    """Records every call, and can be told which cards fail.

    ⚠ Sleeps briefly so concurrency is observable: a serial loop takes N*delay, a parallel one takes
    about delay. Without the sleep both shapes finish instantly and the test proves nothing about
    parallelism — only about call count.
    """
    def __init__(self, delay=0.05, fail=()):
        self.calls = []
        self.delay = delay
        self.fail = set(fail)
        self.lock = threading.Lock()
        self.concurrent = 0
        self.peak = 0

    def run(self, card, body, **kw):
        with self.lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
            self.calls.append((card.cid, body))
        time.sleep(self.delay)
        with self.lock:
            self.concurrent -= 1
        return None if card.cid in self.fail else "ARMED"


class Runner(object):
    """Just enough of the runner to drive arm_auto_attach."""
    def __init__(self, ssh, cards, agg):
        self.ssh = ssh
        self.agg = agg
        self.agg_dial = 9110
        self._cards = cards


def arm(ssh, n_cards, fail=()):
    """Run the real arm_auto_attach against a fake ssh. Returns (elapsed, ssh)."""
    agg = Card("agg")
    cards = {i: Card(f"w{i}") for i in range(1, n_cards)}
    cards[0] = agg                                  # the aggregate must be skipped
    r = Runner(ssh, cards, agg)
    t0 = time.time()
    if CONTROL:
        # The shipped-before shape: two calls per card, strictly serial, result ignored.
        script = tip_runner.worker_attach_script({})
        target = f"{agg.ip}:9110"
        for chunk, card in cards.items():
            if card == agg:
                continue
            ssh.run(card, script)
            ssh.run(card, f"cd /workspace && nohup setsid bash ./autoattach.sh {target} w{chunk} "
                          f"> aa.log 2>&1 < /dev/null & disown; exit 0")
    else:
        tip_runner.FleetRunner.arm_auto_attach(r, cards)
    return time.time() - t0, ssh


N = 20

# ── 1. one ssh per card, not two ────────────────────────────────────────────────────────────────
el, ssh = arm(FakeSSH(), N)
per_card = {}
for cid, _ in ssh.calls:
    per_card[cid] = per_card.get(cid, 0) + 1
check("agg" not in per_card, "the aggregate is skipped — it serves, it does not dial itself")
if CONTROL:
    check(all(v == 2 for v in per_card.values()),
          f"control reproduces it: {N-1} cards x 2 calls = {len(ssh.calls)} ssh round trips")
else:
    check(all(v == 1 for v in per_card.values()),
          f"exactly one ssh per card ({len(ssh.calls)} calls for {len(per_card)} cards)")
    # ⛔ The merged body must still contain BOTH halves, or it arms nothing.
    body = ssh.calls[0][1]
    check("autoattach.sh <<'EOS'" in body or "cat > /workspace/autoattach.sh" in body,
          "the merged body still writes the script")
    check("nohup setsid bash ./autoattach.sh" in body, "and still launches it")
    check(body.index("cat > /workspace/autoattach.sh") < body.index("nohup setsid"),
          "⚠ in that order — launching before writing would run a stale or absent script")

# ── 2. it is concurrent ─────────────────────────────────────────────────────────────────────────
if CONTROL:
    check(ssh.peak == 1, f"control: strictly serial, peak concurrency {ssh.peak}")
    check(el > (N - 1) * 2 * 0.05 * 0.8,
          f"control: {el:.2f}s for {N-1} cards — it scales with the fleet")
else:
    check(ssh.peak > 1, f"cards are armed concurrently (peak {ssh.peak} in flight)")
    check(ssh.peak <= tip_runner.ARM_PARALLEL,
          f"⚠ and bounded by ARM_PARALLEL={tip_runner.ARM_PARALLEL}, not unbounded ({ssh.peak})")
    serial_would_be = (N - 1) * 2 * 0.05
    check(el < serial_would_be / 2,
          f"and it beats the serial shape: {el:.2f}s vs {serial_would_be:.2f}s")

# ── 3. ⛔ a card that fails to arm is NAMED, not swallowed ───────────────────────────────────────
if not CONTROL:
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        arm(FakeSSH(fail={"w3", "w7"}), N)
    out = buf.getvalue()
    check("FAILED to arm" in out, "a card that fails to arm is reported")
    check("w3" in out and "w7" in out, f"and named: {out.strip()[:80]}")
    check("2 card(s)" in out, "with a count")

# ── 4. degenerate ───────────────────────────────────────────────────────────────────────────────
if not CONTROL:
    s2 = FakeSSH()
    agg = Card("agg")
    tip_runner.FleetRunner.arm_auto_attach(Runner(s2, {0: agg}, agg), {0: agg})
    check(s2.calls == [], "a fleet of only the aggregate arms nobody and does not crash")

print()
if fails:
    print(f"FAIL: {fails}")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

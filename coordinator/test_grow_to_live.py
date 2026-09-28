#!/usr/bin/env python3
"""The growth target can be raised while a run is live, and gating overlaps so it arrives in time.

⛔ WHAT THIS EXISTS FOR. During tip hour 3 the fleet sat at 18 cards while missing the 600 s gate on
every tip block, and the operator asked for more:

    "18 cards won't cut it. Can't you increase it?"

`--grow-to` was already 28, so the answer was "it is trying". It could not go faster, and the target
could not be raised without restarting — which would have cost the clock already spent and landed on
the same capacity. Two separate faults wearing one symptom:

  1. the target was fixed at launch (hazync#548);
  2. `_tick` rented ONE pod and gated it ON THE RECRUITER THREAD — ssh, a 411 MB prover fetch, a GPU
     smoke and a reachability probe — then slept `poll_s`. Measured: about one card every four
     minutes, so 18 → 28 would have taken ~40 minutes of a 60-minute window.

Fixing only the first would have let the operator ask for 40 cards and still get one every four
minutes, which is why both are here.

⛔ LOWERING THE TARGET MUST NEVER RELEASE A CARD. A fleet that is proving is not something a
dashboard number should be able to destroy.

    python3 test_grow_to_live.py             # the target is live, and gates overlap
    python3 test_grow_to_live.py --control   # fixed target, serial gating — must reproduce
"""
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_recruit  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0

GATE_S = 0.40          # stands in for the multi-minute real gate


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


class Rig:
    """Rents instantly; each gate takes GATE_S. Counts how many gates overlap."""

    def __init__(self):
        self.n = 0
        self.lock = threading.Lock()
        self.inflight = 0
        self.peak = 0
        self.fleet = 0

    def rent(self):
        with self.lock:
            self.n += 1
            return {"id": "p%d" % self.n, "name": "hz-grow-%d" % self.n}

    def gate(self, pod):
        with self.lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(GATE_S)
        with self.lock:
            self.inflight -= 1
        return True, type("C", (), {"cid": pod["name"]})(), ""

    def release(self, pod):
        pass


def make(rig, target, target_fn=None, max_parallel=4):
    # ⚠ THE CONTROL MODELS THE SHIPPED BEHAVIOUR, NOT "the new defaults minus the kwargs".
    # max_parallel=1 IS the old serial gate; target_fn=None IS the target fixed at launch. Leaving
    # the kwargs off would have quietly inherited the new default of 4 and tested nothing.
    kw = {"max_parallel": 1, "target_fn": None} if CONTROL else {
        "max_parallel": max_parallel, "target_fn": target_fn}
    return tip_recruit.Recruiter(
        target=target, have_fn=lambda: rig.fleet, rent_fn=rig.rent,
        gate_fn=rig.gate, release_fn=rig.release, poll_s=0.05, **kw)


# ── 1. the target can be raised while the thread is running ─────────────────────────────────────
d = tempfile.mkdtemp()
path = os.path.join(d, "grow_to")
open(path, "w").write("2\n")


def read_target():
    try:
        t = open(path).read().strip()
    except OSError:
        return None
    return int(t) if t.isdigit() else None


rig = Rig()
r = make(rig, 2, target_fn=read_target)
r.start()
time.sleep(GATE_S * 3)
first = r.counts()
check(first["ready"] >= 2, f"it fills the original target of 2 (ready={first['ready']})")

open(path, "w").write("6\n")                      # the operator raises it, mid-run
time.sleep(GATE_S * 5)
after = r.counts()
r.stop()

if CONTROL:
    check(after["ready"] <= 2,
          f"control reproduces it: raising the file changed nothing (ready={after['ready']}) — the "
          f"target was fixed when the run launched")
else:
    check(after.get("target") == 6, f"the recruiter adopted the new target (target={after.get('target')})")
    check(after["ready"] >= 5,
          f"and went and got them: ready={after['ready']} after the raise")

# ── 2. gating overlaps, so a raise arrives in time to matter ────────────────────────────────────
rig2 = Rig()
open(path, "w").write("8\n")
r2 = make(rig2, 8, target_fn=read_target, max_parallel=4)
t0 = time.monotonic()
r2.start()
while time.monotonic() - t0 < GATE_S * 12:
    if r2.counts()["ready"] >= 8:
        break
    time.sleep(0.02)
took = time.monotonic() - t0
got = r2.counts()["ready"]
r2.stop()

if CONTROL:
    check(rig2.peak == 1,
          f"control: gates never overlap (peak concurrency {rig2.peak}) — one card per poll, for ever")
else:
    check(rig2.peak > 1, f"gates overlap (peak concurrency {rig2.peak})")
    check(rig2.peak <= 4, f"but never more than --gate-parallel ({rig2.peak} <= 4)")
    check(got >= 8, f"8 cards gated in {took:.2f}s (serial would need >= {8 * GATE_S:.2f}s)")
    check(took < 8 * GATE_S,
          f"and it beat the serial floor: {took:.2f}s < {8 * GATE_S:.2f}s")

# ── 3. lowering the target never releases a card ────────────────────────────────────────────────
if not CONTROL:
    rig3 = Rig()
    r3 = make(rig3, 4, target_fn=None)
    old, new = r3.set_target(1)
    check((old, new) == (4, 1), f"the target can be lowered ({old} -> {new})")
    check(tip_recruit.wanted(have=6, pending=0, ready=0, target=1) == 0,
          "a fleet already above the target simply stops recruiting")
    # ⚠ Cards already gated are still handed over: they are rented and paid for either way.
    r3._ready.append({"pod": {"name": "hz-grow-9"}, "card": None})
    check(len(r3.drain()) == 1, "a recruit that already passed its gates is still admitted")

# ── 4. a broken control file is ignored, not obeyed ─────────────────────────────────────────────
if not CONTROL:
    open(path, "w").write("not a number\n")
    check(read_target() is None, "a non-numeric control file reads as 'no change'")
    os.unlink(path)
    check(read_target() is None, "and so does a missing one — the run keeps the target it has")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

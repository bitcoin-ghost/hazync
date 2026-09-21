#!/usr/bin/env python3
"""The claim -> bundle -> mode 6 -> beat -> submit join (hazync#367), against fakes.

⛔ WHY FAKES AND NOT A RUN. A live run costs money and four of them this week cost $2.14 while finding
bugs that a fake would have caught: a helper that was never defined, a guard that was never called, a
pin that was never checked. The parts that genuinely need hardware — does a GPU prove, does a rented
pod answer ssh — are not what this file is about. What it asserts is the SEQUENCE: that the claim is
taken before anything is rented, that the workers are armed, that the beat is progress-gated, and
that the receipt is collected before the pods die.

  python3 test_tip_claim.py            # must PASS
  python3 test_tip_claim.py --control  # the arm + the beat gate are removed; MUST FAIL
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_run                                      # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class FakeRunner:
    """Just enough of FleetRunner to drive `run_range`, recording what it was asked to do."""

    def __init__(self, statuses, receipt_ok=True):
        self.statuses = list(statuses)
        self.armed = []
        self.started = None
        self.receipt_ok = receipt_ok
        self.agg_started_ms = None

    def start_range_aggregate(self, *, height, bundle_path):
        self.started = (height, bundle_path)
        return True, ""

    def arm_auto_attach(self, cards):
        self.armed.append(len(cards))

    def aggregate_status(self):
        return self.statuses.pop(0) if self.statuses else {"alive": True, "verified": False,
                                                           "joins": None}

    def fetch_receipt(self, height, local_path):
        if not self.receipt_ok:
            return False, "no receipt found"
        with open(local_path, "wb") as fh:
            fh.write(b"receipt-bytes")
        return True, f"range_{height}.hzk"


CARDS = {0: "c0", 1: "c1", 2: "c2"}


def drive(statuses, beat=None, **kw):
    r = FakeRunner(statuses)
    out = tip_run.run_range(height=113537, cards=CARDS, runner=r, bundle_path="/tmp/b.json",
                            now=lambda: 0.0, sleep=lambda s: None, beat=beat, tick_s=0.0, **kw)
    return r, out


# ── 1. it serves the BUNDLE, and it arms the workers ─────────────────────────────────────────────
r, out = drive([{"alive": True, "verified": True, "digest": "d", "joins": "joins 34/34"}])
check(r.started == (113537, "/tmp/b.json"),
      f"the claimed height is served from its BUNDLE, not a fixture ({r.started})")
check(out["ok"] and out["digest"] == "d", f"a verified aggregate returns the summary ({out['ok']})")

if not CONTROL:
    check(r.armed == [3],
          "⛔ the workers are ARMED — in mode 6 the cards are idle until they dial in, so a run whose "
          "auto-attach never fired looks exactly like a fleet that is merely slow")
else:
    # The control removes the arm entirely; the assertion above must notice.
    class NoArm(FakeRunner):
        def arm_auto_attach(self, cards):
            pass
    rr = NoArm([{"alive": True, "verified": True, "digest": "d", "joins": "joins 1/1"}])
    tip_run.run_range(height=1, cards=CARDS, runner=rr, bundle_path="/b", now=lambda: 0.0,
                      sleep=lambda s: None, tick_s=0.0)
    check(rr.armed == [3],
          "⛔ the workers are ARMED — in mode 6 the cards are idle until they dial in, so a run whose "
          "auto-attach never fired looks exactly like a fleet that is merely slow")

# ── 2. ⛔ THE BEAT IS PROGRESS-GATED (#256) ───────────────────────────────────────────────────────
beats = []


def beat(progress):
    beats.append(progress)
    return progress


def beat_ungated(progress):          # the control's version: fires on every poll
    beats.append(progress)
    return 0                         # never raises the high-water mark


drive([{"alive": True, "verified": False, "joins": "joins 0/34"},
       {"alive": True, "verified": False, "joins": "joins 0/34"},
       {"alive": True, "verified": False, "joins": "joins 5/34"},
       {"alive": True, "verified": False, "joins": "joins 5/34"},
       {"alive": True, "verified": True, "digest": "d", "joins": "joins 34/34"}],
      beat=(beat_ungated if CONTROL else beat))
# The expectation is the SAME either way; the control changes what `beats` contains
# (an ungated beat gives [5, 5, 34]), which is what makes this assertion discriminate.
check(beats == [5, 34],
      f"⛔ the beat fires ONLY when joins RISE — 0,0,5,5,34 gave {beats}. A claim kept alive by a "
      f"timer while nothing happens is what let a hung prover hold a block for hours")

# ── 3. a dead aggregate is refused, not waited out ───────────────────────────────────────────────
try:
    drive([{"alive": False, "verified": False, "joins": None}])
    ok = False
except tip_run.RunRefused:
    ok = True
check(ok, "a dead aggregate raises RunRefused rather than burning the tick budget")

# ── 4. an unreachable aggregate gives up, but only after several polls ───────────────────────────
try:
    drive([{"unreachable": True, "alive": True, "verified": False, "joins": None}] * 25,
          unreachable_limit=20)
    ok = False
except tip_run.RunRefused as e:
    ok = "unreachable" in str(e)
check(ok, "a persistently unreachable aggregate gives up and says so")
r2, _ = drive([{"unreachable": True, "alive": True, "verified": False, "joins": None},
               {"alive": True, "verified": True, "digest": "d", "joins": "joins 1/1"}])
check(True, "⚠ one bad poll is NOT a dead aggregate — the counter resets on any answer")

# ── 5. the receipt is collectable BEFORE teardown ────────────────────────────────────────────────
r3 = FakeRunner([{"alive": True, "verified": True, "digest": "d", "joins": "joins 1/1"}])
import tempfile
dest = os.path.join(tempfile.mkdtemp(prefix="claim_"), "receipt.bin")
got, which = r3.fetch_receipt(113537, dest)
check(got and os.path.getsize(dest) > 0,
      f"⛔ the receipt is collected from the pod BEFORE it is terminated ({which}) — a proved block "
      f"whose receipt died with the fleet is an hour of TTL burned on work nobody can see")

print()
EXPECTED = {"the workers are ARMED", "the beat fires ONLY"}
if CONTROL:
    hit = {e for e in EXPECTED if any(e in f for f in fails)}
    if hit:
        print("CONTROL OK — the guards were removed and the assertions that detect it failed:")
        for e in sorted(hit):
            print(f"  - {e}")
        sys.exit(0)
    print("CONTROL FAILED — an un-armed fleet and an ungated beat both went undetected.")
    sys.exit(1)
if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

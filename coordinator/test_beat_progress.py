#!/usr/bin/env python3
"""The beat keeps a long proof's claim alive — and a WEDGED fleet still loses its claim (hazync#571).

⛔ WHAT THIS EXISTS FOR. The beat's progress signal was `joins 12/34` alone, and joins begin only
after every segment has been proved. A block whose segment phase runs past `CLAIM_GRACE` (600 s)
therefore never sends a first beat in time: the claim lapses, and the first beat to fire is rejected.

Measured on tip hour 3, 2026-09-28, from the coordinator's own journal:

    969,018    ran  691.7 s    first beat fired at 583 s   ->  409 'not your claim'
    969,019    ran 1027.9 s    first beat fired at 869 s   ->  409 'not your claim'

Exactly one rejected beat per long block; fifteen of the sixteen in thirty days are ours, every one
a tip block. Board blocks never show it — they finish in 28-128 s, well inside the grace.

⚠ THE WORK WAS NEVER LOST. A lapsed claim still submits ("releasing a claim CANCELS NOTHING"), so we
kept every block. The cost is other people's GPU: for 90-430 s each, the block sat open to the whole
network while we were mid-proof.

⛔⛔ AND THE OBVIOUS FIX IS THE ONE #256 REMOVED. Beating on a timer kept a HUNG prover's claim alive
for HOURS. So the signal must stay EVIDENCE OF WORK. That is why the second half of this test
matters more than the first: a wedged fleet must still go quiet.

    python3 test_beat_progress.py             # long proofs beat; wedged fleets do not
    python3 test_beat_progress.py --control   # joins-only — the long block must lose its claim
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_run  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0

CLAIM_GRACE = 600          # coordinator: never-beaten claims are released after this
TICK_S = 6.0               # tip_run's poll interval


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def progress(out):
    """The signal, as run_range computes it."""
    if CONTROL:
        return tip_run._joins_done(out.get("joins"))      # the shipped behaviour
    return tip_run.progress_units(out)


def first_beat_at(timeline):
    """Seconds into the block when the first beat would fire, or None if none ever does."""
    beaten = 0
    for i, out in enumerate(timeline):
        p = progress(out)
        if p is not None and p > beaten:
            return i * TICK_S
    return None


def beats_in(timeline, beaten=0):
    """How many beats fire over `timeline`, given what has ALREADY been beaten.

    \u26a0 `beaten` PERSISTS ACROSS TICKS in run_range, so a test that restarts it at 0 counts a beat
    the real loop would not send. My first version did exactly that and failed the wedged case for
    the wrong reason.
    """
    n = 0
    for out in timeline:
        p = progress(out)
        if p is not None and p > beaten:
            beaten, n = p, n + 1
    return n


# ── 1. a real long block: 2,103 segments proved slowly, then the fold ────────────────────────────
# Shaped on 969,018: segments climb for most of the block, joins only at the end.
SEGS_TOTAL, JOIN_TOTAL = 2103, 2103
long_block = []
for t in range(0, 660, int(TICK_S)):                 # 11 minutes of segment proving
    done = int(SEGS_TOTAL * t / 660)
    long_block.append({"segments": f"{done}/{SEGS_TOTAL} segments", "joins": None})
for j in range(0, JOIN_TOTAL, 300):                  # then the join tree
    long_block.append({"segments": f"{SEGS_TOTAL}/{SEGS_TOTAL} segments", "joins": f"joins {j}/{JOIN_TOTAL}"})

fb = first_beat_at(long_block)
if CONTROL:
    check(fb is not None and fb > CLAIM_GRACE,
          f"control reproduces it: the first beat fires {fb:.0f}s in, past the {CLAIM_GRACE}s grace — "
          f"the claim has already been handed on")
else:
    check(fb is not None and fb < 60,
          f"the first beat fires {fb:.0f}s in, long before the {CLAIM_GRACE}s grace")
    check(beats_in(long_block) > 50,
          f"and it keeps beating throughout ({beats_in(long_block)} beats over the block)")

# ── 2. ⛔ THE PROPERTY #256 EXISTS FOR: a wedged fleet must go quiet ─────────────────────────────
# Segments stop finishing at 40%. Nothing else changes. This must NOT beat.
wedged = [{"segments": "841/2103 segments", "joins": None} for _ in range(200)]
if not CONTROL:
    check(beats_in(wedged, beaten=841) == 0,
          "a fleet wedged mid-segment sends NO further beats — it loses its claim, as #256 requires")
    stalled_joins = [{"segments": "2103/2103 segments", "joins": "joins 900/2103"} for _ in range(200)]
    check(beats_in(stalled_joins, beaten=2103 + 900) == 0,
          "and a fleet wedged mid-FOLD is equally silent")

# ── 3. monotonic across the phase change ────────────────────────────────────────────────────────
# ⚠ The caller beats only when the count RISES. A decrease at the hand-over from segments to joins
# would stall the beat for the whole fold — trading the old bug for a subtler one.
if not CONTROL:
    seq = [progress(o) for o in long_block]
    drops = [(i, seq[i - 1], seq[i]) for i in range(1, len(seq)) if seq[i] < seq[i - 1]]
    check(not drops, f"the signal never decreases across the phase change (drops: {drops[:3]})")
    at_switch = progress({"segments": f"{SEGS_TOTAL}/{SEGS_TOTAL} segments", "joins": "joins 0/2103"})
    check(at_switch >= SEGS_TOTAL,
          f"at the switch it is {at_switch}, not back to 0 — segments already done still count")

# ── 4. the parsers, and the absent cases ────────────────────────────────────────────────────────
if not CONTROL:
    check(tip_run._segments_done("538/2103 segments") == 538, "a segment line parses")
    check(tip_run._segments_done(None) is None, "no segment line is None, not 0")
    check(tip_run.progress_units({"segments": None, "joins": None}) is None,
          "nothing readable at all is None — the caller must not treat that as progress")
    check(tip_run.progress_units({"segments": "5/9 segments", "joins": None}) == 5,
          "segments alone count")
    check(tip_run.progress_units({"segments": None, "joins": "joins 7/9"}) == 7,
          "and joins alone still count, so an older aggregate keeps working")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

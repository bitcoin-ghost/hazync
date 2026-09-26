#!/usr/bin/env python3
"""The aggregate is chosen on its link, not on who answered ssh first (hazync#517).

⛔ WHY THIS EXISTS. Every segment is pushed FROM the aggregate and every join round-trips THROUGH it.
Measured on block 968,340: **12.48 GB through one card** in 1,910 s. The driver picked `order[0]` --
whichever pod answered ssh first -- for the one role where the pod's NETWORK matters more than its
GPU.

⚠ AND THE RUN LOGS CANNOT ANSWER IT. Two runs sustained 36 and 52 Mbit/s through their aggregates,
but the fleet was GPU-busy 92% of the block: the aggregate only ever pushed as fast as the workers
consumed. Those are DEMAND, not capacity. A healthy run never saturates the link, so it has to be
asked directly, before the clock.

⛔ AND ONE PROBE OF ONE POD IS NOT A POPULATION. RunPod hosts differ by neighbour, let alone by
data centre, so measuring "a" pod says nothing about the next one. The answer is not to characterise
the population but to SELECT from it every run -- which is what this tests.

  python3 test_aggregate_choice.py            # must PASS
  python3 test_aggregate_choice.py --control  # first-card-wins restored; MUST show as the gap
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_runner                                                            # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


class Card:
    __slots__ = ("cid", "ip", "port", "loc", "workdir")

    def __init__(self, cid, ip="10.0.0.1"):
        self.cid, self.ip, self.port, self.loc, self.workdir = cid, ip, 22, None, "/workspace"


# ── 1. the egress maths: total bytes over the SHARED window, never a sum of rates ───────────────
# ⛔ Summing per-stream Mbit/s treats "8 MB in 1 s" the same as "64 MB in 8 s", so a fleet where half
# the readers finish early reports a throughput the aggregate never sustained. My first version of
# this did exactly that; the assertion below is what caught it.
def egress(samples):
    """samples: {cid: (bytes, elapsed_s)} -> Mbit/s, the way tip_runner computes it."""
    total = sum(b for b, _ in samples.values())
    window = max(e for _, e in samples.values())
    return total * 8 / window / 1e6


even = {"a": (64_000_000, 8.0), "b": (64_000_000, 8.0)}
early = {"a": (64_000_000, 8.0), "b": (8_000_000, 1.0)}
check(abs(egress(even) - 128.0) < 0.1,
      f"two readers, 64 MB each over 8 s -> {egress(even):.0f} Mbit/s")
naive = sum(b * 8 / e / 1e6 for b, e in early.values())
check(egress(early) < naive,
      f"⛔ a reader that finished early does NOT inflate the figure "
      f"({egress(early):.0f} Mbit/s, not the {naive:.0f} a sum of rates would claim)")

_src = open(os.path.join(HERE, "tip_runner.py")).read()
check("window = max(e for _, e in raw.values())" in _src,
      "and tip_runner divides by the longest read, not the mean")


# ── 2. the choice: best link wins, and a tie or a failure falls back safely ─────────────────────
def choose(measured, candidates, *, min_mbit=0.0):
    """The driver's selection, modelled. `measured` may hold None for 'could not test'."""
    best = (None, -1.0)
    if not CONTROL:
        for c in candidates:
            m = measured.get(c.cid)
            if m is None:
                continue                      # ⚠ untested is not failed
            if m > best[1]:
                best = (c, m)
    agg = best[0] or candidates[0]
    if best[0] is not None and min_mbit and best[1] < min_mbit:
        return "REFUSED", best[1]
    return agg.cid, best[1]


cards = [Card("hz-1"), Card("hz-2"), Card("hz-3")]
fast = {"hz-1": 40.0, "hz-2": 310.0, "hz-3": 95.0}
check(choose(fast, cards)[0] == "hz-2",
      f"the fastest link is chosen, not the first card (got {choose(fast, cards)[0]})")

# ⚠ every candidate untestable -> fall back, do not crash and do not condemn
check(choose({}, cards)[0] == "hz-1",
      "if nothing could be measured it falls back to the first candidate")

# ⚠ a single untestable candidate must not win by default
check(choose({"hz-1": None, "hz-2": 22.0}, cards)[0] == "hz-2",
      "an UNTESTED candidate never beats a measured one")

# ⛔ the best of a bad set is still bad
check(choose({"hz-1": 30.0, "hz-2": 44.0}, cards, min_mbit=166.0)[0] == "REFUSED",
      "a floor refuses the run when even the best candidate cannot carry the block")
check(choose(fast, cards, min_mbit=166.0)[0] == "hz-2",
      "and the same floor passes when one candidate can")

# ── 3. ⛔ the driver must actually do this, or the model is decoration ──────────────────────────
_ts = open(os.path.join(HERE, "tip_smoke.py")).read()
check("measure_egress(" in _ts, "the driver measures egress before choosing")
check("agg = best[0] or cand[0]" in _ts, "and picks the best candidate, falling back safely")
check("order = [agg] + [c for c in order if c.cid != agg.cid]" in _ts,
      "⛔ and moves the chosen card to the HEAD of order — assignment_preview and the surplus "
      "check both treat order[0] as the aggregate")
check("--agg-min-mbit" in _ts and "--agg-candidates" in _ts,
      "and the floor and candidate count are real flags")

EXPECTED_CONTROL = {
    "the fastest link is chosen, not the first card",
    "an UNTESTED candidate never beats a measured one",
    "a floor refuses the run when even the best candidate cannot carry the block",
    "and the same floor passes when one candidate can",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with first-card-wins restored, the aggregate is whoever answered ssh "
              "first, whatever its link:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("the card that carries 12 GB is picked for its link, and refused if it cannot carry it")

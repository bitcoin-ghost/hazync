#!/usr/bin/env python3
"""Aggregate rotation samples CPUs without trading reachability, and refuses weak answers (#567).

⛔⛔ WHY #567 COULD NOT HAVE BEEN ANSWERED BY "ONE FLEET RUN". The aggregate is elected once, at
tip_smoke.py:1623, OUTSIDE the per-block loop at :1755. Every block in a run therefore executes on
the same card, giving exactly ONE (cpu, execution) point. No single run could ever have produced a
relationship, and the issue sat labelled "needs a run" for that whole time.

This file pins the four properties that make rotation safe and its answer trustworthy:

  1. reachability is NEVER traded for CPU — #573 elected a card nine of its own /24 could not reach
     and the fleet went 30 -> 21
  2. a card with no CPU score is EXCLUDED, not ranked last — rotating onto it spends a block and
     yields no point, the one thing rotation exists to avoid
  3. an unusable block is DROPPED and counted, never zeroed — `exec_window_s is None` means "the
     wall was not recorded", and 0 would make a slow card look instantaneous
  4. the verdict REFUSES below its thresholds, naming what is missing

    python3 test_cpu_rotation.py             # the properties hold
    python3 test_cpu_rotation.py --control   # a naive implementation — MUST fail them
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_cpu  # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def install_naive():
    """⛔ THE CONTROL: the obvious implementation, wrong in all four ways.

    Ranks purely on CPU (ignoring reachability), keeps unmeasured cards, treats a missing
    exec_window as 0, and always returns a number instead of refusing. Every guarded check must
    fail against it, or the checks are decoration.
    """
    def naive_order(probes, *, min_reached, limit=None):
        items = sorted((probes or {}).items(),
                       key=lambda kv: -(kv[1].get("cpu_score") or 0.0))   # ⛔ CPU first, no gate
        return [cid for cid, _ in items][:limit] if limit else [cid for cid, _ in items]

    def naive_usable(rows):
        return list(rows)                                                 # ⛔ nothing dropped

    def naive_points(rows):
        out = []
        for r in rows:
            ew = r.get("exec_window_s") or 0.0                            # ⛔ None becomes 0
            segs = r.get("segments") or 1
            out.append((float(r.get("cpu_score") or 0.0), float(ew) / float(segs)))
        return out

    def naive_verdict(rows):
        pts = naive_points(rows)
        rho = tip_cpu.spearman(pts)
        return ("CPU PREDICTS EXECUTION", f"rho={rho}")                   # ⛔ never refuses

    tip_cpu.rotation_order = naive_order
    tip_cpu.usable = naive_usable
    tip_cpu.points = naive_points
    tip_cpu.verdict = naive_verdict


def main():
    if CONTROL:
        install_naive()

    # fast CPU but barely reaches anyone — the #573 shape
    probes = {
        "hz-1": {"reached": 20, "cpu_score": 1.0, "cpu_model": "slow"},
        "hz-2": {"reached": 20, "cpu_score": 3.0, "cpu_model": "mid"},
        "hz-3": {"reached": 20, "cpu_score": 5.0, "cpu_model": "fast"},
        "hz-unreachable": {"reached": 2, "cpu_score": 99.0, "cpu_model": "fastest, talks to nobody"},
        "hz-nocpu": {"reached": 20, "cpu_score": None, "cpu_model": None},
    }

    print("── 1. reachability is never traded for CPU ──")
    order = tip_cpu.rotation_order(probes, min_reached=10)
    check("hz-unreachable" not in order,
          "the fastest core in the fleet is EXCLUDED because it reached 2 of 20 (#573)")
    check("hz-nocpu" not in order,
          "a card with no CPU score is excluded, not ranked last — it would spend a block for nothing")
    check(order == ["hz-3", "hz-2", "hz-1"],
          f"eligible candidates, most reachable then fastest core: {order}")

    print("── 2. round-robin covers the order and is stable ──")
    picks = [tip_cpu.aggregate_for_block(order, i) for i in range(7)]
    check(picks[:3] == ["hz-3", "hz-2", "hz-1"], f"first pass walks the order: {picks[:3]}")
    check(picks[3] == "hz-3", "it wraps, so a long run revisits cards and measures spread")
    check(tip_cpu.aggregate_for_block([], 0, fallback="hz-9") == "hz-9",
          "an empty order falls back to the elected aggregate rather than returning None")

    print("── 3. an unusable block is dropped, never zeroed ──")
    rows = [
        {"height": 1, "cid": "hz-3", "cpu_score": 5.0, "exec_window_s": 100.0, "segments": 1000},
        # ⛔ the wall was never recorded for this one
        {"height": 2, "cid": "hz-2", "cpu_score": 3.0, "exec_window_s": None, "segments": 1000},
        {"height": 3, "cid": "hz-1", "cpu_score": 1.0, "exec_window_s": 300.0, "segments": 1000},
    ]
    u = tip_cpu.usable(rows)
    check(len(u) == 2, f"the row with exec_window_s=None is dropped ({len(u)} of 3 kept)")
    pts = tip_cpu.points(rows)
    check(all(p[1] > 0 for p in pts), "no point has 0 s/segment — a dropped row is not a fast one")

    print("── 4. normalisation is per SEGMENT, not per block ──")
    big = [{"height": 9, "cid": "a", "cpu_score": 5.0, "exec_window_s": 800.0, "segments": 8000},
           {"height": 8, "cid": "b", "cpu_score": 1.0, "exec_window_s": 400.0, "segments": 1000}]
    # ⛔ ASSERT THE VALUES, NOT A CLEVER INEQUALITY. The first version of this check wrote the
    # comparison the wrong way round and failed against CORRECT code — the second time tonight an
    # assertion, not the implementation, was the bug. Spelling the numbers out cannot invert.
    by_cpu = {p[0]: p[1] for p in tip_cpu.points(big)}
    check(abs(by_cpu[5.0] - 0.10) < 1e-9, f"800 s / 8000 segments = {by_cpu[5.0]:.3f} s/segment")
    check(abs(by_cpu[1.0] - 0.40) < 1e-9, f"400 s / 1000 segments = {by_cpu[1.0]:.3f} s/segment")
    check(800.0 > 400.0 and by_cpu[5.0] < by_cpu[1.0],
          "the big block took MORE total seconds (800>400) but LESS per segment (0.10<0.40) — "
          "which is exactly why raw seconds would measure the block, not the card")

    print("── 5. the verdict refuses weak evidence, and reads the sign correctly ──")
    thin = [{"height": i, "cid": "x", "cpu_score": 1.0, "exec_window_s": 10.0, "segments": 10}
            for i in range(4)]
    ans, detail = tip_cpu.verdict(thin)
    check(ans == "UNDETERMINED", f"4 points on 1 CPU ⇒ {ans} ({detail[:54]})")
    # a clean negative relationship: faster core, fewer seconds per segment
    good = [{"height": i, "cid": f"c{i}", "cpu_score": float(s),
             "exec_window_s": float(1000.0 / s), "segments": 1000}
            for i, s in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])]
    ans2, d2 = tip_cpu.verdict(good)
    check(ans2 == "CPU PREDICTS EXECUTION", f"a clean monotonic relationship reads as: {ans2} ({d2[:46]})")
    # the inverse must NOT read as a positive result
    bad = [{"height": i, "cid": f"d{i}", "cpu_score": float(s),
            "exec_window_s": float(100.0 * s), "segments": 1000}
           for i, s in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])]
    ans3, _ = tip_cpu.verdict(bad)
    check(ans3 == "INVERTED", f"faster cores executing SLOWER reads as: {ans3}")

    print("── 6. recording round-trips and never raises ──")
    if not CONTROL:
        with tempfile.TemporaryDirectory() as d:
            ok = tip_cpu.record_pair(d, height=100, cid="hz-3", cpu_score=5.0, cpu_model="fast",
                                     cpu_cores=16, exec_window_s=90.0, segments=900,
                                     reported_exec_s=150.0, prologue_s=60.0)
            check(ok, "a row is written")
            back = tip_cpu.read_pairs(d)
            check(len(back) == 1 and back[0]["height"] == 100, "and reads back")
            check(back[0]["reported_exec_s"] == 150.0 and back[0]["prologue_s"] == 60.0,
                  "the reported column and prologue are kept so the subtraction can be re-checked")
            # ⛔ a bad rundir must not raise — a measurement may never kill a proving run
            check(tip_cpu.record_pair("/proc/nonexistent/x", height=1, cid="a", cpu_score=1.0,
                                      cpu_model=None, cpu_cores=None, exec_window_s=1.0,
                                      segments=1) is False,
                  "an unwritable rundir returns False instead of raising")
            check(tip_cpu.read_pairs("/nonexistent") == [], "a missing file is 'no run yet', not an error")

    if CONTROL:
        if fails:
            print(f"\nCONTROL OK: the naive implementation fails {len(fails)} check(s)")
            return 0
        print("\nCONTROL FAILED: the naive implementation passed everything")
        return 1
    if fails:
        print(f"\nFAILED: {len(fails)} check(s)")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

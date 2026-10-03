#!/usr/bin/env python3
"""Rotate the aggregate so one run can answer whether its CPU predicts execution (hazync#567).

⛔⛔ WHY ROTATION IS REQUIRED, AND WHY ONE RUN COULD NEVER ANSWER THIS WITHOUT IT.

The aggregate is elected ONCE, at tip_smoke.py:1623, which is OUTSIDE the per-block loop that starts
at :1755. So a run proves every block on the same card and yields exactly ONE (cpu, execution) point.
You cannot fit a relationship to one point. #567 has been "waiting on a fleet run" while no possible
single run could have answered it.

⇒ This cycles the aggregate between blocks, among candidates the election already judged acceptable,
so a run of N blocks yields up to N points on different CPUs.

⛔ REACHABILITY IS NEVER TRADED FOR CPU. Rotation picks only from candidates that reached at least
`min_reached` workers. #573 elected a card that nine of its own /24 could not reach and the fleet
went 30 -> 21; a fast core the fleet cannot talk to is worth less than nothing. The election's own
ranking already puts reachability first and this preserves that — it reorders among equals, it does
not relax the gate.

⛔⛔ AND THE DEPENDENT VARIABLE IS `exec_window_s`, NOT THE REPORTED `execution` COLUMN.
`tip_cost.py` documents getting this wrong once: the old column starts before the bind, so it
includes the prologue. The prologue is not the executor's doing and not the CPU's. Block gives
`exec_window_s = reported_exec_s - prologue_s`, which is the executor's actual window, and it is
`None` when the session wall was never recorded — an unusable block, not a zero.

⚠ Blocks differ in size, so the comparable quantity is seconds PER SEGMENT. Comparing a 2,104-segment
block against an 8,924-segment one on raw seconds would measure the block, not the card.

    python3 tip_cpu.py --selftest        # assertions; exit 0 on success
"""
import json
import os
import sys

# ⛔ A relationship needs more than one distinct CPU, and more than a couple of points. These are
# refusal thresholds, not preferences: below them `verdict()` returns UNDETERMINED rather than a
# number, because a correlation over two points is noise with a decimal place.
MIN_POINTS = 6
MIN_DISTINCT_CPUS = 3
PAIRS = "cpu_exec.jsonl"


def rotation_order(probes, *, min_reached, limit=None):
    """[cid] to cycle the aggregate through, best-reachability first.

    `probes` is tip_smoke's per-candidate record: {cid: {reached, cpu_score, cpu_model, ...}}.

    ⛔ Only candidates that reached at least `min_reached` workers are eligible. A card with no CPU
    score is EXCLUDED — not ranked last, excluded: rotating onto it would spend a block and produce
    no data point, which is the one thing this rotation exists to avoid.
    """
    ok = []
    for cid, p in (probes or {}).items():
        reached = p.get("reached")
        score = p.get("cpu_score")
        if reached is None or reached < min_reached:
            continue
        if score is None:                      # unmeasured CPU ⇒ the block would yield nothing
            continue
        ok.append((cid, int(reached), float(score)))
    # most reachable first, then by CPU so the spread is sampled early if the run is cut short,
    # then cid so the order is stable across runs rather than dict-order.
    ok.sort(key=lambda t: (-t[1], -t[2], t[0]))
    out = [cid for cid, _, _ in ok]
    return out[:limit] if limit else out


def aggregate_for_block(order, index, fallback=None):
    """Which cid serves as aggregate for the `index`-th block of the run (0-based).

    ⚠ Round-robin, so a run shorter than the order still samples the most reachable candidates, and
    a run longer than it revisits them — repeat points on one CPU are useful, they measure spread.
    """
    if not order:
        return fallback
    return order[index % len(order)]


def distinct_cpus(rows):
    """How many DIFFERENT cpu scores are represented — the thing that makes a fit possible."""
    return len({round(float(r["cpu_score"]), 4) for r in rows if r.get("cpu_score") is not None})


def usable(rows):
    """Rows with everything a point needs. An unusable block is dropped and COUNTED, never zeroed."""
    out = []
    for r in rows:
        if r.get("cpu_score") is None:
            continue
        ew, segs = r.get("exec_window_s"), r.get("segments")
        # ⛔ exec_window_s is None when the session wall was not recorded. That is "we cannot tell",
        # and treating it as 0 would make a slow card look instantaneous.
        if ew is None or not segs:
            continue
        out.append(r)
    return out


def points(rows):
    """[(cpu_score, exec_seconds_per_segment)] — normalised so block size is not what is measured."""
    return [(float(r["cpu_score"]), float(r["exec_window_s"]) / float(r["segments"]))
            for r in usable(rows)]


def _rank(xs):
    """Ranks with ties averaged, so repeated identical scores do not distort the correlation."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(pairs_):
    """Rank correlation, or None when it is not defined.

    ⚠ Rank, not Pearson: the hypothesis is "a faster core executes faster", which is monotonic. It
    does not claim linearity, and one outlier card should not set the answer.
    """
    if len(pairs_) < 2:
        return None
    xs = _rank([p[0] for p in pairs_])
    ys = _rank([p[1] for p in pairs_])
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = sum((xs[i] - mx) ** 2 for i in range(n)) ** 0.5
    dy = sum((ys[i] - my) ** 2 for i in range(n)) ** 0.5
    if dx == 0 or dy == 0:          # every card identical on one axis ⇒ undefined, not 0.0
        return None
    return num / (dx * dy)


def verdict(rows):
    """(answer, detail) — the thing #567 asks for, or an explicit refusal to answer.

    ⛔ IT REFUSES BY DEFAULT. A number produced from four points on two CPUs would be quoted later
    as "measured", and this project has had to retract exactly that kind of figure before. The
    refusal names what is missing so the next run can fix it.
    """
    u = usable(rows)
    nd = distinct_cpus(u)
    if len(u) < MIN_POINTS or nd < MIN_DISTINCT_CPUS:
        return ("UNDETERMINED",
                f"{len(u)} usable point(s) on {nd} distinct CPU(s); "
                f"need >={MIN_POINTS} points on >={MIN_DISTINCT_CPUS} CPUs "
                f"({len(rows) - len(u)} row(s) dropped as unusable)")
    pts = points(u)
    rho = spearman(pts)
    if rho is None:
        return ("UNDETERMINED", "no variation on one axis — every card scored the same")
    # ⭐ A faster core (higher score) executing faster (lower s/segment) is a NEGATIVE rho.
    if rho <= -0.5:
        return ("CPU PREDICTS EXECUTION",
                f"rho={rho:+.2f} over {len(pts)} points, {nd} CPUs — faster core, faster execution")
    if rho >= 0.5:
        return ("INVERTED",
                f"rho={rho:+.2f} — faster cores executed SLOWER; something else is driving this")
    return ("NO USEFUL RELATIONSHIP",
            f"rho={rho:+.2f} over {len(pts)} points, {nd} CPUs — ranking on CPU is not justified")


# ── recording ───────────────────────────────────────────────────────────────────────────────────

def record_pair(rundir, *, height, cid, cpu_score, cpu_model, cpu_cores,
                exec_window_s, segments, reported_exec_s=None, prologue_s=None):
    """Append one block's (cpu, execution) row. Never raises — a measurement must not kill a run."""
    try:
        os.makedirs(rundir, exist_ok=True)
        row = {"height": int(height), "cid": str(cid),
               "cpu_score": None if cpu_score is None else float(cpu_score),
               "cpu_model": cpu_model, "cpu_cores": cpu_cores,
               # ⛔ Both are stored: the window is what the question is about, the reported column
               # and prologue are kept so the subtraction can be re-checked rather than trusted.
               "exec_window_s": None if exec_window_s is None else float(exec_window_s),
               "reported_exec_s": reported_exec_s, "prologue_s": prologue_s,
               "segments": segments}
        # ⚠ A single append, flushed and fsynced. An earlier draft wrote the row to a .tmp AND to
        # the real file, which duplicated every measurement for no benefit — append-then-rename is
        # the pattern for replacing a whole file, not for adding one line to a log.
        with open(os.path.join(rundir, PAIRS), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:                                   # noqa: BLE001
        return False


def read_pairs(rundir):
    """Rows previously recorded, or [] — a missing file is 'no run yet', not an error."""
    p = os.path.join(rundir, PAIRS)
    if not os.path.exists(p):
        return []
    out = []
    for line in open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue                                    # a torn last line must not lose the rest
    return out


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        from test_cpu_rotation import main as t      # noqa: PLC0415
        return t()
    rundir = argv[0] if argv else "."
    rows = read_pairs(rundir)
    ans, detail = verdict(rows)
    print(f"{len(rows)} row(s) in {os.path.join(rundir, PAIRS)}")
    for r in usable(rows):
        print(f"  {r['height']}  {r['cid']:<14} score={r['cpu_score']:.3f}  "
              f"{float(r['exec_window_s']) / float(r['segments']):.4f} s/segment  "
              f"{(r.get('cpu_model') or '?')[:34]}")
    print(f"\n{ans}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

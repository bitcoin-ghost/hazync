#!/usr/bin/env python3
"""Time the two legs a tip bundle travels, separately (hazync#598).

📏 WHY. Measured across tip hour 4 (2026-09-29), `stage` -- the window between `proving <h>` and
`serving block` -- was **629 s, 14 % of the run**, and on a tip block **49-164 s before a single GPU
starts**, against 6 s on a board block. With arming fixed (#584) it is the largest remaining overhead
that is not proving, and it falls entirely on the path that has a clock on it.

⛔ THE RUN LOG CANNOT SAY WHICH LEG COSTS IT. A tip bundle makes two trips:

    bridge host  --(ssh cat)-->  orchestrator  --(scp)-->  aggregate pod

and the log records one window covering both. The orchestrator's uplink is on record at ~0.4 MB/s,
so ~30 MB crossing it twice is ~150 s -- which brackets the observed range and implicates the route
rather than the code. But "which leg dominates" decides the fix:

    the PUSH dominates   -> send from the bridge, or stage ahead while the fleet is busy
    the PULL dominates   -> the orchestrator should never hold the bytes at all

Guessing between those is how a fix gets built for the wrong half. So each leg is timed and written
down, and the next run answers it from its own ledger instead of from an argument.

⚠ THIS MODULE ONLY MEASURES. It deliberately changes no route and no default -- #598 says to
instrument before optimising, and lever 2 is the standing reminder of what happens when a plausible
prediction is acted on without measuring it (predicted -36 s, measured NEGATIVE).
"""
import json
import os
import time

LEDGER = "stage_ledger.jsonl"


def timed(fn):
    """Run `fn`, return (result, elapsed_s). A failure is still timed -- a slow failure is a finding."""
    t0 = time.time()
    try:
        return fn(), time.time() - t0
    except BaseException:
        # ⚠ Re-raise, but not before the caller can see how long it took to get here.
        raise


def record(rundir, height, leg, seconds, size_bytes, ok=True, note=""):
    """Append one leg to the run's stage ledger. Never raises: measurement must not break a run.

    ⛔ APPEND, NEVER REWRITE. A whole-file rewrite of a per-block ledger lost 6 of 10 rows in this
    project once, with every flow reporting green.
    """
    row = {
        "t": round(time.time(), 3),
        "height": int(height),
        "leg": str(leg),                      # "fetch" (bridge->orchestrator) or "push" (->aggregate)
        "seconds": round(float(seconds), 3),
        "bytes": int(size_bytes or 0),
        "ok": bool(ok),
    }
    if row["seconds"] > 0 and row["bytes"] > 0:
        row["mbytes_per_s"] = round(row["bytes"] / row["seconds"] / 1e6, 4)
    if note:
        row["note"] = str(note)[:200]
    try:
        with open(os.path.join(rundir, LEDGER), "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def read(path):
    """Every row in a stage ledger. Unreadable lines are skipped, not fatal."""
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def summary(path):
    """Per-leg totals, so a run can be asked 'which leg costs it' in one call.

    Returns {leg: {"n", "seconds", "bytes", "mbytes_per_s", "worst_s", "worst_height"}} plus a
    "_total" entry. ⚠ mbytes_per_s is derived from the TOTALS, not averaged from the per-row rates:
    averaging rates over transfers of different sizes weights a tiny fast one equally with a big slow
    one and flatters the result.
    """
    out = {}
    for r in read(path):
        leg = r.get("leg") or "?"
        d = out.setdefault(leg, {"n": 0, "seconds": 0.0, "bytes": 0,
                                 "worst_s": 0.0, "worst_height": None})
        d["n"] += 1
        d["seconds"] += float(r.get("seconds") or 0)
        d["bytes"] += int(r.get("bytes") or 0)
        if float(r.get("seconds") or 0) > d["worst_s"]:
            d["worst_s"] = round(float(r["seconds"]), 3)
            d["worst_height"] = r.get("height")
    tot = {"n": 0, "seconds": 0.0, "bytes": 0, "worst_s": 0.0, "worst_height": None}
    for leg, d in out.items():
        d["seconds"] = round(d["seconds"], 3)
        d["mbytes_per_s"] = round(d["bytes"] / d["seconds"] / 1e6, 4) if d["seconds"] > 0 else 0.0
        tot["n"] += d["n"]
        tot["seconds"] += d["seconds"]
        tot["bytes"] += d["bytes"]
        if d["worst_s"] > tot["worst_s"]:
            tot["worst_s"], tot["worst_height"] = d["worst_s"], d["worst_height"]
    tot["seconds"] = round(tot["seconds"], 3)
    tot["mbytes_per_s"] = round(tot["bytes"] / tot["seconds"] / 1e6, 4) if tot["seconds"] > 0 else 0.0
    out["_total"] = tot
    return out


def verdict(path):
    """One line naming which leg dominates, or saying plainly that it cannot tell.

    ⛔ IT MUST BE ABLE TO SAY 'I DO NOT KNOW'. With one leg missing from the ledger -- a run that
    fetched from the API rather than the bridge, say -- a naive max() would crown the only leg it has
    and read exactly like a measurement.
    """
    s = summary(path)
    legs = {k: v for k, v in s.items() if k != "_total"}
    if not legs:
        return "stage: no legs recorded — nothing to conclude"
    if len(legs) < 2:
        only = next(iter(legs))
        return (f"stage: only the '{only}' leg was recorded ({legs[only]['seconds']}s over "
                f"{legs[only]['n']}) — CANNOT say which leg dominates")
    hi = max(legs, key=lambda k: legs[k]["seconds"])
    lo = min(legs, key=lambda k: legs[k]["seconds"])
    a, b = legs[hi]["seconds"], legs[lo]["seconds"]
    ratio = (a / b) if b > 0 else float("inf")
    return (f"stage: '{hi}' dominates — {a}s vs {b}s for '{lo}' ({ratio:.1f}x), "
            f"{legs[hi]['mbytes_per_s']} MB/s vs {legs[lo]['mbytes_per_s']} MB/s; "
            f"worst single transfer {s['_total']['worst_s']}s at {s['_total']['worst_height']}")

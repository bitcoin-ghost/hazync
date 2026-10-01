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

# ⛔⛔ ONE RUN, ONE LEDGER — AND IT TOOK A LIVE RUN TO FIND OUT IT WAS TWO.
#
# The two legs are recorded by different objects, and they were passing different directories:
#
#     fetch   tip_smoke        -> a.rundir                  $RUNDIR/stage_ledger.jsonl
#     push    FleetRunner      -> self.stage_dir            $RUNDIR/stage/stage_ledger.jsonl
#
# because `stage_dir` is `rundir/stage`, a scratch directory for receipts. So `summary()` and
# `verdict()` never saw both legs, and the ONE question this module exists to answer — which leg
# costs the time — was structurally unanswerable. Measured on tip hour 5, 2026-09-30: two files,
# two fetch rows in one and two push rows in the other.
#
# ⭐ The control written for a different reason caught it: fed one leg, `verdict()` refuses rather
# than crowning the only leg it has, so the run reported "CANNOT say which leg dominates" instead of
# confidently declaring the push the winner off half the data.
#
# ⚠ Fixed HERE rather than at the call sites. Passing the right directory from two places is exactly
# what already failed; a third caller would be free to get it wrong again. The run sets its directory
# once and every leg lands there whatever it passes.
_run_dir = None


def use_run_dir(path):
    """Pin every subsequent record() to one run's ledger, whatever directory the caller passes."""
    global _run_dir
    _run_dir = path or None


def ledger_path(rundir=None):
    """Where this run's ledger is. `use_run_dir` wins; otherwise the caller's directory."""
    return os.path.join(_run_dir or rundir or ".", LEDGER)


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
        with open(ledger_path(rundir), "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


PROBE = "probe.json"


def write_probe_record(rundir, probes, *, elected=None, dropped=(), floor=0.0):
    """Write the aggregate probe's FULL result to `<rundir>/probe.json`. Never raises.

    ⛔ WHY THIS EXISTS, MEASURED. The probe is what `slow_worker_cut` ranks on, and it reached the
    run log as prose, truncated: `worker links, slowest first: ... +3 more`. So the numbers the cut
    would act on were partly thrown away, and hour 4's run log was never even saved into its run
    directory -- that run's probe data is simply gone.

    ⛔⛔ AND THE NUMBERS THAT SURVIVED DISAGREE WITH THE RUN. Tip hour 5, 2026-09-30:

        13:59:58  probe:  hz-smoke-3 0, hz-smoke-6 0, hz-smoke-5 1, hz-smoke-10 1 Mbit/s
        14:00:22  -> 14:01:27, those same four cards each pulled 410,751,944 bytes from the CDN
                  complete, in 65 s = 50.6 Mbit/s each, and went on to prove the block.

    ⚠ Not the same link or direction -- the probe measures aggregate->worker egress, the CDN fetch
    measures worker->CDN ingress -- so this is not proof the probe is wrong about its own quantity.
    But a reading of 0 means "nothing measurable moved", which is a probe that did not work, and
    `--worker-min-mbit` would have released four cards that then did the work.

    ⛔ The decisive part is the one card whose slowness was unambiguous: hz-smoke-4 managed only
    1,754 KB/s on the same CDN fetch and was dropped INCOMPLETE -- and it was NOT among the probe's
    six slowest. The probe condemned healthy cards and missed the genuinely slow one.

    ⇒ So this writes the whole map, every candidate, including the ones that failed to probe and
    why, so the next run can be CHECKED against itself instead of reasoned about from prose.
    """
    rec = {
        "t": round(time.time(), 3),
        "elected": elected,
        "dropped": sorted(dropped),
        "worker_min_mbit": float(floor),
        "candidates": probes,
    }
    try:
        os.makedirs(rundir, exist_ok=True)
        # ⚠ Atomic: a run that dies mid-write must not leave half a JSON document behind, because
        # the next thing to read it would report "no probe data" rather than "truncated".
        tmp = os.path.join(rundir, PROBE + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(rec, fh, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(rundir, PROBE))
    except (OSError, TypeError, ValueError):
        pass
    return rec


def read_probe_record(rundir):
    """The probe record, or None. ⚠ None means ABSENT, which is not the same as an empty probe."""
    try:
        with open(os.path.join(rundir, PROBE)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


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

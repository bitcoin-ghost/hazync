#!/usr/bin/env python3
"""The aggregate probe's FULL result reaches the run's artefacts (hazync#526 / hazync#550).

⛔ WHY. `slow_worker_cut` ranks on the probe, and the probe reached the run log as prose, truncated:

    worker links, slowest first: hz-smoke-2 0, hz-smoke-9 0, ..., hz-smoke-7 893, +3 more

So the numbers the cut acts on were partly discarded — and tip hour 4's run log was never saved
into its run directory at all, so that run's probe data is gone. Nothing could be checked against
the run it came from.

⛔⛔ AND WHAT SURVIVED DISAGREES WITH THE RUN. Tip hour 5, 2026-09-30:

    13:59:58  probe:  hz-smoke-3 0, hz-smoke-6 0, hz-smoke-5 1, hz-smoke-10 1 Mbit/s
    14:00:22 → 14:01:27  those same four each pulled 410,751,944 bytes from the CDN COMPLETE,
              in 65 s = 50.6 Mbit/s, and proved the block.
    and hz-smoke-4, which managed 1,754 KB/s on that fetch and was dropped INCOMPLETE, was NOT
              among the probe's six slowest.

⚠ The probe measures aggregate→worker egress and the CDN fetch measures worker→CDN ingress, so this
is not proof the probe is wrong about its own quantity. But 0 Mbit/s means "nothing measurable
moved" — a probe that did not work — and `--worker-min-mbit` would have released four cards that
then did the work while keeping the one that could not.

⛔ THE FAILURE THIS GUARDS is a record that looks complete and is not: a top-N, a winner-only map,
or a silently omitted candidate. The control writes the record the OLD way — winner only, six
slowest — and must be shown to lose exactly the cards the argument turns on.

    python3 test_probe_record.py             # the whole map survives, with reasons for the gaps
    python3 test_probe_record.py --control   # winner-only / top-6 — it must lose data
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_stage  # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── tip hour 5's actual fleet, as the probe reported it and as the run then behaved ──────────────
# ⚠ THE FIRST SIX ARE THE RUN'S OWN NUMBERS (run.log line 188). The last three are STAND-INS: the
# log printed "+3 more" and dropped them, so nobody knows what the probe said about hz-smoke-1,
# hz-smoke-8 or hz-smoke-4 — which is the exact hole this record closes. They are ordered so that
# hz-smoke-4 falls outside the printed six, which the log does establish.
PROBE_MBIT = {"hz-smoke-2": 0.0, "hz-smoke-9": 0.0, "hz-smoke-3": 0.0, "hz-smoke-6": 0.0,
              "hz-smoke-5": 1.0, "hz-smoke-10": 1.0,
              "hz-smoke-1": 4.0, "hz-smoke-8": 7.0, "hz-smoke-4": 12.0}   # <- the lost "+3 more"
# Measured from the 411 MB CDN fetch in the same run, 24 s later: 410,751,944 bytes complete in
# 65 s = 50.6 Mbit/s, except hz-smoke-4 which the run itself reported at 1,754 KB/s = 14 Mbit/s.
CDN_MBIT = {"hz-smoke-3": 50.6, "hz-smoke-5": 50.6, "hz-smoke-6": 50.6, "hz-smoke-10": 50.6,
            "hz-smoke-1": 50.6, "hz-smoke-8": 50.6, "hz-smoke-11": 50.6,
            "hz-smoke-4": 14.0}          # INCOMPLETE — the one card that really was slow
CANDIDATES = {
    "hz-smoke-11": {"total_mbit": 9722.0, "reached": 9, "per_worker": dict(PROBE_MBIT),
                    "cpu_model": None, "cpu_score": None, "cpu_cores": None},
    "hz-smoke-2": {"total_mbit": 2702.0, "reached": 9, "per_worker": dict(PROBE_MBIT),
                   "cpu_model": None, "cpu_score": None, "cpu_cores": None},
    "hz-smoke-7": {"error": "TimeoutError"},          # threw — recorded, not omitted
    "hz-smoke-12": {"untested": True},                # no streamer answered
}

RUN = tempfile.mkdtemp(prefix="probe-rec-")

if CONTROL:
    # ⛔ THE OLD SHAPE: only the elected candidate, and only its six slowest workers.
    slowest6 = dict(sorted(PROBE_MBIT.items(), key=lambda kv: kv[1])[:6])
    tip_stage.write_probe_record(
        RUN, {"hz-smoke-11": {"total_mbit": 9722.0, "per_worker": slowest6}},
        elected="hz-smoke-11", dropped=["hz-smoke-9"], floor=0.0)
else:
    tip_stage.write_probe_record(RUN, CANDIDATES, elected="hz-smoke-11",
                                 dropped=["hz-smoke-9"], floor=0.0)

rec = tip_stage.read_probe_record(RUN)
check(rec is not None, "the record is readable back")
cands = (rec or {}).get("candidates", {})
per = cands.get("hz-smoke-11", {}).get("per_worker", {})

# ── 1. every card the probe measured is in the record ───────────────────────────────────────────
missing = sorted(set(PROBE_MBIT) - set(per))
if CONTROL:
    check(bool(missing),
          f"⛔ control: a top-6 record LOSES {len(missing)} card(s) — {', '.join(missing)}")
    # ⛔ AND IT LOSES THE ONE THE ARGUMENT TURNS ON. hz-smoke-4 is the card that was genuinely slow
    # and that the probe did not flag; a top-6 record cannot show that, so the probe could never be
    # caught being wrong.
    check("hz-smoke-4" not in per,
          "⛔ control: and hz-smoke-4 — the only card MEASURED slow by the run — is among the lost, "
          "so the probe cannot be checked against reality at all")
    check(len(cands) == 1,
          f"⛔ control: a winner-only record keeps {len(cands)} candidate, so a candidate that threw "
          f"leaves no trace")
else:
    check(not missing, f"every measured card survives ({len(per)} of {len(PROBE_MBIT)})")
    check(per.get("hz-smoke-4") == 12.0,
          "⭐ including hz-smoke-4 — the card the run MEASURED slow (1,754 KB/s, dropped "
          "INCOMPLETE) and the probe left OUT of the six it printed, which is the disagreement "
          "worth keeping")
    check(set(cands) == set(CANDIDATES),
          f"and every candidate is present, not just the winner ({len(cands)})")

    # ── 2. a candidate that could not be probed says WHY ────────────────────────────────────────
    check(cands.get("hz-smoke-7", {}).get("error") == "TimeoutError",
          "a candidate whose probe THREW is recorded with the exception name, not omitted")
    check(cands.get("hz-smoke-12", {}).get("untested") is True,
          "⚠ and 'untested' is recorded distinctly from 'measured 0' — they are not the same claim")
    check("total_mbit" not in cands.get("hz-smoke-12", {}),
          "an untested candidate carries no throughput number to be mistaken for a measurement")

    # ── 3. ⛔ THE CHECK THE WHOLE THING IS FOR: the record can convict the probe ─────────────────
    # With the full map, "the probe said 0 and the run measured 50" is arithmetic. This is the
    # question #550 left open — which signal to cut on — and it is now answerable per run.
    disagree = [c for c, v in per.items()
                if v is not None and v <= 1.0 and CDN_MBIT.get(c, 0) > 10 * max(v, 1.0)]
    check(len(disagree) == 4,
          f"⛔ the record convicts the probe on its own run: {len(disagree)} card(s) rated "
          f"≤1 Mbit/s then moved 411 MB at ~50 — {', '.join(sorted(disagree))}")
    worst_by_run = min(CDN_MBIT, key=lambda c: CDN_MBIT[c])
    rank = sorted(per, key=lambda c: per[c]).index(worst_by_run) + 1
    check(rank > 6,
          f"⛔ and the card the run measured slowest ({worst_by_run}) ranked {rank}th of "
          f"{len(per)} in the probe — outside the six the log printed")

# ── ⛔ IS IT CALLED? A recorder nothing invokes records nothing ──────────────────────────────────
# This project has shipped rules that were written and never called. The unit checks above would
# pass just as well with the call site deleted, so assert the wiring in tip_smoke itself.
SRC = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
check(SRC.count("tip_stage.write_probe_record(") == 1,
      f"tip_smoke calls write_probe_record exactly once ({SRC.count('tip_stage.write_probe_record(')})")
check("write_probe_record(a.rundir, probes" in SRC,
      "and passes the run directory and the accumulated map, not a fresh dict")
# ⛔ All three probe outcomes must populate it: measured, threw, untested. A map filled on only the
# happy path would silently omit exactly the candidates whose failure is the finding.
check(SRC.count("probes[c.cid] =") == 3,
      f"probes is populated on all three outcomes — measured, threw, untested "
      f"({SRC.count('probes[c.cid] =')})")
check(SRC.index("probes = {}") < SRC.index("probes[c.cid] ="),
      "and it is initialised before the probe loop writes to it")
check(SRC.index("tip_stage.write_probe_record(") > SRC.index("w_drop, w_ranked = slow_worker_cut("),
      "⚠ written AFTER the cut, so `dropped` records what was actually released")

# ── 4. the write is atomic and leaves nothing behind ────────────────────────────────────────────
check(not os.path.exists(os.path.join(RUN, tip_stage.PROBE + ".tmp")),
      "no .tmp is left behind — a half-written record would read as 'no probe data'")
raw = open(os.path.join(RUN, tip_stage.PROBE)).read()
check(json.loads(raw) == rec, "the file on disk parses to exactly what was read back")

# ── 5. absent is not empty, and a broken write does not raise ───────────────────────────────────
check(tip_stage.read_probe_record(tempfile.mkdtemp()) is None,
      "⚠ a run with no probe record reads as None, not as an empty probe")
# ⛔ Measurement must never break a run: an unserialisable value is dropped, not raised.
bad = tip_stage.write_probe_record(tempfile.mkdtemp(), {"c": {"per_worker": {1: object()}}})
check(isinstance(bad, dict), "an unserialisable probe value does not raise — a run outlives its "
                             "own telemetry")

print()
if CONTROL:
    if fails:
        print(f"FAIL (control): {len(fails)} — a truncated record must be shown to lose data")
        sys.exit(1)
    print("PASS (control): winner-only / top-6 loses the cards the argument turns on")
    sys.exit(0)
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (real)")

#!/usr/bin/env python3
"""The two legs a tip bundle travels are timed SEPARATELY, and the ledger can name the slow one.

📏 WHY (hazync#598). `stage` was **629 s of tip hour 4, 14 % of the run**, and **49-164 s on a tip
block before a single GPU starts** — against 6 s on a board block. With arming fixed (#584) it is the
largest remaining overhead that is not proving, and it lands entirely on the clocked path.

⛔ THE RUN LOG COVERS BOTH LEGS IN ONE WINDOW. A tip bundle goes

    bridge host --(ssh cat)--> orchestrator --(scp)--> aggregate pod

so a single measurement cannot say which of the two costs the time — and they have OPPOSITE fixes:
if the push dominates, send from the bridge or stage ahead; if the pull dominates, the orchestrator
should not hold the bytes at all. Building for the wrong half is the failure this guards.

    python3 test_stage_legs.py             # legs recorded apart; the verdict names the slow one
    python3 test_stage_legs.py --control   # one combined timing — the verdict must REFUSE to answer
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


RUN = tempfile.mkdtemp(prefix="stage-")
MB = 30_000_000

# Tip hour 4's shape: a ~30 MB bundle, the push much slower than the pull.
if CONTROL:
    # ⛔ THE BUG BEING GUARDED: one timing for the whole staging window, as the run log has today.
    for h, secs in ((969118, 120.0), (969119, 49.0), (969120, 110.0)):
        tip_stage.record(RUN, h, "stage", secs, MB)
else:
    for h, pull, push in ((969118, 38.0, 82.0), (969119, 15.0, 34.0), (969120, 30.0, 80.0)):
        tip_stage.record(RUN, h, "fetch", pull, MB, note="from ssh")
        tip_stage.record(RUN, h, "push", push, MB, note="-> hz-smoke-12")

LED = os.path.join(RUN, tip_stage.LEDGER)

# ── 1. it is on disk, one row per leg, append-only ──────────────────────────────────────────────
rows = tip_stage.read(LED)
check(len(rows) == (3 if CONTROL else 6), f"every transfer is a row ({len(rows)})")
check(all("leg" in r and "seconds" in r and "bytes" in r for r in rows),
      "each row carries its leg, its seconds and its bytes")

# ── 2. the summary keeps the legs apart ─────────────────────────────────────────────────────────
s = tip_stage.summary(LED)
legs = sorted(k for k in s if k != "_total")
if CONTROL:
    check(legs == ["stage"], f"control: one undifferentiated leg ({legs})")
else:
    check(legs == ["fetch", "push"], f"the two legs are separate ({legs})")
    check(abs(s["push"]["seconds"] - 196.0) < 0.01, f"push total is 196.0s (got {s['push']['seconds']})")
    check(abs(s["fetch"]["seconds"] - 83.0) < 0.01, f"fetch total is 83.0s (got {s['fetch']['seconds']})")
    # ⚠ Rate from the TOTALS, not averaged per row: averaging weights a small fast transfer equally
    # with a big slow one and flatters the number.
    check(abs(s["push"]["mbytes_per_s"] - 90_000_000 / 196.0 / 1e6) < 0.001,
          f"push rate is derived from the totals ({s['push']['mbytes_per_s']} MB/s)")

# ── 3. ⛔ THE ONE THAT MATTERS: can it name the slow leg, and can it decline? ────────────────────
v = tip_stage.verdict(LED)
print(f"       verdict: {v}")
if CONTROL:
    check("CANNOT say which leg dominates" in v,
          "⛔ control: with one combined timing the verdict REFUSES to answer — it does not crown "
          "the only leg it has")
    check("dominates —" not in v, "and does not state a winner it cannot have")
else:
    check("'push' dominates" in v, "the verdict names push as the slow leg")
    check("2.4x" in v, f"with the ratio ({v[v.find('('):v.find(')')+1] if '(' in v else '?'})")
    check("worst single transfer 82.0s at 969118" in v, "and names the worst single transfer")

# ── 3b. ⛔⛔ ONE RUN, ONE LEDGER — the bug that shipped and cost a live run ──────────────────────
#
# The two legs are recorded by DIFFERENT objects that were passing DIFFERENT directories:
#     fetch  tip_smoke   -> a.rundir           $RUNDIR/stage_ledger.jsonl
#     push   FleetRunner -> self.stage_dir     $RUNDIR/stage/stage_ledger.jsonl   (stage_dir = rundir/stage)
# so summary() and verdict() never saw both, and the one question this module exists to answer was
# structurally unanswerable. Measured on tip hour 5, 2026-09-30: two files, two rows in each.
#
# ⚠ The legs below are recorded with the SAME directories the real callers use, so this test fails if
# anyone reintroduces the split — it does not assert on a helper, it reproduces the call shape.
RUN2 = tempfile.mkdtemp(prefix="stage2-")
STAGE2 = os.path.join(RUN2, "stage")
os.makedirs(STAGE2, exist_ok=True)
if not CONTROL:
    tip_stage.use_run_dir(RUN2)          # what tip_smoke does once, where the rundir is made
tip_stage.record(RUN2, 969305, "fetch", 18.7, 29_800_000)     # the fetch caller passes rundir
tip_stage.record(STAGE2, 969305, "push", 54.8, 29_800_000)    # the push caller passes stage_dir
one = tip_stage.summary(os.path.join(RUN2, tip_stage.LEDGER))
legs2 = sorted(k for k in one if k != "_total")
if CONTROL:
    check(legs2 == ["fetch"],
          f"⛔ control: the run's ledger holds only the fetch leg {legs2} — the push went to "
          f"{STAGE2}, and the verdict can never compare them")
    check(os.path.exists(os.path.join(STAGE2, tip_stage.LEDGER)),
          "control: a SECOND ledger exists under stage/ — the split that shipped")
else:
    check(legs2 == ["fetch", "push"],
          f"⛔ both legs land in ONE ledger even though the callers pass different directories ({legs2})")
    check(not os.path.exists(os.path.join(STAGE2, tip_stage.LEDGER)),
          "and nothing is written under stage/ — there is one ledger, not two")
    check("dominates" in tip_stage.verdict(os.path.join(RUN2, tip_stage.LEDGER)),
          "so the verdict can finally answer the question the module exists for")
tip_stage.use_run_dir(None)              # ⚠ leave no global set for the checks below

# ── 4. measurement must never break a run ───────────────────────────────────────────────────────
r = tip_stage.record("/nonexistent/dir/that/cannot/be/written", 1, "push", 1.0, 1)
check(isinstance(r, dict), "an unwritable ledger returns a row rather than raising")
check(tip_stage.summary("/nonexistent/ledger.jsonl")["_total"]["n"] == 0,
      "and a missing ledger summarises to nothing rather than crashing")
check(tip_stage.verdict("/nonexistent/ledger.jsonl") == "stage: no legs recorded — nothing to conclude",
      "an empty ledger says so plainly")

# ── 5. a failed transfer is still timed — a slow failure is a finding ───────────────────────────
if not CONTROL:
    tip_stage.record(RUN, 969121, "push", 600.0, 0, ok=False, note="timed out")
    bad = [r for r in tip_stage.read(LED) if not r.get("ok")]
    check(len(bad) == 1 and bad[0]["seconds"] == 600.0,
          "a failed leg is recorded with its duration, not dropped")

print()
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

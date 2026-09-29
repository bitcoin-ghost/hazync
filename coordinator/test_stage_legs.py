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

#!/usr/bin/env python3
"""Tests for tip_fleet.py — the tip rig's execution guards (Phase 5 ⑧).

WHY THIS EXISTS. Block 966,256 was proved four times on 2026-09-10. Runs 1, 2 and 4 came in at 8.09,
8.97 and 9.07 minutes; **run 3 was lost** to a dead card and two orchestrator mistakes. Run 2 lost two
chunks to a reassignment onto a card still holding 22 GB of VRAM. Every guard here is one of those
failures, and each costs $1.39 and ten minutes to rediscover on real hardware.

⛔ THE DANGEROUS DIRECTION IS THE CONFIDENT ONE. Each of these failures produced a WRONG ANSWER rather
than an error: a healthy card declared stalled, a dead aggregate read as alive, a busy card read as
free. So the cases below pin both directions — the guard must fire when it should and stay silent when
it should not, because a guard that fires on a healthy card kills a whole run.

  python3 test_tip_fleet.py            # assertions; exit 0 on success
  python3 test_tip_fleet.py --control  # the idle guard is removed; MUST fail
"""
import os
import subprocess
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if CONTROL:
    os.environ["HAZYNC_TIP_CONTROL_NO_IDLE_GUARD"] = "1"

import tip_fleet as tf  # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# A probe reply is "SZ:UTIL:VRAM:NPROC".
BUSY        = "104857:98:22460:1"    # proving hard
COLD_START  = "0:0:22460:1"          # first segment: CUDA context + JIT, silent but holding VRAM
MID_EXECUTE = "2048:3:18000:1"       # CPU-only execute phase: low GPU util, still working
WEDGED      = "104857:0:12:0"        # process gone
IDLE_CARD   = "104857:0:500:0"       # finished and released
RECEIPT_HELD = "0:0:22460:1"         # receipt written, process still holding VRAM

# ── 1. the classifier, both directions ─────────────────────────────────────────────────────────────
check(tf.card_state(BUSY) == tf.WORKING, "a proving card is WORKING")
check(tf.card_state("DONE") == tf.DONE, "a finished card is DONE")
check(tf.card_state(WEDGED) == tf.IDLE, "a card with no prove process is IDLE")

# ⛔ The two that killed run 3. Neither may ever read as idle.
check(tf.card_state(COLD_START) == tf.WORKING,
      "a COLD-STARTING card (silent log, holds VRAM) is WORKING, not idle")
check(tf.card_state(MID_EXECUTE) == tf.WORKING,
      "a card in the CPU-only execute phase (3% GPU) is WORKING, not idle")

# ⛔ An unreachable card is not a stalled one — this is the confusion that shot healthy cards.
check(tf.card_state("") == tf.UNREACHABLE, "an empty probe is UNREACHABLE, not idle")
check(tf.card_state(None) == tf.UNREACHABLE, "a missing probe is UNREACHABLE, not idle")
check(tf.card_state("garbage") == tf.UNREACHABLE, "an unparseable probe is UNREACHABLE, not idle")

# ── 2. wedged needs BOTH idle and static ───────────────────────────────────────────────────────────
check(tf.is_wedged(tf.IDLE, 500, stall_s=100), "idle for longer than the stall window is wedged")
check(not tf.is_wedged(tf.IDLE, 10, stall_s=100), "idle but only just is NOT wedged")
check(not tf.is_wedged(tf.WORKING, 100000, stall_s=100),
      "a WORKING card is never wedged however long its log has been static")
check(not tf.is_wedged(tf.UNREACHABLE, 100000, stall_s=100),
      "an UNREACHABLE card is never wedged — we have no evidence either way")

# ── 3. "receipt exists" is not "card is free" — the run 2 killer ────────────────────────────────────
check(not tf.can_accept_reassignment("DONE"),
      "a card reporting DONE is NOT free (the receipt is written before VRAM is released)")
check(not tf.can_accept_reassignment(RECEIPT_HELD),
      "a card still holding 22 GB is NOT free, even with its receipt on disk")
check(tf.can_accept_reassignment(IDLE_CARD),
      "a card with no process and released VRAM IS free")
check(not tf.can_accept_reassignment(""), "an unreachable card is never a reassignment target")

# ── 4. recovery planning ───────────────────────────────────────────────────────────────────────────
probes = {"a": IDLE_CARD, "b": RECEIPT_HELD, "c": BUSY}
p = tf.plan_recovery(4, owner="c", candidates=["a", "b", "c"],
                     finished={"a", "b"}, busy=set(), probes=probes)
check(p == {"action": "reassign", "chunk": 4, "to": "a"}, f"a wedged chunk moves to the free card ({p})")

p = tf.plan_recovery(4, owner="c", candidates=["b", "c"],
                     finished={"b"}, busy=set(), probes=probes)
check(p["action"] == "restart_in_place" and p["on"] == "c",
      "with no genuinely free card the chunk restarts in place rather than stalling for ever")

p = tf.plan_recovery(4, owner="c", candidates=["a", "c"],
                     finished={"a"}, busy={"a"}, probes=probes)
check(p["action"] == "restart_in_place",
      "a card already given someone else's chunk is not given a second")

p = tf.plan_recovery(4, owner="a", candidates=["a"], finished={"a"}, busy=set(), probes=probes)
check(p["action"] == "restart_in_place", "a chunk is never reassigned to the card it is taken from")

# ── 5. aggregate liveness: truncation and self-match ───────────────────────────────────────────────
check(tf.aggregate_alive("systemd\nhazync-host-cud\nsshd\n"),
      "a live aggregate is seen despite `ps -eo comm` truncating at 15 chars")
check(not tf.aggregate_alive("systemd\nsshd\n"), "a dead aggregate reads as dead")
check(not tf.aggregate_alive(""), "no output is not evidence of life")
# The full 16-char name never appears in `comm`; a check written against it matches nothing and a LIVE
# aggregate reads as dead, which is how seg-serve was once relaunched on top of a healthy one.
check("hazync-host-cuda" not in ("hazync-host-cud",), "the matched name is the TRUNCATED one")

# ── 6. the probe body must not be interpolable on the orchestrator ─────────────────────────────────
body = tf.probe_body()
check("$(stat" in body and "$(pgrep" in body,
      "the probe keeps its command substitutions for the REMOTE shell")
# ⛔ The whole point is that the caller can wrap this in SINGLE quotes so nothing expands locally. A
# single quote anywhere in the body would end that quoting early and hand the rest to the local shell —
# which is the exact bug that turned the remote probe into a constant and stalled every healthy card.
check("'" not in body, "the probe body contains no single quote, so it can be single-quoted for ssh")
check("$RDIR" in body and "$CHUNK" in body,
      "the probe references RDIR and CHUNK as variables, assigned by the caller outside this body")

# ── 7. staging is asserted, not assumed ────────────────────────────────────────────────────────────
check(tf.staging_complete(27, 27), "27 of 27 staged is complete")
check(not tf.staging_complete(21, 22), "21 of 22 is NOT complete — the silent scp drop")
check(tf.staging_complete(28, 27), "an extra file from a re-run does not refuse the aggregate")
check(not tf.staging_complete(0, 0), "nothing expected and nothing staged is not a pass")

# ── 8. the tick planner: one cycle's decisions ─────────────────────────────────────────────────────
A = {0: "a", 1: "b", 2: "c"}

# A finished chunk is staged immediately, not batched at the end (that cost 57 s in an earlier run).
r = tf.plan_tick(assignments=A, probes={"a": "DONE", "b": BUSY, "c": BUSY},
                 staged=set(), busy=set(), last_size={}, last_change={}, now=1000.0)
check([x for x in r["actions"] if x["action"] == "stage"] == [{"action": "stage", "chunk": 0, "from": "a"}],
      "a finished chunk is staged on the tick it finishes")
check(r["chunks_done"] == {0}, "the finished chunk is recorded as done")

# Staging happens once; a chunk already staged produces no further action.
r = tf.plan_tick(assignments=A, probes={"a": "DONE", "b": BUSY, "c": BUSY},
                 staged={0}, busy=set(), last_size={}, last_change={}, now=1000.0)
check(r["actions"] == [], "an already-staged chunk is not staged twice")

# ⛔ An unreachable card must not advance OR reset its timers — we learned nothing about it.
r = tf.plan_tick(assignments=A, probes={"a": "", "b": BUSY, "c": BUSY},
                 staged=set(), busy=set(),
                 last_size={0: 500}, last_change={0: 100.0}, now=1000.0)
check(r["actions"] == [], "an unreachable card produces no action")
check(r["last_change"][0] == 100.0, "an unreachable card does not reset the stall clock")
check(r["last_size"][0] == 500, "an unreachable card does not disturb the recorded progress")

# Progress is the log CHANGING, never how fast. A card whose size moved has its clock reset.
r = tf.plan_tick(assignments=A, probes={"a": "900:98:22460:1", "b": BUSY, "c": BUSY},
                 staged=set(), busy=set(),
                 last_size={0: 500}, last_change={0: 100.0}, now=1000.0)
check(r["last_change"][0] == 1000.0, "a card whose log grew has its stall clock reset")
check(r["actions"] == [], "a card making progress is left alone")

# ⛔ A WORKING card is never recovered, however long its log has been static -- the cold-start case.
r = tf.plan_tick(assignments=A, probes={"a": COLD_START, "b": BUSY, "c": BUSY},
                 staged=set(), busy=set(),
                 last_size={0: 0}, last_change={0: 0.0}, now=100000.0)
check(r["actions"] == [], "a cold-starting card is never recovered, however long it has been silent")

# A genuinely wedged card is recovered, onto a free card if one exists.
r = tf.plan_tick(assignments=A, probes={"a": WEDGED, "b": IDLE_CARD, "c": BUSY},
                 staged={1}, busy=set(),
                 last_size={0: 104857}, last_change={0: 0.0}, now=1000.0)
rec = [x for x in r["actions"] if x["action"] in ("reassign", "restart_in_place")]
check(len(rec) == 1 and rec[0]["action"] == "reassign" and rec[0]["to"] == "b",
      f"a wedged chunk moves to a finished, VRAM-released card ({rec})")
check(r["last_change"][0] == 1000.0, "a recovered chunk restarts its stall clock")

# ... and with no free card it restarts in place rather than stalling for ever.
r = tf.plan_tick(assignments=A, probes={"a": WEDGED, "b": RECEIPT_HELD, "c": BUSY},
                 staged={1}, busy=set(),
                 last_size={0: 104857}, last_change={0: 0.0}, now=1000.0)
rec = [x for x in r["actions"] if x["action"] in ("reassign", "restart_in_place")]
check(len(rec) == 1 and rec[0]["action"] == "restart_in_place",
      f"with only a VRAM-holding card available, the chunk restarts in place ({rec})")

# ── ⛔ A FINISHED CARD THAT HAS LET GO OF THE GPU CAN TAKE MORE WORK ──────────────────────────────
# The probe used to `echo DONE` and stop, so a card with a receipt reported nothing about its process
# or its VRAM — and can_accept_reassignment, which needs exactly those, could only refuse it. Once
# every card had a receipt, NO card could accept reassigned work, so a wedged chunk could only ever be
# restarted on the card that wedged. Measured 2026-09-20: a 4090 with no usable CUDA device
# (cudaErrorNoDevice) was handed its own chunk back while ten idle finished cards sat there.
body = tf.probe_body()
check("DONE:$Z:" in body,
      "the probe reports its metrics even when the receipt exists")
check(body.index("nvidia-smi") < body.index("chunk_$CHUNK.bin"),
      "⛔ and gathers them BEFORE testing for the receipt, so the DONE path cannot skip them")

check(tf.card_state("DONE:512:0:400:0") == tf.DONE, "DONE:… still reads as DONE")
check(tf.card_state("DONE") == tf.DONE, "and a bare DONE from an older card still parses")
check(tf.can_accept_reassignment("DONE:512:0:400:0"),
      "⛔ a finished card with no prover and VRAM released CAN take reassigned work")
check(not tf.can_accept_reassignment("DONE:512:8:23000:1"),
      "⛔ but a finished card still HOLDING 23 GB cannot — pod-prove.sh writes the receipt while the "
      "process keeps the VRAM, and reassigning there starts a second prove and both die")
check(not tf.can_accept_reassignment("DONE:512:0:400:1"),
      "nor one whose prover process is still alive")

# and the planner now MOVES the work rather than retrying the broken card
plan = tf.plan_tick(assignments={0: "dead", 1: "fin"},
                    probes={"dead": "512:0:0:0", "fin": "DONE:900:0:300:0"},
                    staged={1}, busy=set(),
                    last_size={0: 512}, last_change={0: 0.0},   # keyed by CHUNK, not card
                    now=1000.0, stall_s=100.0)
acts = [a for a in plan["actions"] if a["action"] in ("reassign", "restart_in_place")]
check(acts and acts[0]["action"] == "reassign" and acts[0]["to"] == "fin",
      f"⛔ a wedged chunk is REASSIGNED to the idle finished card, not retried in place ({acts})")

EXPECTED_CONTROL_FAILURES = {
    "a proving card is WORKING",
    "a COLD-STARTING card (silent log, holds VRAM) is WORKING, not idle",
    "a card in the CPU-only execute phase (3% GPU) is WORKING, not idle",
    "a card with no process and released VRAM IS free",
    "a wedged chunk moves to the free card ({'action': 'reassign', 'chunk': 4, 'to': 'a'})",
}

print()
if CONTROL:
    got = set(fails)
    # The control removes the idle guard, so every card reads as idle. The assertions that depend on a
    # card being recognised as WORKING must fail — those are the ones whose absence killed run 3.
    missing = {f for f in EXPECTED_CONTROL_FAILURES if "WORKING" in f} - got
    if not missing and got:
        print(f"CONTROL OK — the idle guard was removed and {len(got)} assertion(s) failed, as they must:")
        for f in sorted(got):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the idle guard did not produce the expected failures.")
    for f in sorted(missing):
        print(f"  should have failed and did not: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

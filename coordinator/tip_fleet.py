#!/usr/bin/env python3
"""The tip rig's execution layer: drive one block across a fleet without a human (Phase 5 ⑧).

`tip_controller.py` decides *how many* cards to run and *which* to drop. This module decides what to do
with a card **right now** — is it working, finished, wedged or merely unreachable, and may another card
take its chunk.

⛔ EVERY GUARD BELOW EXISTS BECAUSE A RUN DIED WITHOUT IT. Block 966,256 was proved four times on
2026-09-10: runs 1, 2 and 4 came in at 8.09, 8.97 and 9.07 minutes, and **run 3 was lost** — a dead card
plus two orchestrator mistakes put it past 15 minutes. Run 2 lost two chunks to a reassignment that
started a second prove on a card still holding 22 GB of VRAM. The fourteen checks in
`tools/milestone/run_continuous.sh` are the result, and this module is that state machine made testable.

⛔ THE GUARDS ARE ONLY CORRECT IN THEIR SPECIFIC FORMS. `pgrep -f seg-serve` self-matches the shell
running it; `ps -eo comm` truncates at 15 characters; "a receipt exists" is not "the card is free". Each
one reads like something a tidy-up would simplify, and each simplification is a run that dies. The tests
in `test_tip_fleet.py` fail without them for exactly that reason — do not relax one without replacing its
case.

⚠ NOTHING HERE OPENS A SOCKET OR RUNS A COMMAND. It is pure so the failure modes can be tested without a
fleet, because a fleet costs $1.39 and ten minutes per attempt. The SSH layer that feeds it is thin by
design and lives in the caller.
"""

import os

# A card is judged wedged only after its log has been static this long AND it looks idle. 100 s comes from
# run_continuous.sh, where it survived four runs without a false positive.
STALL_S = float(os.environ.get("HAZYNC_TIP_STALL_S", "100"))

# Below this GPU utilisation AND this VRAM, a card is doing nothing. Both must hold: a card mid-execute
# reports low utilisation while legitimately holding its working set, and a card that has written its
# receipt still holds VRAM until the process exits.
IDLE_UTIL_PCT = float(os.environ.get("HAZYNC_TIP_IDLE_UTIL", "5"))
IDLE_VRAM_MIB = float(os.environ.get("HAZYNC_TIP_IDLE_VRAM", "2000"))

# test_tip_fleet.py --control removes one guard to show the tests can fail. Never set this otherwise.
_CONTROL_NO_IDLE_GUARD = os.environ.get("HAZYNC_TIP_CONTROL_NO_IDLE_GUARD") == "1"

DONE, WORKING, IDLE, UNREACHABLE = "done", "working", "idle", "unreachable"


def card_state(probe):
    """Classify one card from its probe reply.

    `probe` is exactly what the remote command printed: `DONE`, or `SZ:UTIL:VRAM:NPROC`, or empty.

    ⛔ AN UNREACHABLE CARD IS NOT A STALLED ONE. An empty reply means the ssh failed — the card may be
    proving perfectly. Run 3 killed healthy cards on exactly this confusion. Unreachable is its own state
    and the caller must leave the card's timers alone and look again next tick.
    """
    if probe is None:
        return UNREACHABLE
    probe = probe.strip()
    if not probe:
        return UNREACHABLE
    if probe == "DONE" or probe.startswith("DONE:"):
        return DONE

    parts = probe.split(":")
    if len(parts) != 4:
        # Anything we cannot parse is a probe we do not understand, which is not evidence of a wedge.
        return UNREACHABLE
    try:
        _size, util, vram, nproc = (float(p) if p else 0.0 for p in parts)
    except ValueError:
        return UNREACHABLE

    # ⛔ NO PROXY FOR "BUSY". Three runs were wrecked by inferring a stall from how fast prove.log grew:
    # first a byte-count threshold that killed cards mid-execute, then a tighter limit keyed on "proving
    # has begun" — which fires during the first segment, where CUDA context creation and kernel JIT make
    # a cold card silent for ~2 minutes. A working card says so directly: its process is alive, it holds
    # VRAM, and the GPU reports utilisation.
    if _CONTROL_NO_IDLE_GUARD:
        return IDLE
    if nproc < 1:
        return IDLE
    if util < IDLE_UTIL_PCT and vram < IDLE_VRAM_MIB:
        return IDLE
    return WORKING


def is_wedged(state, static_s, stall_s=None):
    """May this card's chunk be taken away from it?

    Both conditions, never one: it must look idle AND have produced nothing for `stall_s`. A card that is
    merely quiet is still proving; a card that is idle but only just went quiet may be between segments.
    """
    stall_s = STALL_S if stall_s is None else stall_s
    return state == IDLE and static_s > stall_s


def can_accept_reassignment(probe):
    """Is this card genuinely free to take another chunk?

    ⛔ "RECEIPT EXISTS" IS NOT "CARD IS FREE", and this is the one that cost run 2 two chunks.
    `pod-prove.sh` writes `chunk_N.bin` near the end but the process keeps ~22 GB of VRAM until it
    exits. Reassigning onto such a card starts a second prove on top of the first and both die with the
    hazync#97 memory failure (`rc=101`, "drop a rung ... SEG_PO2=20"). Require the card to be idle by
    measurement — no prove process AND VRAM released — never by inference from its output.
    """
    if card_state(probe) == UNREACHABLE:
        return False
    parts = probe.strip().split(":")
    # A finished card now reports `DONE:SZ:UTIL:VRAM:NPROC`; drop the marker and judge the metrics.
    # ⛔ THE ORIGINAL POINT STILL STANDS: a receipt is NOT evidence the card is free -- pod-prove.sh
    # writes chunk_N.bin while the process still holds ~22 GB, and reassigning there starts a second
    # prove on top of the first and both die (hazync#97). The difference is that we now MEASURE
    # whether it has let go, instead of refusing every finished card for ever.
    if parts and parts[0] == "DONE":
        parts = parts[1:]
    if len(parts) != 4:
        return False
    try:
        _size, _util, vram, nproc = (float(p) if p else 0.0 for p in parts)
    except ValueError:
        return False
    return nproc == 0 and vram < IDLE_VRAM_MIB


def plan_recovery(chunk, *, owner, candidates, finished, busy, probes):
    """What to do about a wedged chunk: move it to a free card, or restart it where it is.

    `candidates` are card ids; `finished` are those whose own chunk is done; `busy` are those already
    given someone else's chunk this run; `probes` maps card id to its latest probe reply.

    ⛔ RESTART IN PLACE IS A FIRST-CLASS OUTCOME, NOT A FAILURE PATH. With no free card the chunk used to
    stay pinned to the wedged card with nothing logged at all, and the run could then never finish. A
    hazync#147 wedge is transient, so restarting in place recovers it; reassignment is for a card that is
    genuinely gone. Returning "restart_in_place" is the common case on a small fleet and must not be
    treated as an error by the caller.
    """
    for card in candidates:
        if card == owner or card in busy or card not in finished:
            continue
        if can_accept_reassignment(probes.get(card)):
            return {"action": "reassign", "chunk": chunk, "to": card}
    return {"action": "restart_in_place", "chunk": chunk, "on": owner}


def aggregate_alive(ps_comm_output):
    """Is the aggregate still running, from `ps -eo comm` output?

    ⛔ TWO WAYS TO GET THIS WRONG, AND BOTH HAVE HAPPENED.
    `pgrep -cf seg-serve` SELF-MATCHES — the remote shell's own command line contains the string, so it
    never returns 0 and a dead aggregate reads as alive.
    `ps -eo comm` TRUNCATES AT 15 CHARACTERS, so grepping `^hazync-host-cuda$` (16) matches nothing and a
    LIVE aggregate reads as dead. That mistake relaunched `seg-serve` on top of a healthy one, which then
    died on `bind()` while the original kept working with its log unlinked.

    So: match the TRUNCATED name, against `comm` output only.
    """
    if not ps_comm_output:
        return False
    want = "hazync-host-cud"          # 15 chars — what `comm` actually shows
    return any(line.strip().startswith(want) for line in ps_comm_output.splitlines())


def probe_body():
    """The remote probe, as a SINGLE-QUOTED shell body.

    ⛔ THIS MUST NEVER BE INTERPOLATED ON THE ORCHESTRATOR. Written in double quotes, `$(stat ...)` and
    `$(pgrep ...)` expand on the *laptop* before ssh runs, so the remote command becomes a constant, the
    log size never changes, and every healthy card is declared stalled at the same moment. That killed a
    chunk five minutes into run 3 and was working down the fleet when the driver was stopped.

    The caller passes RDIR and CHUNK as assignments *before* this body, never inside it.
    """
    # ⛔ THE METRICS ARE REPORTED EVEN WHEN THE RECEIPT EXISTS. This used to `echo DONE` and stop, so a
    # card that had finished reported NOTHING about its process or its VRAM -- and
    # can_accept_reassignment, which needs exactly those, could only refuse it. The consequence was
    # that once every card had a receipt no card could ever take reassigned work, so a wedged chunk
    # could only ever be restarted ON THE CARD THAT WEDGED. Measured 2026-09-20: a 4090 with no usable
    # CUDA device (`cudaErrorNoDevice`) was handed its own chunk back instead of the work moving to any
    # of the ten idle cards that had already finished.
    #
    # `DONE:` is kept as a prefix so the state stays readable at a glance and old replies still parse.
    return (
        'Z=$(stat -c%s "$RDIR/prove.log" 2>/dev/null || echo 0); '
        'U=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1); '
        'V=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1); '
        'N=$(pgrep -cf hazync-host-cuda); '
        'if [ -f "$RDIR/chunk_$CHUNK.bin" ]; then echo "DONE:$Z:${U:-0}:${V:-0}:$N"; '
        'else echo "$Z:${U:-0}:${V:-0}:$N"; fi'
    )


def staging_complete(staged, expected):
    """Every chunk must be on the coordinator before the aggregate starts.

    ⛔ A SILENT scp DROP IS THE FAILURE THIS CATCHES. One run staged 21 of 22 and `seg-serve` panicked
    with no useful message. The count is asserted, not assumed, and `>=` is deliberate: a re-run can
    leave an extra file and that is not a reason to refuse.
    """
    return isinstance(staged, int) and staged >= expected > 0


def probe_size(probe):
    """The prove.log byte count a probe reports, or None when it reports nothing usable.

    Progress is judged by this number MOVING, never by how fast it moves. The rate is the proxy that
    killed healthy cards; the fact of change is evidence, its speed is not.
    """
    if card_state(probe) not in (WORKING, IDLE):
        return None
    try:
        return int(float(probe.strip().split(":")[0]))
    except (ValueError, IndexError):
        return None


def plan_tick(*, assignments, probes, staged, busy, last_size, last_change, now, stall_s=None):
    """Decide everything for one poll cycle. Pure: it returns actions and the next state, and performs
    nothing.

    `assignments` maps chunk -> card. `staged` are chunks whose receipt is already collected. `busy` are
    cards that have taken someone else's chunk. `last_size`/`last_change` carry per-chunk progress
    between ticks.

    ⛔ THE TICK MUST BE CHEAP. This was once 27 sequential ssh round-trips at ~0.7 s each, so a tick took
    ~20 s and the last chunk of run 4 sat FINISHED and unnoticed for 24.9 s — the single largest
    recoverable waste in that run, bigger than anything left in the chunk phase. The caller fans the
    probes out in parallel and hands the results here all at once; this function never waits on anything.
    """
    stall_s = STALL_S if stall_s is None else stall_s
    actions, finished = [], set(staged)
    size, change = dict(last_size), dict(last_change)

    # Which cards have finished their OWN chunk — the only ones that may take another.
    for chunk, card in assignments.items():
        if chunk in finished:
            continue
        if card_state(probes.get(card)) == DONE:
            finished.add(chunk)

    for chunk, card in assignments.items():
        if chunk in staged:
            continue
        state = card_state(probes.get(card))

        if state == DONE:
            # ⛔ STAGE NOW, NOT AT THE END. Batching every receipt after the last chunk cost 57 s of dead
            # time in an earlier run. Collecting each one as it appears overlaps the transfer with the
            # cards still proving, which is why run 4 staged in 5.8 s after its chunk phase.
            actions.append({"action": "stage", "chunk": chunk, "from": card})
            continue

        if state == UNREACHABLE:
            # ⛔ LEAVE THE TIMERS ALONE. We learned nothing, so pretending we did — in either direction —
            # is the mistake. Neither progress nor a stall may be inferred from a failed ssh.
            continue

        sz = probe_size(probes.get(card))
        if sz is not None and sz != size.get(chunk):
            size[chunk] = sz
            change[chunk] = now

        static_for = now - change.get(chunk, now)
        if is_wedged(state, static_for, stall_s):
            plan = plan_recovery(chunk, owner=card,
                                 candidates=list(assignments.values()),
                                 finished={assignments[c] for c in finished if c in assignments},
                                 busy=busy, probes=probes)
            plan["static_s"] = round(static_for, 1)
            actions.append(plan)
            # A recovered chunk starts its clock again, wherever it ended up.
            change[chunk] = now
            size[chunk] = 0

    return {"actions": actions, "last_size": size, "last_change": change,
            "chunks_done": finished}

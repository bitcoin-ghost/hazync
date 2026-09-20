#!/usr/bin/env python3
"""One block, start to receipt, without a human (Phase 5 ⑧).

The phase order is `tools/milestone/run_continuous.sh`, which is the only sequence that has ever produced
a near-tip block under ten minutes — three times, on 2026-09-10. It is reproduced here rather than
redesigned, because every gate in it is a run that died without it.

    0  GATE     every pod cleared AND VERIFIED EMPTY        <- outside the clock: setup, not proving
    1  T0       launch all chunks, in parallel
    2           arm auto-attach so cards cost ~0 to join when the listener opens
    3  POLL     probe in parallel -> plan_tick -> act; receipts stream as they appear
    4  GATHER   sweep anything whose streaming retries failed
    5  GATE     staged == N, asserted
    6  AGGREGATE start seg-serve, wait for VERIFIED

⛔ THE CLOCK STARTS AT PHASE 1, NOT PHASE 0. Clearing the fleet is setup; counting it would flatter every
figure this produces. run_continuous.sh is explicit about it and so is this.

⛔ PHASE 0 IS NOT OPTIONAL AND ITS FAILURE IS NOT A WARNING. A pod carrying a stale `chunk_N.bin` from a
previous run reports DONE in 63 seconds, its receipt is collected, and the aggregate fails — or worse,
succeeds against the wrong work. Every pod is verified empty and the run refuses otherwise.

Injectable `runner`, `now` and `sleep` so the whole sequence can be driven by a test at $0 instead of
$1.39 and ten minutes.
"""

import tip_fleet


class RunRefused(RuntimeError):
    """A gate said no. The message says which, and the run has not started."""


def verify_fleet_empty(runner, cards):
    """Phase 0. Every pod must report LEFT:0 REDIRS:0 — and we must have HEARD from every one.

    ⛔ AN UNREACHABLE POD IS NOT A CLEAN POD. Counting only the ones that answered would let a pod we
    could not talk to carry a stale receipt into the run, which is the failure this phase exists for.
    """
    clean, dirty, silent = [], [], []
    for cid, reply in runner.clear_and_check(cards).items():
        if reply is None:
            silent.append(cid)
        elif "LEFT:0 REDIRS:0" in reply:
            clean.append(cid)
        else:
            dirty.append(cid)
    return {"clean": sorted(clean), "dirty": sorted(dirty), "silent": sorted(silent),
            "ok": not dirty and not silent and len(clean) == len(cards)}


def run_block(*, block, cards, runner, now, sleep, stall_s=None, max_ticks=1200, tick_s=3.0):
    """Drive one block to a verified receipt. Returns a summary dict.

    `cards` maps chunk -> card. `runner` supplies the remote actions. `now`/`sleep` are injected so a
    test can run the whole sequence instantly.
    """
    n = len(cards)
    if n == 0:
        raise RunRefused("no cards")

    # ── phase 0 ────────────────────────────────────────────────────────────────────────────────────
    empty = verify_fleet_empty(runner, cards)
    if not empty["ok"]:
        raise RunRefused(
            f"fleet not verified empty: {len(empty['clean'])}/{n} clean, "
            f"dirty={empty['dirty']}, unreachable={empty['silent']} — a stale chunk_N.bin reports DONE "
            f"in seconds and poisons the aggregate")

    # ── phase 1: THE CLOCK STARTS ──────────────────────────────────────────────────────────────────
    t0 = now()
    runner.launch_all(cards, block=block, chunks=n)
    runner.arm_auto_attach(cards)

    staged, busy, reassigned = set(), set(), set()
    last_size, last_change = {}, {c: t0 for c in cards}
    events = []

    for _ in range(max_ticks):
        probes = runner.probe_all(cards, reassigned=reassigned)
        plan = tip_fleet.plan_tick(assignments=cards, probes=probes, staged=staged, busy=busy,
                                   last_size=last_size, last_change=last_change,
                                   now=now(), stall_s=stall_s)
        last_size, last_change = plan["last_size"], plan["last_change"]

        for act in plan["actions"]:
            if act["action"] == "stage":
                # Streamed during the chunk phase, not batched at the end -- batching cost 57 s once.
                if runner.stage_receipt(cards[act["chunk"]], act["chunk"]):
                    staged.add(act["chunk"])
            elif act["action"] == "reassign":
                runner.kill_provers(cards[act["chunk"]])
                target = act["to"]
                runner.relaunch(target, act["chunk"], block=block, chunks=n, workdir_suffix=act["chunk"])
                cards[act["chunk"]] = target
                busy.add(target)
                reassigned.add(act["chunk"])
                events.append(f"chunk {act['chunk']} reassigned after {act['static_s']}s static")
            elif act["action"] == "restart_in_place":
                runner.kill_provers(cards[act["chunk"]])
                runner.relaunch(cards[act["chunk"]], act["chunk"], block=block, chunks=n,
                                workdir_suffix=act["chunk"])
                reassigned.add(act["chunk"])
                events.append(f"chunk {act['chunk']} restarted in place after {act['static_s']}s static")

        if len(staged) >= n:
            break
        sleep(tick_s)

    chunk_phase_s = round(now() - t0, 1)

    # ── phase 4: sweep anything the streaming retries failed to collect ────────────────────────────
    for chunk, card in cards.items():
        if chunk not in staged and runner.stage_receipt(card, chunk):
            staged.add(chunk)

    # ── phase 5: the gate that a silent scp drop fails ────────────────────────────────────────────
    count = runner.staged_count()
    if not tip_fleet.staging_complete(count, n):
        raise RunRefused(
            f"coordinator has {count}/{n} chunks — refusing to aggregate. A silent scp drop once staged "
            f"21 of 22 and seg-serve panicked with no useful message")

    # ── phase 6 ───────────────────────────────────────────────────────────────────────────────────
    runner.start_aggregate(block=block, chunks=n)
    for _ in range(max_ticks):
        out = runner.aggregate_status()
        if out.get("verified"):
            return {"ok": True, "block": block, "cards": n,
                    "wall_s": round(now() - t0, 1), "chunk_phase_s": chunk_phase_s,
                    "digest": out.get("digest"), "events": events}
        if not out.get("alive", True):
            raise RunRefused("the aggregate died before verifying — see agg.err on the coordinator")
        sleep(tick_s)

    raise RunRefused("the aggregate never verified within the tick budget")

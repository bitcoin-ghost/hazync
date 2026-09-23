#!/usr/bin/env python3
"""A block is done when it is done, and its work belongs to it (hazync#498, #499).

⛔ WHY THIS EXISTS. Two defects with one shape: the frame's per-block figures rest entirely on the
`block` column of the telemetry, and that column is sparse.

  #498  tip-stream.sh reads the height off the `RANGE [n..n]` banner inside `tail -c 40000` of the
        prover log. As the log grows the banner scrolls OUT of that window, so the column goes empty
        part-way through a block and every later row was dropped -- silently, because a row with no
        height just `continue`s. Measured on the 968,243/968,255 run: of 15,483 `assembling` rows,
        19 named 968,243 and 18 named 968,255; the other 15,446 named nothing. Fold time was built
        from ~18 samples instead of thousands, so the folding bar drew as nothing at all.

  #499  `done` fell back to "no card has named this height for 5 seconds". That rule is for the END
        of a run -- the collector stops before its own grace period elapses, so the last block would
        never count. But it also fired MID-BLOCK, in the handover from proving to assembling.
        Measured on 968,255: done_at fired 09:55:14 UTC, the run logged `verified` at 09:56:56. The
        tile went green and the ring read VERIFIED 102 SECONDS EARLY while the frame still showed
        FOLDING 89%. No card ever reported phase=done for that height -- silence alone said so.

  python3 test_block_attribution.py            # must PASS
  python3 test_block_attribution.py --control  # both fallbacks restored; MUST FAIL
"""
import os
import sys
import tempfile
import time

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import collect                                                                  # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


NOW = time.time()


def capture(rows, name="hz-smoke-12"):
    """rows: (age_s, phase, block) — written oldest first, exactly as tip-stream.sh emits them."""
    d = tempfile.mkdtemp(prefix="attr_")
    os.makedirs(os.path.join(d, "stream"))
    with open(os.path.join(d, "pods.txt"), "w") as p:
        p.write(f"pod0 {name} 10.0.0.1 22000 0.4900 NVIDIA_A40\n")
    with open(os.path.join(d, "stream", f"{name}.csv"), "w") as fh:
        for age, phase, blk in rows:
            fh.write(f"{NOW - age:.6f},95,4000,60,146.0,2000,7251,{phase},100,2215,{blk},0,0\n")
    return d


# ⛔ THE CONTROLS RESTORE THE ORIGINAL LINES, in the shipped module.
if CONTROL:
    _orig_read_streams = collect.read_streams
    _orig_blocks = collect.blocks_from_cards

    def drop_unnamed(rundir, now, cursors=None):
        """CONTROL (#498): a row with no height is dropped instead of inheriting the last one."""
        cards = _orig_read_streams(rundir, now, cursors)
        for c in cards:
            bb = c.get("block_s") or {}
            for h, e in bb.items():
                # undo the carry-forward ENTIRELY: keep only what the raw column actually named,
                # for prove as well as asm. Rewriting just one of them let the other ride along
                # green and understated how far the defect reached.
                asm = prove = 0
                path = os.path.join(rundir, "stream", f"{c['name']}.csv")
                for line in open(path):
                    f = line.split(",")
                    if len(f) >= 11 and f[10].strip() == h:
                        if f[7] == "assembling":
                            asm += 1
                        elif f[7] in ("proving", "executed"):
                            prove += 1
                e["asm"], e["prove"] = asm, prove
        return cards

    def silence_only(cards, state, now, verified=()):
        """CONTROL (#499): silence alone marks a block done, whatever the fleet is doing."""
        out = _orig_blocks(cards, state, now, verified)
        for b in out:
            for c in cards:
                e = (c.get("block_s") or {}).get(str(b["h"]))
                if e and (now - e["t1"]) > 5:
                    b["done"], b["done_at"] = True, e["t1"]
        return out

    collect.read_streams = drop_unnamed
    collect.blocks_from_cards = silence_only


# ── 1. ⛔ #498. The banner scrolls out: 3 named rows, then 40 with an empty column ────────────────
rows = [(300 - i, "proving", "968255" if i < 3 else "") for i in range(20)]
rows += [(280 - i, "assembling", "") for i in range(40)]      # the whole fold, unnamed
d = capture(rows)
cards = collect.read_streams(d, NOW, {})
bs = (cards[0].get("block_s") or {}) if cards else {}
e = bs.get("968255", {})

check("968255" in bs, f"the block is tracked at all (heights seen: {sorted(bs)})")
check(e.get("asm", 0) >= 40,
      f"all 40 assembling samples are attributed to it, not just the named ones "
      f"(got {e.get('asm', 0)}) — this is what draws the folding bar")
check(e.get("prove", 0) >= 20,
      f"and all 20 proving samples too (got {e.get('prove', 0)})")

# ── 2. ⚠ the carry-forward must STOP at idle — an idle card has finished with that height ────────
rows = [(300 - i, "proving", "968255") for i in range(10)]
rows += [(290 - i, "idle", "") for i in range(5)]
rows += [(285 - i, "proving", "") for i in range(5)]          # a NEW block, height not yet seen
d = capture(rows)
bs = (collect.read_streams(d, NOW, {})[0].get("block_s") or {})
check(bs.get("968255", {}).get("prove", 0) == 10,
      f"work after an idle gap is NOT charged to the old block "
      f"(got {bs.get('968255', {}).get('prove', 0)}, expected 10)")

# ── 3. ⛔ #499 THE CENTRAL CASE. Silent for 10s, but cards are still assembling: NOT done ─────────
cards = [{"name": "hz-smoke-12", "cost_hr": 0.49, "phase": "assembling", "up": True,
          "block": 968255,
          "block_s": {"968255": {"t0": NOW - 800, "t1": NOW - 10, "segs": 2215,
                                 "prove": 600, "asm": 180, "n": 780}}}]
b = collect.blocks_from_cards(cards, {"since": 0.0}, NOW, set())[0]
check(not b["done"],
      f"10s of silence while a card is ASSEMBLING is a handover, not a finish (done={b['done']})")
check(b["done_at"] is None,
      f"so nothing goes green and nothing claims VERIFIED (done_at={b['done_at']})")

# ── 3b. ⛔ A FINISHED BLOCK MUST SURVIVE THE FLEET MOVING ON ──────────────────────────────────────
# The first version of the #499 guard asked "is ANY card working", fleet-wide. So the moment the run
# claimed the next height, the block it had just finished reverted to done=False: its tile went green
# -> orange and "blocks today" fell 1 -> 0. Observed live on 968,257 (verified 11:14:43, reading
# `0 blocks` by 11:18:41). The guard has to be about THIS block, not about the fleet.
two = [{"name": "hz-smoke-1", "cost_hr": 0.72, "phase": "proving", "up": True, "block": 968264,
        "block_s": {"968257": {"t0": NOW - 2000, "t1": NOW - 300, "segs": 7846,
                               "prove": 1200, "asm": 240, "n": 1800},
                    "968264": {"t0": NOW - 200, "t1": NOW - 0.5, "segs": 4184,
                               "prove": 190, "asm": 0, "n": 200}}},
       {"name": "hz-smoke-2", "cost_hr": 0.72, "phase": "proving", "up": True, "block": None,
        "block_s": {"968264": {"t0": NOW - 200, "t1": NOW - 0.5, "segs": 4184,
                               "prove": 190, "asm": 0, "n": 200}}}]
bl = {b["h"]: b for b in collect.blocks_from_cards(two, {"since": 0.0}, NOW, set())}
check(bl[968257]["done"],
      f"the block the fleet has FINISHED stays done once it moves to the next "
      f"(done={bl[968257]['done']})")
check(not bl[968264]["done"],
      f"and the block it moved TO is not done (done={bl[968264]['done']})")

# ── 4. the end of a run: cards stopped, silence now DOES mean done (the rule's real purpose) ─────
cards[0]["phase"], cards[0]["up"] = "idle", False
b = collect.blocks_from_cards(cards, {"since": 0.0}, NOW, set())[0]
check(b["done"] and b["done_at"] is not None,
      f"with nothing working, silence still closes the last block of a run (done={b['done']})")

# ── 5. and the prover's own word still wins immediately, whatever the fleet is doing ─────────────
cards[0]["phase"], cards[0]["up"] = "done", True
cards[0]["block_s"]["968255"]["t1"] = NOW - 1
b = collect.blocks_from_cards(cards, {"since": 0.0}, NOW, set())[0]
check(b["done"], "a card reporting phase=done marks it done at once, with no silence needed")

EXPECTED_CONTROL = {
    "all 40 assembling samples are attributed",
    "and all 20 proving samples too",
    "10s of silence while a card is ASSEMBLING",
    "so nothing goes green and nothing claims VERIFIED",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — the fold drew from a handful of samples, and a block went green while "
              "its cards were still folding:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly {len(EXPECTED_CONTROL)} failures; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("every sample lands on the block it belongs to, and a block goes green only when it is done")

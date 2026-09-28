#!/usr/bin/env python3
"""Segment size is chosen by the WEAKEST card in the fleet, and it cannot be mixed.

⭐ WHY IT MATTERS AT THE TIP. po2 22 measured **~11–12% faster** on block 962,000 (~7,200 inputs) —
a near-tip block — and peaks ~40.6 GB. Applied to tip hour 3's measured per-segment costs on a
36-card fleet:

    969,020   2,104 segs    po2 21  226s  UNDER    po2 22  200s  UNDER
    969,018   6,269 segs    po2 21  674s  OVER     po2 22  597s  UNDER   <- flips
    969,019   8,924 segs    po2 21  960s  OVER     po2 22  850s  over

So it is not a micro-optimisation: it moves the ceiling on 36 cards from ~5,577 to ~6,302 segments.

⛔ THE STALE NOTE THAT SAYS OTHERWISE. `prover/host/src/main.rs` records po2 21 and 22 as "flat",
measured on **block 130,000** — a 2011 block with almost no transactions. Fold overhead scales with
segment count, so that says nothing about a 9,000-segment block. The tip measurement is the one to
believe, and this project's own rule is that cost is a property of the card AND the operating point.

⛔ IT CANNOT BE MIXED ACROSS CARDS. `segment_limit_po2()` is set on the ExecutorEnv, so the aggregate
segments the block ONCE and every worker proves whatever it is handed. There is no per-card po2 —
which is exactly why the fleet is capped by its smallest VRAM rather than its average.

    python3 test_seg_po2.py             # the weakest card decides; unknown means 21
    python3 test_seg_po2.py --control   # pick on the BIGGEST card — a 24GB card must be starved
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_smoke  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0

CAT = [{"id": "PRO6000", "vram": 96}, {"id": "H100", "vram": 80}, {"id": "L40S", "vram": 48},
       {"id": "A6000", "vram": 48}, {"id": "RTX4090", "vram": 24}, {"id": "L4", "vram": 24}]


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def pick(types):
    """The po2 the fleet runs."""
    fleet = [{"gpu_type": t} for t in types]
    if CONTROL:
        # the tempting wrong rule: judge on the best card present
        vram = {c["id"]: c["vram"] for c in CAT}
        biggest = max(vram.get(t, 0) for t in types)
        return "22" if biggest >= tip_smoke.PO2_22_MIN_VRAM_GB else "21"
    return tip_smoke.resolve_seg_po2("auto", fleet, CAT)[0]


# ── 1. a uniform fleet of big cards ─────────────────────────────────────────────────────────────
check(pick(["PRO6000"] * 36) == "22", "36x PRO 6000 (96GB) runs po2 22")
check(pick(["L40S", "A6000"]) == "22", "a 48GB fleet runs po2 22 — 40.6GB peak fits")

# ── 2. ⛔ ONE SMALL CARD CAPS THE WHOLE FLEET ───────────────────────────────────────────────────
got = pick(["PRO6000"] * 35 + ["RTX4090"])
if CONTROL:
    check(got == "22",
          "control reproduces it: 35 big cards and one 24GB card still picks 22 — that card cannot "
          "hold a 40.6GB segment and stalls every block it is given")
else:
    check(got == "21",
          "one 24GB card among 35 big ones caps the fleet at 21 — the block is segmented ONCE, so "
          "the weakest card decides for everybody")
    check(pick(["RTX4090"]) == "21", "an all-24GB fleet is 21")

# ── 3. an unknown card is treated as too small ──────────────────────────────────────────────────
if not CONTROL:
    po2, why = tip_smoke.resolve_seg_po2("auto", [{"gpu_type": "MYSTERY"}], CAT)
    check(po2 == "21", "a card whose VRAM we do not know gets 21")
    check("unknown" in why.lower(), f"and says so: {why[:60]}")
    # ⚠ Guessing upward costs the whole block: every worker OOMs on a segment it cannot hold, and
    # the retry ladder reads as a merely slow fleet.
    check(tip_smoke.resolve_seg_po2("auto", [], CAT)[0] == "21", "an empty fleet is 21")

# ── 4. pinning overrides, and says it was pinned ────────────────────────────────────────────────
if not CONTROL:
    for want in ("21", "22", 20):
        po2, why = tip_smoke.resolve_seg_po2(want, [{"gpu_type": "PRO6000"}], CAT)
        check(po2 == str(want), f"--seg-po2 {want} is honoured")
        check("pinned" in why, "and the reason names it as pinned, not inferred")

# ── 5. a VRAM lookup that fails must not guess upward, or crash the run ─────────────────────────
if not CONTROL:
    orig = tip_smoke.rank_card_types
    try:
        tip_smoke.rank_card_types = lambda api, **kw: (_ for _ in ()).throw(RuntimeError("api down"))
        po2, why = tip_smoke.resolve_seg_po2("auto", [{"gpu_type": "PRO6000"}], None, api=object())
        check(po2 == "21", "a failed catalogue lookup falls to 21 rather than assuming 22")
    finally:
        tip_smoke.rank_card_types = orig

# ── 6. it actually reaches the pods ─────────────────────────────────────────────────────────────
if not CONTROL:
    src = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
    check('"HAZYNC_SEG_PO2": seg_po2' in src, "the resolved po2 goes into prove_env")
    m = re.search(r'assigns = " "\.join', open(os.path.join(HERE, "tip_runner.py"),
                                                encoding="utf8").read())
    check(m is not None, "and prove_env is expanded into every pod's launch command")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

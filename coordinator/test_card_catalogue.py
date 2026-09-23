#!/usr/bin/env python3
"""Rank cards by what was MEASURED, and never attribute a mixture to one card (hazync#493).

⛔ WHY THIS EXISTS. #448 found that a fleet containing an A40 took 382.8 s against an all-4090 fleet's
272.2 s, AND cost more per proof ($0.262 vs $0.243) — so ranking cards by price per hour buys the
slower, dearer fleet. The fix then was to hard-code 4090-only. On 2026-09-23 that turned into an
outage: 4090 secure stock went "Low", five launch attempts rented 0-2 pods, and the run never
started. A catalogue is the right answer, but it has two ways to silently recreate #448:

  * ordering the MEASURED tier by price instead of by usd_per_proof — the original mistake, back
  * attributing a MIXED fleet's cost to a single card. `docs/history/fleet-economics.jsonl` holds
    '2x A40 + 1x RTX 4090 → $0.259'. Read that as an A40 number and the A40 is PROMOTED into the
    measured tier on the strength of runs that were 40% SLOWER, ranking above every genuinely
    unmeasured card on evidence that does not exist for it.

⚠ It does NOT overtake the 4090: those mixed runs cost $0.259-$0.262 against the 4090's $0.243, so
the fabricated number still sorts second. That is luck, not a safeguard — had the mixture come in
under $0.243 the inversion would be total. The defect is the fabrication, so that is what is pinned.

Both are pinned below, each with a control that reproduces it.

  python3 test_card_catalogue.py            # must PASS
  python3 test_card_catalogue.py --control  # mixtures are attributed + price ranks; MUST FAIL
"""
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_smoke as t                                                            # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# The real rows from docs/history/fleet-economics.jsonl, plus a uniform A40 row that does NOT exist
# (nobody has ever run an all-A40 fleet — which is the whole point: it must stay unmeasured).
ECON = """\
{"block":"741000","cards":3,"fleet":"3x RTX 4090","seconds":267.9,"usd_per_proof":0.243}
{"block":"741000","cards":3,"fleet":"3x RTX 4090","seconds":271.7,"usd_per_proof":0.259}
{"block":"741000","cards":3,"fleet":"1x A40 + 2x RTX 4090","seconds":357.9,"usd_per_proof":0.262}
{"block":"741000","cards":3,"fleet":"2x A40 + 1x RTX 4090","seconds":380.2,"usd_per_proof":0.259}
{"block":"968243","cards":16,"fleet":"16x A40","seconds":3410.6,"usd_per_proof":7.428}
{"block":"741000","cards":3,"fleet":"3x RTX 4090","seconds":140.0,"usd_per_proof":0.120,"po2":22,"gpus_per_pod":1}
{"block":"741000","cards":3,"fleet":"3x A40","seconds":300.0,"usd_per_proof":0.130,"po2":22,"gpus_per_pod":8}
"""


def econ_file():
    d = tempfile.mkdtemp(prefix="econ_")
    p = os.path.join(d, "fleet-economics.jsonl")
    open(p, "w", encoding="utf8").write(ECON)
    return p


class FakeAPI:
    """RunPod's catalogue as it actually answered on 2026-09-23, plus cards that must be excluded."""

    CARDS = [
        # id,                             display,      vram, secure price, stock
        ("NVIDIA GeForce RTX 4090",       "RTX 4090",     24, 0.74, "Low"),
        ("NVIDIA A40",                    "A40",          48, 0.49, "Low"),
        ("NVIDIA L40S",                   "L40S",         48, 1.09, "Low"),
        ("NVIDIA L4",                     "L4",           24, 0.49, "Low"),
        ("NVIDIA H100 80GB HBM3",         "H100 SXM",     80, 3.49, "Medium"),
        ("NVIDIA GeForce RTX 3070",       "RTX 3070",      8, 0.20, "High"),    # too little VRAM
        ("NVIDIA A100 80GB PCIe",         "A100 PCIe",    80, 1.59, None),      # no stock
        ("AMD Instinct MI300X OAM",       "MI300X",      192, 0.99, "High"),    # cannot run CUDA
        # ⛔ A MIG PARTITION, NOT A CARD. Priced BELOW every whole GPU here, so if it is not excluded
        # it sorts near the top of the unmeasured tier on price alone.
        ("NVIDIA RTX PRO 6000 Blackwell Server Edition MIG 1g.24gb",
         "PRO 6000 MIG 24GB", 24, 0.59, "Low"),
    ]

    def _gql(self, q):
        if "gpuTypes { id }" in q:
            return {"gpuTypes": [{"id": c[0]} for c in self.CARDS]}
        for gt, disp, vram, price, stock in self.CARDS:
            if f'"{gt}"' in q:
                lp = None if stock is None else {"uninterruptablePrice": price,
                                                 "stockStatus": stock}
                return {"gpuTypes": [{"id": gt, "displayName": disp, "memoryInGb": vram,
                                      "lowestPrice": lp}]}
        raise KeyError("no such type")


# ⛔ THE CONTROLS MUTATE THE SHIPPED FUNCTIONS, not copies of them.
if CONTROL:
    _orig = t.measured_cost_per_proof

    def attribute_mixtures(path=t.ECONOMICS):
        """CONTROL: charge a mixed fleet's cost to every card in it.

        ⚠ ONE VARIABLE. This mirrors the real function's operating-point grouping exactly and
        changes ONLY the mixture rule. An earlier version also flattened the grouping, so it failed
        six assertions instead of three and proved nothing about which rule was load-bearing.
        """
        import json
        import re as _re
        per = {}
        for line in open(path, encoding="utf8"):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            usd, blk = r.get("usd_per_proof"), str(r.get("block") or "")
            if usd is None or not blk:
                continue
            point = (blk, r.get("po2"), r.get("gpus_per_pod"))
            b = per.setdefault(point, {"best": {}, "rows": 0})
            b["rows"] += 1
            for part in r.get("fleet", "").split("+"):     # the mutation: mixtures attributed
                m = _re.match(r"\s*\d+x\s+(.+?)\s*$", part)
                if m and (m.group(1) not in b["best"] or usd < b["best"][m.group(1)]):
                    b["best"][m.group(1)] = float(usd)
        if not per:
            return {}, None
        pt = max(per, key=lambda k: (len(per[k]["best"]), per[k]["rows"]))
        blk, po2, gpp = pt
        label = f"block {blk}"
        if po2 is not None:
            label += f", po2 {po2}"
        if gpp is not None:
            label += f", {gpp} GPU/pod"
        return per[pt]["best"], label

    t.measured_cost_per_proof = attribute_mixtures

ECON_PATH = econ_file()

# ── 1. ⛔ THE CENTRAL CASE. A mixture is not a measurement of either card in it ───────────────────
m, mblk = t.measured_cost_per_proof(ECON_PATH)
check("RTX 4090" in m, f"the 4090 has a measured number from its uniform runs ({m.get('RTX 4090')})")
check(abs(m.get("RTX 4090", 0) - 0.243) < 1e-9,
      f"and it is the BEST uniform run, $0.243 (got {m.get('RTX 4090')})")
check("A40" not in m,
      f"the A40 stays UNMEASURED on this block — its only clean run is on ANOTHER block, and a "
      f"10,666-segment tip block is not comparable with 741,000 (got {m.get('A40')})")
check(mblk.startswith("block 741000"),
      f"the comparison is confined to ONE operating point (got {mblk!r})")
# ⛔ A po2-22 row on the SAME block and the SAME card is a DIFFERENT operating point. po2 22 halves
# the segment count and needs a 48GB+ card, so crediting it to the silicon would buy the wrong card.
# Likewise 8 GPUs per pod share a NIC and PCIe -- a deployment shape, not a card property.
check("po2" not in mblk or "22" not in mblk,
      f"the po2-22 rows did not merge into the po2-21 comparison (got {mblk!r})")
check(abs(m.get("RTX 4090", 0) - 0.243) < 1e-9,
      f"and the 4090's figure is still its po2-21 number, not the cheaper po2-22 one "
      f"(got {m.get('RTX 4090')}, the po2-22 row is $0.120)")

# ── 2. the ranking: measured tier first, ordered by cost per proof ────────────────────────────────
ranked = t.rank_card_types(FakeAPI(), economics=ECON_PATH)
ids = [c["id"] for c in ranked]
by = {c["id"]: c for c in ranked}

check(ids and ids[0] == "NVIDIA GeForce RTX 4090",
      f"the 4090 ranks FIRST — the only card with a measured cost per proof (got {ids[:1]})")
check(by.get("NVIDIA A40") is not None
      and ids.index("NVIDIA A40") > ids.index("NVIDIA GeForce RTX 4090"),
      "the A40 is still offered, but BELOW the 4090 — cheaper per hour is not cheaper per proof")
check(by.get("NVIDIA L40S") is not None,
      "the L40S is in the catalogue at all — it was refused outright before (#493)")

# ── 3. the unmeasured tier is ordered by price, and says so ──────────────────────────────────────
unmeasured = [c for c in ranked if c["usd_per_proof"] is None]
check([c["price"] for c in unmeasured] == sorted(c["price"] for c in unmeasured),
      "unmeasured cards are ordered cheapest-per-hour first (the only ordering available)")
# ⛔ NOT `all(c["usd_per_proof"] is None for c in unmeasured)` — `unmeasured` is DEFINED as the cards
# whose usd_per_proof is None, so that assertion re-derives its own subject and cannot fail. Name the
# card and name the tier instead.
check(by["NVIDIA A40"]["usd_per_proof"] is None,
      f"the A40 sits in the UNMEASURED tier, where a card with no uniform run belongs "
      f"(got {by['NVIDIA A40']['usd_per_proof']})")
measured_tier = {c["display"] for c in ranked if c["usd_per_proof"] is not None}
check(measured_tier == {"RTX 4090"},
      f"and the measured tier is exactly the cards with a uniform run: {sorted(measured_tier)}")

# ── 4. exclusions: a card that cannot run, or cannot be bought, is not offered ───────────────────
check("AMD Instinct MI300X OAM" not in ids, "an AMD card is excluded — the prover is a CUDA build")
check("NVIDIA GeForce RTX 3070" not in ids,
      f"an 8GB card is excluded — below the {t.VRAM_FLOOR_GB}GB the 4090 proves at")
check("NVIDIA A100 80GB PCIe" not in ids,
      "a card with no SECURE stock is excluded — it cannot be rented however it is listed")
check(not any("MIG" in i for i in ids),
      f"a MIG PARTITION is excluded — it is a slice of a card, and at $0.59 it would rank above "
      f"every whole GPU here on price alone (got {[i for i in ids if 'MIG' in i]})")

# ⚠ NAME EVERY ONE AND COUNT THEM. Listing a subset lets an unrelated breakage ride along inside a
# "CONTROL OK". Attributing mixtures gives the A40 $0.259, which both invents a number for it AND
# promotes it above the 4090 — one mutation, two wrong answers, which is the shape of #448 itself.
EXPECTED_CONTROL = {
    "the A40 stays UNMEASURED",
    "the A40 sits in the UNMEASURED tier",
    "and the measured tier is exactly the cards with a uniform run",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — attributing a mixture to its cards fabricated an A40 measurement "
              "and promoted it into the measured tier:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected exactly {len(EXPECTED_CONTROL)} attribution failures; "
          f"got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("every card RunPod can sell is offered, ranked by what was measured, mixtures attributed to "
      "nobody")

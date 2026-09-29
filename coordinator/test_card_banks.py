#!/usr/bin/env python3
"""Banking cards by their measured tail concentrates the tail; banking by name does not (hazync#550).

⭐ WHY. The fold waits for the slowest peer at EVERY level, so one bad link sets the wall for the
whole block — and adding cards does not fix a tail, it adds levels for the tail to appear at.
📏 Measured, tip hour 4: per-card join p90 spanned 2,957 ms to 16,437 ms, a **5.6x spread on a
UNIFORM fleet** of 38 identical cards at one price. Not the card model; `--gpu-type` cannot fix it.

⛔ THE FAILURE THIS GUARDS is a grouping that looks principled and separates nothing. Any partition
of a fleet produces banks, and every one of them reports a "worst peer" — so a report alone proves
nothing. The control banks by NAME, an arbitrary grouping, and must come out visibly worse at
concentrating the tail. If the two modes agree, the tool is not measuring what it claims.

    python3 test_card_banks.py             # by measured tail: the tail lands in one bank
    python3 test_card_banks.py --control   # by name: it must NOT separate
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_banks  # noqa: E402

CONTROL = "--control" in sys.argv
fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def samples_for(spec):
    """[(level, rtt_ms, card)] with `spec` = {card: (rtt_ms, n)}.

    ⚠ Constant RTT per card on purpose: the partition is what is under test, not the percentile
    maths, which join_levels already covers.
    """
    out = []
    for card, (rtt, n) in spec.items():
        out.extend((0, float(rtt), card) for _ in range(n))
    return out


# Tip hour 4's shape: most cards clustered low, a handful with a long tail.
# ⚠ Names are deliberately INTERLEAVED against speed — a-fast, b-slow, c-fast … — so that banking by
# name cannot accidentally separate the tail and pass for the wrong reason.
FAST = {"a1": 3000, "c1": 3200, "e1": 3400, "g1": 3600, "i1": 3800, "k1": 4000}
SLOW = {"b1": 12000, "d1": 13000, "f1": 14000, "h1": 15000, "j1": 16000, "l1": 16400}
spec = {c: (r, 200) for c, r in {**FAST, **SLOW}.items()}
S = samples_for(spec)

ranked, thin, unlabelled = tip_banks.rank_cards(S)
stats = dict(ranked)
check(len(ranked) == 12, f"all 12 cards are rankable ({len(ranked)})")
check([c for c, _ in ranked][0] in FAST, "the ranking runs fastest-first")

groups = tip_banks.banks(ranked, 2, by_name=CONTROL)
check(len(groups) == 2, f"two banks ({len(groups)})")

w = [tip_banks.worst_peer(g, stats) for g in groups]
fleet_worst = max(v["p90"] for v in stats.values())
best = min(w)
ratio = fleet_worst / best
print(f"       banks: {[sorted(g) for g in groups]}")
print(f"       worst peers: {w}  fleet {fleet_worst:.0f}ms  ratio {ratio:.1f}x")

if CONTROL:
    # ⛔ Banking by name must leave a slow card in every bank, so the best bank is barely better
    # than the whole fleet. That is the whole argument of #550 stated as a failing case.
    check(ratio < 1.5,
          f"⛔ control: banking by NAME barely improves the worst peer ({ratio:.1f}x) — an arbitrary "
          f"grouping does not concentrate the tail")
    check(any(any(c in SLOW for c in g) for g in groups) and
          all(any(c in SLOW for c in g) for g in groups),
          "control: every bank still contains a slow card")
else:
    # ── the one that matters ────────────────────────────────────────────────────────────────────
    fast_bank = min(groups, key=lambda g: tip_banks.worst_peer(g, stats))
    check(all(c in FAST for c in fast_bank),
          f"the fast bank contains ONLY fast cards ({sorted(fast_bank)})")
    slow_bank = max(groups, key=lambda g: tip_banks.worst_peer(g, stats))
    check(all(c in SLOW for c in slow_bank),
          f"and the tail is concentrated in the other ({sorted(slow_bank)})")
    check(ratio > 2.5,
          f"the best bank's worst peer is much better than the fleet's ({ratio:.1f}x)")
    # ⛔ It must NOT balance. Equalising the banks would give both a similar worst peer, which is
    # the opposite of segregating a tail — and is what a generic partitioner would do.
    check(abs(w[0] - w[1]) > 5000,
          f"the banks are deliberately UNEQUAL ({w[0]:.0f}ms vs {w[1]:.0f}ms) — balancing them "
          f"would defeat the purpose")

# ── thin and unlabelled cards are named and excluded, never quietly merged ──────────────────────
S2 = S + samples_for({"thin1": (900, 5)}) + [(0, 5000.0, None)] * 7
ranked2, thin2, unl2 = tip_banks.rank_cards(S2)
check("thin1" in thin2 and "thin1" not in dict(ranked2),
      "a card with too few joins is excluded and named, not ranked against one with 200")
check(unl2 is not None and unl2["n"] == 7,
      f"unlabelled samples are counted separately ({unl2['n'] if unl2 else 0}), not merged into a card")

# ── degenerate ─────────────────────────────────────────────────────────────────────────────────
check(tip_banks.report([], 2) == 1, "no samples reports a reason and fails rather than inventing banks")
one = samples_for({"only": (1000, 200)})
check(tip_banks.report(one, 2) == 1, "fewer rankable cards than banks refuses to split")

print()
if fails:
    print(f"FAIL {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

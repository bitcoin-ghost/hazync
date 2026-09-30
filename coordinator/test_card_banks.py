#!/usr/bin/env python3
"""Banking cards by their measured tail concentrates the tail; banking by name does not (hazync#550).

⭐ WHY. The fold waits for the slowest peer at EVERY level, so one bad link sets the wall for the
whole block — and adding cards does not fix a tail, it adds levels for the tail to appear at.
📏 Measured, tip hour 4: per-card join p90 spanned 3,562 ms to 45,253 ms, a **12.7x spread on a
UNIFORM fleet** of 38 identical cards at one price. Not the card model; `--gpu-type` cannot fix it.
⛔ That was published as 5.6x from `agg-history.log` alone — half the harvest, missing the live
`agg.log` that holds the run's worst tail. Pass every aggregate log.

⛔⛔ AND THE SECOND FAILURE THIS NOW GUARDS is the one the first version walked into: a **ratio of
worst peers read as a speedup**. "5.1x better worst peer" is true and means far less than it sounds,
because the fold waits for the slowest join in each batch separately and most batches are not the
one the worst card ruined. Measured, dropping hour 4's worst card takes 12.9 % off the fold's
waiting, and the worst five take 27.8 %. ⚠ And the prize is NOT a constant: replicated on hour 5's
six-card fleet the worst card alone takes off 45.2 %, because one bad card in six sets the wall in
nearly every batch. A bank suffers its worst member far more than the whole fleet does.
`tail_prize` must therefore stay far below the worst-peer ratio on tail-shaped data, and its own
control is a fleet with NO tail, where it must find almost nothing to win.

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
import join_levels  # noqa: E402
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

# ── ⛔ THE PRIZE: what the fold actually waits for, not a ratio of worst peers ───────────────────
# `worst_peer` is one number over a whole run. The fold waits for the slowest join in each batch,
# every time — so the measurable cost of a tail is the sum of those maxima, and dropping the worst
# card only removes it from the batches it was actually in.
def batches_for(spec, n_batches=50):
    """[[(level, rtt, card)]] — every card appears in every batch, so a batch IS a cohort."""
    return [[(0, float(rtt), card) for card, (rtt, _) in spec.items()] for _ in range(n_batches)]


# ⚠ HOUR 4'S SHAPE, not two even clusters: 30 cards packed between 3,500 and 4,500 ms and five
# with a real tail. That is what makes the curve FLATTEN past the tail — once the tail is gone the
# pack sets the wall, and the next card removed buys almost nothing. An evenly spread fleet cannot
# show that. ⛔ "Flatten", not "saturate": on hour 4's real data the curve climbs again far past
# this window (49.5 % at twenty-two of thirty-seven), because by then it is measuring a small fleet
# rather than a tail. I published "saturates" and it was the shape of the window I chose.
PACK = {f"p{i:02d}": (3500 + i * 33, 200) for i in range(30)}
TAIL = {"t1": (12000, 200), "t2": (16000, 200), "t3": (21000, 200),
        "t4": (32000, 200), "t5": (45000, 200)}
TAILED = {**PACK, **TAIL}
B = batches_for(TAILED)
worst_first = sorted(TAILED, key=lambda c: -TAILED[c][0])
base, curve = tip_banks.tail_prize(B, worst_first, ks=(1, 2, 3, 5, 8, 12))
frac = dict((k, f) for k, _, _, f in curve)
left = dict((k, n) for k, n, _, _ in curve)
print(f"       prize curve: {[(k, left[k], round(f * 100, 1)) for k, _, _, f in curve]}")
if not CONTROL:
    check(abs(base - 50 * 45000) < 1.0,
          f"the baseline is the slowest card in each batch, summed ({base:.0f} vs {50 * 45000})")
    # ⛔ THE MISREADING THIS GUARDS. The worst-peer ratio here is 45,000/3,500 = 12.9x, the same
    # shape as hour 4's headline. The measured prize for removing that card is the GAP to the
    # SECOND worst (45,000 -> 32,000 = 28.9%), because something is still slowest in every batch.
    ratio_says = TAILED["t5"][0] / min(v[0] for v in TAILED.values())
    check(frac[1] < 0.35,
          f"⛔ dropping the worst card saves {frac[1] * 100:.1f} % where the worst-peer ratio is "
          f"{ratio_says:.1f}x — the ratio is not a speedup")
    check(frac[5] > frac[1],
          f"clearing the whole tail helps more ({frac[5] * 100:.1f} % vs {frac[1] * 100:.1f} %)")
    # ⛔ AND IT MUST FLATTEN ONCE THE TAIL IS GONE. Past the five tail cards, each further card
    # removed leaves the tight pack setting the wall, so the next seven buy almost nothing. A tool
    # whose prize kept climbing here would be telling us to rent nothing.
    check(frac[12] - frac[5] < 0.05,
          f"⛔ it FLATTENS past the tail: {frac[5] * 100:.1f} % for the five tail cards, still only "
          f"{frac[12] * 100:.1f} % for twelve — the pack sets the wall")
    # ⚠ Every row says how many cards are LEFT, because a prize is meaningless without it.
    check(all(left[k] == len(TAILED) - k for k in frac),
          f"and each row reports the surviving fleet ({left})")

    # ── ⛔ THE GUARD: a prize that leaves no fleet is not reported at all ────────────────────────
    # Measured on hour 5: six rankable cards, and the first version happily reported "drop the
    # worst 5 — 82.2 % off", which is the wall time of a ONE-CARD fleet.
    six = {f"s{i}": (3000 + i * 2000, 200) for i in range(6)}
    _, small = tip_banks.tail_prize(batches_for(six), sorted(six, key=lambda c: -six[c][0]),
                                   ks=(1, 2, 3, 4, 5))
    ks_reported = [k for k, _, _, _ in small]
    check(ks_reported == [1, 2, 3],
          f"⛔ on six cards only k<=3 is reported, not 4 or 5 — {ks_reported} (MIN_FLEET="
          f"{tip_banks.MIN_FLEET})")
    check(all(n >= tip_banks.MIN_FLEET for _, n, _, _ in small),
          "and every reported row leaves a real fleet standing")
    _, none_left = tip_banks.tail_prize(batches_for({"a": (1, 9), "b": (2, 9)}), ["a", "b"])
    check(none_left == [],
          f"⚠ with two cards there is NO prize to report, not a prize of zero ({none_left})")
    # ⛔ POSITIVE CONTROL, inline: on a fleet with NO tail there is nothing to win, and a tool that
    # reports a big prize anyway is measuring the partition rather than the tail.
    flat = {c: (5000, 200) for c in TAILED}
    _, fc = tip_banks.tail_prize(batches_for(flat), sorted(flat))
    check(all(f < 0.01 for _, _, _, f in fc),
          f"⛔ control: with every card identical the prize is ~0 "
          f"({max(f for _, _, _, f in fc) * 100:.2f} % at best), not a partition artefact")

# ── the card effect is measured against the batch, not against the run ──────────────────────────
# ⚠ Two batches, one slow and one fast for EVERYONE: a card that only appears in the slow batch
# looks slow by p90 and is average once its own batch is the yardstick. That is the 12.7x vs 6.5x
# difference on hour 4, stated as a case with a known answer.
# ⛔ `late` and `slow` have the SAME p90 (20,000 ms) and are not the same thing: one merely joined
# while everything was slow, the other was twice its own batch. Only the cohort view tells them
# apart, and that is the whole reason hour 4's 12.7x spread is a 6.5x card effect.
SLOWBATCH = [(0, 20000.0, "late"), (0, 20000.0, "always"), (0, 40000.0, "slow")]
FASTBATCH = [(0, 2000.0, "always"), (0, 2000.0, "early")]
eff = tip_banks.cohort_effect([SLOWBATCH] * 60 + [FASTBATCH] * 60, min_samples=40)
p90s = join_levels.by_card([x for b in [SLOWBATCH] * 60 + [FASTBATCH] * 60 for x in b])
print(f"       cohort effect: {dict((k, round(v, 2)) for k, v in eff.items())}")
print(f"       p90 says:     {dict((k, v['p90']) for k, v in p90s.items())}")
if not CONTROL:
    check(p90s["late"]["p90"] == p90s["slow"]["p90"] == 20000.0 or
          p90s["late"]["p90"] == 20000.0,
          f"⚠ by p90 `late` reads {p90s['late']['p90']:.0f}ms — as slow as anything in the run")
    check(eff["late"] <= 1.0,
          f"⛔ but against the cards it joined ALONGSIDE, `late` is at or below average "
          f"({eff['late']:.2f}x) — it joined while everything was slow, it is not slow")
    check(eff["slow"] > 1.4,
          f"while a card that really is twice its batch shows it ({eff['slow']:.2f}x)")
    check(eff["slow"] > eff["late"] * 1.4,
          "⛔ so the two are separated, which a p90 alone cannot do")

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

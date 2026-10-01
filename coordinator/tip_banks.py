#!/usr/bin/env python3
"""Group cards into banks by their MEASURED tail, from logs already on disk (hazync#550).

⭐ WHY. The fold waits for the slowest peer at every level, so one bad link sets the wall clock for
the whole block — and **adding cards does not fix a tail, it adds levels for the tail to appear at.**
Measured on tip hour 4 (13,676 labelled joins, 37 cards): per-card join p90 ranged 3,562 ms to
45,253 ms, a **12.7x spread on a UNIFORM fleet** of 38 identical cards at one price. So the spread is
not the card model, and `--gpu-type` cannot address it.

⛔ THAT WAS PUBLISHED AS 5.6x AND 5.6x WAS HALF A HARVEST — `agg-history.log` alone, 9,366 of 13,676
joins, missing the live `agg.log` that holds the run's worst tail. **Pass every aggregate log.**

Today that measurement is used only to CUT (`--worker-min-mbit`, `slow_worker_cut`, which defaults
to 0.0 and did not fire in hour 4). #550 proposes using it to GROUP: cards that feed each other
quickly form a bank, and each bank takes its own block. A bank's wall is set by its slowest member,
so segregating the tail into one bank leaves the other bank with a far better worst peer.

⛔⛔ BUT A RATIO OF WORST PEERS OVERSTATES THE PRIZE. "5.1x better worst peer" is what this tool
reported for hour 4, and it invites the reading that a bank would prove 5x faster. What the fold
actually waits for is the slowest join in each batch that is in flight together, and that is
measurable from the same logs: summed over hour 4's 619 batches it is 6,416 s, and removing the
worst card entirely takes off **12.9 %**; the worst five, 27.8 %.

⛔ AND THE PRIZE DEPENDS ON THE FLEET SIZE, WHICH IS THE WHOLE POINT OF #550. Replicated on tip
hour 5 (2026-09-30, 6 rankable cards, 258 batches, the SAME tool and the same two logs):

    fleet    drop the worst card
    37          12.9 %
     6          45.2 %

One bad card in six is in nearly every batch and sets the wall nearly every time; one in
thirty-seven is not. ⇒ **A bank suffers its worst member far more than the whole fleet does**,
which argues for banking and warns that the slow bank pays for it.

⚠ AND THE CURVE DOES NOT "SATURATE" -- I published that, and it was the shape of the window I
chose. The default `ks` stops at ten, and inside that window hour 4 does flatten (27.8 % at five,
30.2 % at ten). Over the full range it climbs again: 49.5 % at twenty-two, 74.9 % at thirty-one.
Of course it does -- past a point it is measuring a tiny fleet, not a tail. So `tail_prize` now
reports how many cards are LEFT at each step and refuses a step that leaves fewer than MIN_FLEET.

⛔ AND THE DATACENTRE LABEL IS NOT THE SIGNAL — REPLICATED ON TWO RUNS. Decomposing the variance
of log(rtt):

                        hour 4            hour 5
                   (37 cards, 9 dc)   (6 cards, 2 dc)
    WHEN (batch)        31.4 %            26.1 %
    WHICH LEVEL         21.6 %            28.6 %
    WHICH CARD          17.3 %            17.5 %      <- near identical across both
    WHICH DATACENTRE    12.7 %             7.8 %      <- smallest in both

⭐ The CARD share lands within 0.2 points on two runs whose fleets differ sixfold, which is the
strongest thing measured here. And `dc` is the smallest term in both, so grouping by it would
capture almost none of the spread. #550's own wording -- bank by measured link, not by a datacentre
label -- is what the data supports.

⚠ Hour 5's dc row rests on TWO groups covering four of its six cards, so on its own it is weak; it
does not contradict hour 4 and is not independent confirmation either. Cited as replication of the
ORDERING, not of the number.

⚠ AND THE CARD EFFECT IS SMALLER AND LESS STABLE THAN THE SPREAD SUGGESTS. Measured against the
cards it joined alongside, rather than against the whole run, the per-card effect is 6.5x
(0.46x .. 2.98x) — and a card's p90 in the first half of the run predicts its second half with
Spearman rho +0.44. A slow card tends to stay slow; it is not a fixed property, so a bank assignment
made once at the start of a run decays.

⛔ WHAT THIS TOOL DOES NOT DO — AND WHY IT STOPS SHORT ON PURPOSE.

  * It does not predict a block time. It reports each bank's WORST PEER and, given cohorts, the
    measured share of the fold's waiting that the tail accounts for. Turning either into a block
    time needs a model of the join tree under a different fleet size, and this project has a scar
    from exactly that: lever 2 was predicted to save ~36 s from a plausible model and measured
    NEGATIVE.
  * `tail_prize` is an UPPER BOUND, not a saving. The dropped cards' segments still have to be
    proved by someone, and a second bank pays its own fold. It bounds the prize.
  * It does not turn anything on. Two banks need TWO AGGREGATES and two concurrent blocks — the
    aggregate executes a specific block and cannot be shared (#506). That orchestration is not
    built. This answers "is it worth building?", not "switch it on".

⚠ A card is ranked only if it has enough joins to have a meaningful p90. A card with 12 samples
sitting beside one with 335 is not a comparison, and the early blocks of a run — 3 to 10 cards
attached — carry RTTs inflated by QUEUEING rather than transport (`[rtt]` is dispatch-to-return).
Under-sampled cards are named and excluded, never quietly merged.

    python3 tip_banks.py --banks 2 <log>...
    python3 tip_banks.py --banks 2 --control <log>...   # bank by NAME — must fail to separate it
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import join_levels  # noqa: E402

MIN_SAMPLES = 40


def rank_cards(samples, min_samples=MIN_SAMPLES):
    """(ranked, thin, unlabelled) — cards fast-to-slow by p90, plus what was excluded and why."""
    bc = join_levels.by_card(samples)
    unlabelled = bc.pop("?", None)
    thin = {c: v for c, v in bc.items() if v["n"] < min_samples}
    good = {c: v for c, v in bc.items() if v["n"] >= min_samples}
    ranked = sorted(good.items(), key=lambda kv: kv[1]["p90"])
    return ranked, thin, unlabelled


def banks(ranked, n_banks, by_name=False):
    """Split ranked cards into `n_banks`.

    The real policy SEGREGATES: fastest cards together, so one bank carries the tail and the other
    does not. ⛔ It deliberately does NOT balance the banks — snake-drafting to equalise them would
    give every bank a similar worst peer, which is the opposite of the point.

    `by_name` is the control: bank by card name, the arbitrary grouping #550 argues against.
    """
    cards = [c for c, _ in ranked]
    if by_name:
        cards = sorted(cards)
    out, size = [], (len(cards) + n_banks - 1) // n_banks
    for i in range(n_banks):
        chunk = cards[i * size:(i + 1) * size]
        if chunk:
            out.append(chunk)
    return out


def cohort_effect(groups, min_samples=MIN_SAMPLES):
    """{card: factor} — each card's rtt relative to the cards it joined ALONGSIDE (1.0 = average).

    ⛔ WHY NOT JUST RANK BY p90. A p90 compares a card against the whole run, and hour 4 got slower
    for everyone as the blocks grew: a card that attached late inherits that and looks slow for a
    reason that is nothing to do with it. Dividing by its own batch's average removes the run's
    trend, and on hour 4 it takes the apparent 12.7x spread down to a 6.5x card effect.

    ⚠ Geometric, via logs, because a round trip is multiplicative — a card that is twice as slow at
    3 s and twice as slow at 30 s is one effect, not two. Cards under `min_samples` are omitted.
    """
    tot = {}
    for g in groups:
        if len(g) < 2:
            continue                       # ⚠ a batch of one has no cohort to be compared against
        mean = sum(math.log(r) for _, r, _ in g) / len(g)
        for _, rtt, card in g:
            if card:
                tot.setdefault(card, []).append(math.log(rtt) - mean)
    return {c: math.exp(sum(v) / len(v)) for c, v in tot.items() if len(v) >= min_samples}


MIN_FLEET = 3


def tail_prize(groups, order, ks=(1, 2, 3, 5, 8, 10), min_fleet=MIN_FLEET):
    """What dropping the worst k cards takes off the time the FOLD ACTUALLY WAITS.

    ⭐ THE QUANTITY THAT MATTERS. A bank's "worst peer" is one number over a whole run; the fold
    waits for the slowest join in each batch, separately, every time. Summing the max of each batch
    is therefore the measured thing a tail costs — and it is much less than the worst-peer ratio
    implies, because most batches are not the one the worst card ruined.

    Returns (baseline_ms, [(k, cards_left, remaining_ms, fraction_saved)]).

    ⛔ AN UPPER BOUND, NOT A SAVING. The dropped cards' segments still have to be proved by someone,
    and a second bank pays its own fold.

    ⛔⛔ AND IT IS NOT A CURVE THAT SATURATES, WHICH I PUBLISHED AND WAS WRONG ABOUT. Within the
    default `ks` (which stops at ten) hour 4 does flatten: 27.8 % at five, 30.2 % at ten. Over the
    full range it climbs again -- 49.5 % at twenty-two, 74.9 % at thirty-one -- because past a
    point the thing being measured is a tiny fleet rather than a tail. Dropping 36 of 37 cards
    "saves" 94.1 %, which means nothing at all.

    ⚠ SO EVERY ROW CARRIES HOW MANY CARDS ARE LEFT, and a `k` that would leave fewer than
    `min_fleet` is not reported at all. The caller should not have to remember that a prize
    computed on a fleet of two is not a prize.
    """
    usable = [g for g in groups if g]
    base = sum(max(r for _, r, _ in g) for g in usable)
    out = []
    for k in ks:
        # ⛔ BOTH GUARDS, NOT ONE. `k > len(order)` is nonsense; `len(order) - k < min_fleet` is
        # arithmetic on a fleet too small to mean anything, and it is the one that bit -- hour 5
        # has six cards and the old code happily reported dropping five of them.
        # ⚠ `continue`, not `break`: `ks` is not required to be sorted.
        if k > len(order) or len(order) - k < min_fleet:
            continue
        drop = set(order[:k])
        maxes = [max((r for _, r, c in g if c not in drop), default=None) for g in usable]
        maxes = [x for x in maxes if x is not None]
        rem = sum(maxes)
        out.append((k, len(order) - k, rem, 1.0 - rem / base if base else 0.0))
    return base, out


def worst_peer(bank, stats):
    """The p90 of the slowest card in this bank — what sets its wall at every level."""
    return max(stats[c]["p90"] for c in bank) if bank else None


def report(samples, n_banks, by_name=False, batches=None):
    ranked, thin, unlabelled = rank_cards(samples)
    if not ranked:
        print("no card has enough labelled joins to rank — is this log from before hazync#570?")
        return 1
    if len(ranked) < n_banks:
        print(f"only {len(ranked)} rankable card(s) for {n_banks} bank(s) — nothing to split")
        return 1

    stats = dict(ranked)
    fleet_worst = max(v["p90"] for v in stats.values())
    fleet_best = min(v["p90"] for v in stats.values())
    groups = banks(ranked, n_banks, by_name=by_name)

    mode = "BY NAME (control)" if by_name else "by measured tail"
    print(f"banking {len(ranked)} card(s) into {len(groups)} bank(s), {mode}")
    print(f"  whole fleet as one bank: worst peer {fleet_worst:.0f}ms  (best card {fleet_best:.0f}ms)")
    print()
    worsts = []
    for i, g in enumerate(groups, 1):
        w = worst_peer(g, stats)
        worsts.append(w)
        slow = max(g, key=lambda c: stats[c]["p90"])
        print(f"  bank {i}: {len(g)} card(s), worst peer {w:.0f}ms (set by {slow})")
        print(f"    {' '.join(sorted(g))}")
    print()

    best_bank = min(worsts)
    # ⚠ The claim is bounded to what was measured: a ratio of worst peers, not a block time.
    print(f"  best bank's worst peer {best_bank:.0f}ms vs {fleet_worst:.0f}ms for the whole fleet"
          f"  ->  {fleet_worst / max(best_bank, 1):.1f}x better worst peer")
    if by_name:
        print("  ⛔ CONTROL: banking by name does not concentrate the tail — the best bank's worst")
        print("     peer stays close to the fleet's, which is the point #550 is making.")
    print()
    print("  ⚠ This is a ratio of MEASURED worst peers, not a predicted block time. Turning it into")
    print("     a saving needs a model of the join tree at a different fleet size, and lever 2 was")
    print("     predicted to save ~36s from that kind of model and measured NEGATIVE.")

    # ⛔ AND THE RATIO ABOVE OVERSTATES IT. What the fold waits for is the slowest join in each batch
    # that was in flight together, which these same logs can answer -- so answer it rather than let
    # the ratio be read as a speedup.
    if batches:
        eff = cohort_effect(batches)
        base, curve = tail_prize(batches, sorted(eff, key=lambda c: -eff[c]))
        print()
        print(f"  ⛔ WHAT THE FOLD ACTUALLY WAITS FOR — the slowest join in each of "
              f"{len([g for g in batches if g])} batch(es) that were in flight together, summed:"
              f" {base / 1000:.0f} s")
        if not curve:
            print(f"     ⚠ {len(eff)} rankable card(s): dropping ANY of them leaves fewer than "
                  f"{MIN_FLEET}, so there is no prize to report — not a prize of zero.")
        for k, left, rem, frac in curve:
            print(f"     drop the worst {k:<2} ({left:>2} left)  {rem / 1000:7.0f} s   "
                  f"{frac * 100:5.1f} % off")
        print("     ⚠ the prize grows as the fleet SHRINKS — measured 12.9 % for the worst card on"
              " hour 4's 37 cards and 45.2 % on hour 5's 6. It is not a constant, and past a point"
              " it stops being a tail and becomes a small fleet.")
        if eff:
            hi = max(eff.values())
            lo = min(eff.values())
            print(f"  ⚠ card effect measured against the cards it joined ALONGSIDE: {lo:.2f}x .. "
                  f"{hi:.2f}x = {hi / max(lo, 1e-9):.1f}x, against {fleet_worst / max(fleet_best, 1):.1f}x"
                  f" for the raw p90 spread above.")
        print("  ⛔ An UPPER BOUND, not a saving: the dropped cards' segments still have to be proved")
        print("     by someone, and a second bank pays its own fold. It bounds the prize.")
    else:
        print("  ⚠ No batch grouping was supplied, so the share of the fold's WAITING that this tail")
        print("     accounts for is not shown — and the worst-peer ratio above overstates it about")
        print("     threefold on hour 4. Run this on log FILES to get it.")
    print("  ⛔ Nothing is switched on by this. Two banks need TWO AGGREGATES and two concurrent")
    print("     blocks — the aggregate executes a specific block and cannot be shared (#506).")
    if thin:
        names = ", ".join(f"{c}({v['n']})" for c, v in sorted(thin.items()))
        print(f"  ⚠ excluded, under {MIN_SAMPLES} joins: {names}")
    if unlabelled:
        print(f"  ⚠ {unlabelled['n']} sample(s) carried no card= and were excluded, not merged.")
    return 0


def main():
    argv = sys.argv[1:]
    by_name = "--control" in argv
    argv = [a for a in argv if a != "--control"]
    n = 2
    if "--banks" in argv:
        i = argv.index("--banks")
        n = int(argv[i + 1])
        argv = argv[:i] + argv[i + 2:]
    if not argv:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    # ⛔ PER FILE, and the batches added rather than the LINES concatenated: a batch at the end of
    # one log would otherwise fuse with a batch at the start of the next.
    lines, batches = [], []
    for p in argv:
        try:
            with open(p, errors="replace") as fh:
                own = fh.readlines()
        except OSError as e:
            print(f"cannot read {p}: {e}")
            return 2
        lines.extend(own)
        batches.extend(join_levels.cohorts(own))
    return report(join_levels.parse(lines), n, by_name=by_name, batches=batches)


if __name__ == "__main__":
    sys.exit(main())

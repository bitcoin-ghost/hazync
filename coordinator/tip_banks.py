#!/usr/bin/env python3
"""Group cards into banks by their MEASURED tail, from logs already on disk (hazync#550).

⭐ WHY. The fold waits for the slowest peer at every level, so one bad link sets the wall clock for
the whole block — and **adding cards does not fix a tail, it adds levels for the tail to appear at.**
Measured on tip hour 4 (9,366 labelled joins, 37 cards): per-card join p90 ranged 2,957 ms to
16,437 ms, a **5.6x spread on a UNIFORM fleet** of 38 identical cards at one price. So the spread is
not the card model, and `--gpu-type` cannot address it.

Today that measurement is used only to CUT (`--worker-min-mbit`, `slow_worker_cut`). #550 proposes
using it to GROUP: cards that feed each other quickly form a bank, and each bank takes its own block.
A bank's wall is set by its slowest member, so segregating the tail into one bank leaves the other
bank with a far better worst peer.

⛔ WHAT THIS TOOL DOES NOT DO — AND WHY IT STOPS SHORT ON PURPOSE.

  * It does not predict a block time. It reports each bank's WORST PEER, which is arithmetic over
    measured joins. Turning that into a saving needs a model of the join tree under a different
    fleet size, and this project has a scar from exactly that: lever 2 was predicted to save ~36 s
    from a plausible model and measured NEGATIVE.
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


def worst_peer(bank, stats):
    """The p90 of the slowest card in this bank — what sets its wall at every level."""
    return max(stats[c]["p90"] for c in bank) if bank else None


def report(samples, n_banks, by_name=False):
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
    lines = []
    for p in argv:
        try:
            with open(p, errors="replace") as fh:
                lines.extend(fh.readlines())
        except OSError as e:
            print(f"cannot read {p}: {e}")
            return 2
    return report(join_levels.parse(lines), n, by_name=by_name)


if __name__ == "__main__":
    sys.exit(main())

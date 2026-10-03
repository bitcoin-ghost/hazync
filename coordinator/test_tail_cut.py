#!/usr/bin/env python3
"""A tail-based worker cut is RANKED and REPORTED, and never drops the aggregate (hazync#550).

⛔⛔ WHY THIS EXISTS. #550's analysis — `rank_cards`, `banks`, `cohort_effect`, `tail_prize` — was
written and had ZERO callers outside its own module and tests. There was no bank or cohort path
anywhere in tip_smoke, tip_runner or tip_recruit, so the live run cut workers on measured BANDWIDTH
and never saw the measured TAIL that #550 argues is the variable that matters most.

`tail_ranking` / `tail_cut` / `cut_agreement` close that gap, and this file pins the three
properties that make them safe to run beside the live cut:

  1. the AGGREGATE is never ranked and never cut — order[0] is excluded by construction
  2. an UNMEASURED card is never cut and never sorts as fast — "could not tell" is not "fine"
  3. the trimming semantics match slow_worker_cut exactly, so the two are comparable at all

⚠ WHAT THIS CANNOT SHOW: whether cutting by tail is BETTER. That needs one fleet run where the two
criteria disagree. `cut_agreement` exists to produce exactly that evidence.

    python3 test_tail_cut.py             # the properties hold
    python3 test_tail_cut.py --control   # guards removed — MUST fail
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
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


class Card:
    def __init__(self, cid):
        self.cid = cid

    def __repr__(self):
        return f"Card({self.cid})"


def rtt_lines(pairs):
    """`[rtt]` lines exactly as the aggregate prints them, so the real parser is exercised."""
    out = []
    for cid, rtt in pairs:
        tag = join_levels.JOIN_TAG | (1 << 16) | 3
        out.append(f"  [rtt] card={cid} peer=1.2.3.4:5 kind=join "
                   f"tag={hex(tag)} rtt_ms={rtt} bytes_out=1 bytes_in=1")
    return out


def samples_for(spec, n):
    """n join samples per card at the given rtt — enough to clear MIN_SAMPLES."""
    pairs = []
    for cid, rtt in spec.items():
        pairs.extend([(cid, rtt)] * n)
    return join_levels.parse(rtt_lines(pairs))


def install_naive():
    """⛔ THE CONTROL: the obvious implementation, which gets all three properties WRONG.

    It ranks every card in `order` (so the aggregate is in it), sorts unmeasured cards as 0 ms (so
    "could not tell" reads as "fastest"), and drops one card instead of trimming to `need`. Every
    guarded check below must fail against this — otherwise the checks are decoration.

    ⚠ Written as a monkeypatch because the properties live in the code, not in the data: there is no
    input that makes a correct implementation drop the aggregate, so the control has to be a
    different implementation rather than a different fixture.
    """
    def naive_ranking(order, samples, min_samples=tip_banks.MIN_SAMPLES):
        stats = join_levels.by_card(samples)
        out = []
        for c in order:                                  # ⛔ includes order[0], the aggregate
            cid = getattr(c, "cid", c)
            s = stats.get(cid)
            out.append((cid, s["p90"] if s else 0.0))    # ⛔ unmeasured == 0 ms == fastest
        out.sort(key=lambda kv: -kv[1])
        return out, []

    def naive_cut(order, samples, *, need, min_samples=tip_banks.MIN_SAMPLES):
        ranked, _ = naive_ranking(order, samples)
        return [ranked[0][0]] if ranked else []          # ⛔ drops ONE, never trims to need

    tip_banks.tail_ranking = naive_ranking
    tip_banks.tail_cut = naive_cut


def main():
    if CONTROL:
        install_naive()
    n = tip_banks.MIN_SAMPLES + 5
    # agg + four workers. hz-4 is the tail, hz-1 the best.
    order = [Card("hz-agg"), Card("hz-1"), Card("hz-2"), Card("hz-3"), Card("hz-4")]
    spec = {"hz-1": 1000.0, "hz-2": 4000.0, "hz-3": 9000.0, "hz-4": 45000.0}
    samples = samples_for(spec, n)
    # ⛔ The aggregate appears in the samples too — a real agg-history.log contains its own joins.
    samples += samples_for({"hz-agg": 99000.0}, n)

    print("── 1. the aggregate is never ranked and never cut ──")
    ranked, thin = tip_banks.tail_ranking(order, samples)
    names = [cid for cid, _ in ranked]
    check("hz-agg" not in names,
          "the aggregate is absent from the ranking even though it has the WORST tail in the log")
    cut = tip_banks.tail_cut(order, samples, need=2)
    check("hz-agg" not in cut, "the aggregate is absent from the cut")

    print("── 2. worst tail first, and the order is the measured one ──")
    check(names[:4] == ["hz-4", "hz-3", "hz-2", "hz-1"],
          f"ranked worst-first by p90: {names[:4]}")
    check([p for _, p in ranked[:4]] == [45000.0, 9000.0, 4000.0, 1000.0],
          "each card carries its measured p90")

    print("── 3. an UNMEASURED card is never cut and never sorts as fast ──")
    order2 = order + [Card("hz-new")]            # no samples at all
    ranked2, thin2 = tip_banks.tail_ranking(order2, samples)
    check("hz-new" in thin2, "a card with no joins is reported as thin, not ranked on nothing")
    check(ranked2[-1][0] == "hz-new" and ranked2[-1][1] is None,
          "it sorts LAST with p90=None — 'could not tell' is not 'it is fast'")
    # ⚠ order2 is SIX cards (agg + 5), so need=5 is a surplus of ONE. The first version of this
    # check passed need=4 and expected one cut — the code correctly returned two, and the test
    # caught my arithmetic rather than a defect. Spelling the surplus out stops that recurring.
    check(len(order2) == 6, "the fleet size these checks reason about is still six")
    cut2 = tip_banks.tail_cut(order2, samples, need=5)          # surplus = 1
    check("hz-new" not in cut2,
          "a surplus of 1 cuts the worst MEASURED card, not the unmeasured one")
    check(cut2 == ["hz-4"], f"it cut the measured tail: {cut2}")
    check(tip_banks.tail_cut(order2, samples, need=4) == ["hz-4", "hz-3"],
          "a surplus of 2 cuts the two worst measured, in tail order")

    # thin-but-present: fewer than MIN_SAMPLES joins must behave like unmeasured
    thinsamples = samples + samples_for({"hz-thin": 80000.0}, 3)
    order3 = order + [Card("hz-thin")]
    cut3 = tip_banks.tail_cut(order3, thinsamples, need=5)
    check("hz-thin" not in cut3,
          "3 samples is below MIN_SAMPLES, so hz-thin is unmeasured despite an 80s join")

    print("── 4. trimming semantics match slow_worker_cut ──")
    check(tip_banks.tail_cut(order, samples, need=len(order)) == [],
          "no surplus ⇒ cuts nothing")
    check(tip_banks.tail_cut(order, samples, need=99) == [],
          "need above the fleet size ⇒ cuts nothing")
    check(len(tip_banks.tail_cut(order, samples, need=2)) == len(order) - 2,
          "it trims DOWN TO need, like slow_worker_cut (not 'drop one')")
    check(tip_banks.tail_cut([], samples, need=1) == [], "an empty fleet cuts nothing")

    print("── 5. cut_agreement reports the disagreement #550 needs measured ──")
    both, bw_only, tail_only = tip_banks.cut_agreement(["hz-4", "hz-2"], ["hz-4", "hz-3"])
    check(both == ["hz-4"], f"agreed: {both}")
    check(bw_only == ["hz-2"], f"bandwidth only: {bw_only}")
    check(tail_only == ["hz-3"], f"tail only: {tail_only}")
    b2, o2, t2 = tip_banks.cut_agreement([], [])
    check((b2, o2, t2) == ([], [], []), "both empty ⇒ nothing reported")
    # ⚠ Card objects and strings must compare the same way — the live caller passes cids.
    b3, _, _ = tip_banks.cut_agreement([Card("hz-4").cid], ["hz-4"])
    check(b3 == ["hz-4"], "cids compare as strings regardless of how the caller held them")

    if CONTROL:
        if fails:
            print(f"\nCONTROL OK: {len(fails)} check(s) failed as they must")
            return 0
        print("\nCONTROL FAILED: every check passed with the guards removed")
        return 1
    if fails:
        print(f"\nFAILED: {len(fails)} check(s)")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

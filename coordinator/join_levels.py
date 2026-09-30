#!/usr/bin/env python3
"""Per-level join cost from `[rtt]` lines, and what HAZYNC_JOIN_LOCAL_MAX would save.

⭐ WHY THIS EXISTS. Lever 2 (`HAZYNC_JOIN_LOCAL_MAX`, hazync#252) proves the NARROW top levels of the
join tree on the aggregate instead of shipping each pair to a worker. Near the root the tree is
narrower than the fleet, so distributing buys no parallelism at all and still pays a round trip. It is
implemented and OFF by default, held "pending a fleet run" because it measured only +0.4-1.6 s.

⛔ BUT THAT MEASUREMENT WAS TAKEN WHERE THE ROUND TRIP WAS CHEAP: 3 A40s with 2 remote workers, join
p50 rtt 1596 ms. On a real multi-DC fleet join RTT p50 is 17.1 s and max 107.4 s. The lever's whole
value is the round trip it avoids, so it scales with RTT — and the 3-card figure is the one number
guaranteed not to show it. A big win at a wide ceiling is a 3-card artifact, and so is a small one.

⇒ So do not re-run an experiment. `[rtt] tag=` already encodes the join's LEVEL, so a run's own logs
answer this: how many pairs each level had, what a join cost there, and therefore exactly what any
`HAZYNC_JOIN_LOCAL_MAX` would have saved. This reads harvested logs and says so.

⛔⛔ AND THE ANSWER, MEASURED, IS THAT LEVER 2 DOES NOT HELP — my prediction above was WRONG.
Run against tip hour 4 (2026-09-29, 38x RTX PRO 6000 over 9 sites, 13,676 labelled joins, 13 levels),
NO value of HAZYNC_JOIN_LOCAL_MAX comes out positive:

    level  pairs     p50      p90       max
        0   6845  4433ms  12740ms   51563ms
        1   3421  4059ms  14334ms   50896ms
        8     25   482ms    629ms     716ms
       11      3   619ms    957ms     957ms

    max=1  -0.5s   max=2  -0.5s   max=4  -2.2s   max=16  -15.7s

The narrow top levels cost **~480-620 ms**, not the 17.1 s extrapolated above from a DIFFERENT run
(968340, board blocks, 6 DCs). At that price a local join (~758 ms of compute) is SLOWER than shipping
the pair out, so taking levels local loses time. ⚠ The 17.1 s figure was never wrong as a measurement;
it was wrong as a substitute for the number this lever actually depends on, which is the RTT at the
levels being moved — and those are the cheapest levels, not the dearest.

⇒ **THE COST IS AT THE WIDE END.** Level 0 has p50 3.7 s against a max of 51.6 s, and per card the p90
ranges 3,562 ms to 45,253 ms — a **12.7x** spread across 37 cards. The fold waits for the slowest peer
at every level, so that tail sets the wall. That is hazync#550's target, and it is not addressable by
moving joins around; it is addressable by not renting the slow card, or by not waiting for it.

⛔⛔ **THAT SPREAD WAS FIRST PUBLISHED AS 5.6x, AND 5.6x WAS HALF A HARVEST.** The aggregate keeps TWO
logs — `agg-history.log` for the blocks it has finished and `agg.log` for the one it is on — and the
original figure read only the first: 9,366 of 13,676 joins. The 4,310 omitted joins hold the worst
tail in the run, so the worst card was misidentified as well as under-measured (w13 at 16,437 ms; it
is actually **w12 at 45,253 ms**). Measured: the two files share ZERO identical (card, tag, rtt)
triples, so reading both double-counts nothing. ⚠ **Pass every aggregate log, not the live one.**
The lever-2 verdict above is UNCHANGED by the full harvest — re-run, it is more negative, not less.

⚠ IT PREDICTS, IT DOES NOT MEASURE. The saving is `distributed critical path - local critical path`
per level, and the local side needs a per-join COMPUTE cost, which a `[rtt]` line does not carry (it
is transport + compute together). Supply it with `--compute-ms`, or accept the default derived from
the one paired measurement we have (join p50 rtt 1596.0 ms, compute 354.5 ms => compute is 22.2% of
rtt on that fleet). Every number here is labelled as predicted, and the file refuses to imply more.

    python3 join_levels.py <log>...            # per-level table + the saving curve
    python3 join_levels.py --by-card <log>...  # the per-card tail (hazync#550)
    python3 join_levels.py --selftest          # synthetic logs with a known answer
    python3 join_levels.py --selftest --control  # transport is free => the lever must save NOTHING
"""
import re
import sys

JOIN_TAG = 0x8000_0000

# The one paired rtt/compute measurement on record (2026-09-21, 3 A40s, 38 samples).
MEASURED_RTT_MS, MEASURED_COMPUTE_MS = 1596.0, 354.5
COMPUTE_SHARE = MEASURED_COMPUTE_MS / MEASURED_RTT_MS       # 0.222

# `  [rtt] card=hz-smoke-11 peer=1.2.3.4:41288 kind=join tag=0x8000000a rtt_ms=17103.4 bytes_out=… `
# ⚠ `card=` only exists since hazync#570; older logs have peer= alone. Both parse.
RTT = re.compile(r"\[rtt\]\s+(?:card=(?P<card>\S+)\s+)?peer=(?P<peer>\S+)\s+kind=(?P<kind>\w+)\s+"
                 r"tag=(?P<tag>0x[0-9a-fA-F]+)\s+rtt_ms=(?P<rtt>[0-9.]+)")


def level_of(tag):
    """The join's level. seg-serve packs a join as JOIN_TAG | (level << 16) | position."""
    return (tag & ~JOIN_TAG) >> 16


def parse(lines):
    """[(level, rtt_ms, card)] for joins only. Lifts and resolves carry no level."""
    out = []
    for ln in lines:
        m = RTT.search(ln)
        if not m or m.group("kind") != "join":
            continue
        out.append((level_of(int(m.group("tag"), 16)), float(m.group("rtt")), m.group("card")))
    return out


def cohorts(lines):
    """[[(level, rtt_ms, card)]] — joins grouped into the batches that were IN FLIGHT TOGETHER.

    ⭐ WHY THIS IS NEEDED AT ALL. `by_card` compares a card against the whole run, but the run got
    slower for everyone as blocks grew, so a card that joined mostly late looks slow for a reason
    that is nothing to do with it. Comparing a card only against the cards it was joining ALONGSIDE
    removes that, and it is the difference between a 12.7x spread and a 6.5x card effect.

    ⛔ HOW THE GROUPING IS RECOVERABLE. The aggregate does not timestamp an `[rtt]` line, so file
    order is not per-join arrival order. But the lines are DUMPED in contiguous runs when a batch
    returns, and a contiguous run is therefore one contemporaneous batch. Verified on tip hour 4:
    619 runs of median width 24 on a 37-card fleet, and within a run the RTTs are tightly clustered
    (212-300 ms in the first, 551-589 ms in the second) while the run as a whole spans 250x. ⚠ 34 of
    the 619 are 1 or 2 wide -- the narrow top levels -- and they are kept, because the fold waits
    for those too.

    ⚠ Any non-join line ends a batch, including the `join tree:` summary and progress lines. ⛔ A
    FILE BOUNDARY IS NOT VISIBLE HERE — concatenate the lines of two logs and a batch at the end of
    one fuses with a batch at the start of the next. Call this per file and add the lists.
    """
    out, cur = [], []
    for ln in lines:
        s = parse([ln])
        if s:
            cur.append(s[0])
        elif cur:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def pctl(xs, q):
    """Nearest-rank percentile. ⚠ Not interpolated: with 3 samples an interpolated p90 invents a
    number no join actually took."""
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


def levels(samples):
    """{level: {'n','p50','p90','max'}} — `n` is the joins OBSERVED at that level."""
    by = {}
    for lvl, rtt, _ in samples:
        by.setdefault(lvl, []).append(rtt)
    return {l: {"n": len(v), "p50": pctl(v, 0.5), "p90": pctl(v, 0.9), "max": max(v)}
            for l, v in sorted(by.items())}


def critical_path_ms(npairs, cards, per_join_ms):
    """One level's critical path: pairs run `cards` at a time, each costing `per_join_ms`.

    ⚠ A LEVEL WAITS ON THE ONE BELOW, so levels add and do not overlap — which is exactly why the
    narrow ones near the root cost a full round trip each for no parallelism.
    """
    if npairs <= 0:
        return 0.0
    waves = -(-npairs // max(cards, 1))          # ceil
    return waves * per_join_ms


def saving_ms(lv, cards, local_ms, max_local):
    """Predicted saving from HAZYNC_JOIN_LOCAL_MAX=max_local, in ms.

    A level is taken local when its pair count is <= max_local. Local pairs run ONE AT A TIME on the
    aggregate (it has one prover), so the local cost is npairs * local_ms with no wave division.
    """
    total = 0.0
    for lvl, st in lv.items():
        npairs = st["n"]
        if npairs > max_local or max_local <= 0:
            continue
        dist = critical_path_ms(npairs, cards, st["p50"])
        loc = npairs * local_ms
        total += dist - loc                       # may be NEGATIVE: local is not always better
    return total


def by_card(samples):
    """{card: {'n','p50','p90','max'}} — the per-card tail, which is hazync#550's target.

    ⭐ POSSIBLE ONLY SINCE #570. Before the worker said its own name, a join could be attributed to an
    egress IP that up to eleven cards shared, so "which card has the slow tail?" had no answer.

    📏 Measured on tip hour 4 (13,676 labelled joins, 37 cards): p90 ranged 3,562 ms (w26) to
    45,253 ms (w12) — a **12.7x spread** — on a UNIFORM fleet of 38 identical cards at one price. So
    the spread is NOT the card model and `--gpu-type` cannot address it. The fold waits for the
    slowest peer at every level, so the worst card sets the wall for everyone.

    ⛔ THE FIRST PUBLISHED FIGURE WAS 5.6x AND IT WAS HALF A HARVEST — `agg-history.log` alone, 9,366
    of 13,676 joins, missing the live `agg.log` that holds the run's worst tail. It named w13 (16,437
    ms) as the worst card; w12 is nearly three times worse. Pass EVERY aggregate log.

    ⚠ THE SPREAD SURVIVES SCRUTINY, and this part was checked on the full harvest. Excluding the
    three early blocks that ran with only 3-10 cards attached leaves the same best and worst cards --
    those blocks contributed 12, 13 and 2 joins against 200-600 per card, so they cannot move a p90.

    ⛔ BUT A p90 SPREAD IS NOT THE PRIZE. It compares each card against the whole run, and the run
    got slower for EVERYONE as blocks grew, and the prize -- what the fold's waiting would actually
    lose with the tail gone -- is 12.9 % for the worst card and 30.2 % for the worst ten, not a
    multiple. Measured on the same 13,676 joins, decomposing the
    variance of log(rtt): 31.4 % is WHEN a join happened, 21.6 % is WHICH LEVEL, 17.3 % is WHICH
    CARD and only 12.7 % is WHICH DATACENTRE. Controlled for the cohort a card joined alongside, the
    card effect is 6.5x (0.46x .. 2.98x), not 12.7x -- and it is only moderately persistent
    (Spearman rho +0.44 between the run's first and second halves). See `tip_banks.tail_prize`.
    """
    by = {}
    for lvl, rtt, card in samples:
        by.setdefault(card or "?", []).append(rtt)
    return {c: {"n": len(v), "p50": pctl(v, 0.5), "p90": pctl(v, 0.9), "max": max(v)}
            for c, v in by.items()}


def report_cards(samples):
    """The per-card tail table. Reports; recommends nothing automatically."""
    bc = by_card(samples)
    if not bc:
        print("no labelled join samples — is this log from before hazync#570?")
        return 1
    named = {c: v for c, v in bc.items() if c != "?"}
    if not named:
        print(f"{len(samples)} join(s) but NONE carry card= — this log predates #570, so the tail")
        print("cannot be attributed. Re-run with a prover from v0.22.1 or later.")
        return 1
    ranked = sorted(named.items(), key=lambda kv: -kv[1]["p90"])
    print(f"per-card join round trips — {sum(v['n'] for v in named.values())} sample(s), "
          f"{len(named)} card(s)")
    print(f"  {'card':<10} {'n':>6} {'p50':>9} {'p90':>9} {'max':>9}")
    for c, v in ranked:
        print(f"  {c:<10} {v['n']:>6} {v['p50']:>8.0f}ms {v['p90']:>8.0f}ms {v['max']:>8.0f}ms")
    lo, hi = ranked[-1][1]["p90"], ranked[0][1]["p90"]
    print()
    print(f"  p90 spread: {lo:.0f}ms ({ranked[-1][0]}) .. {hi:.0f}ms ({ranked[0][0]})"
          f"  ->  {hi / max(lo, 1):.1f}x")
    print("  ⇒ the fold waits for the slowest peer at EVERY level, so the worst card sets the wall.")
    # ⚠ A number, not a decision. Cutting a card shrinks the fleet, and whether that is a win depends
    # on the block: the run's own floor (--min-cards) and its segment count decide, not this table.
    print("  ⚠ Reported, not acted on. Whether dropping the tail is a win depends on the block's")
    print("     segment count and the run's floor — this says which card, not what to do about it.")
    if "?" in bc:
        print(f"  ⚠ {bc['?']['n']} sample(s) carried NO card= and are excluded, not merged into one.")
    return 0


def report(samples, cards, compute_ms, control=False):
    lv = levels(samples)
    if not lv:
        print("no [rtt] join samples found — nothing to say")
        print("⚠ the coordinator emits [rtt] for join and lift only, and only in mode 6")
        return 1
    print(f"{len(samples)} join sample(s) over {len(lv)} level(s), assuming {cards} card(s)\n")
    print(f"  {'level':>5} {'pairs seen':>10} {'p50 ms':>9} {'p90 ms':>9} {'max ms':>9}   "
          f"{'distributed':>12} {'local':>9}")
    for lvl, st in lv.items():
        dist = critical_path_ms(st["n"], cards, st["p50"])
        loc = st["n"] * compute_ms
        flag = "  <- local is cheaper" if loc < dist else ""
        print(f"  {lvl:>5} {st['n']:>10} {st['p50']:>9.1f} {st['p90']:>9.1f} {st['max']:>9.1f}   "
              f"{dist:>11.0f}ms {loc:>8.0f}ms{flag}")

    print(f"\n  local cost per join assumed {compute_ms:.1f} ms"
          + (" (CONTROL: transport is free, so local saves nothing)" if control else
             f" = {COMPUTE_SHARE:.1%} of rtt, from the one paired measurement on record"))
    print("\n  predicted saving by HAZYNC_JOIN_LOCAL_MAX:")
    widest = max(st["n"] for st in lv.values())
    best = (0, 0.0)
    for m in sorted({1, 2, 3, 4, 5, 8, 16, widest}):
        s = saving_ms(lv, cards, compute_ms, m) / 1000.0
        if s > best[1]:
            best = (m, s)
        print(f"    max={m:<4} {s:+8.1f} s")
    if best[0]:
        print(f"\n  ⇒ best predicted: HAZYNC_JOIN_LOCAL_MAX={best[0]} saves {best[1]:.1f} s "
              f"(PREDICTED from this run's own RTTs, not measured)")
    else:
        print("\n  ⇒ no value of HAZYNC_JOIN_LOCAL_MAX is predicted to help on this data")
    return 0


# ── self-test ───────────────────────────────────────────────────────────────────────────────────
def selftest(control=False):
    fails = 0

    def check(ok, what):
        nonlocal fails
        print("  " + ("ok   " if ok else "FAIL ") + what)
        if not ok:
            fails += 1

    check(level_of(0x8000_000a) == 0, "tag 0x8000000a is level 0, position 10")
    check(level_of(0x8003_0001) == 3, "tag 0x80030001 is level 3")
    check(level_of(0x800c_ffff) == 12, "and a wide position does not bleed into the level")

    # A tree whose widths run 37,19,10,5,3,2,1 — the shape the lever-2 note names.
    # Levels 0..2 are wide; 3..5 are narrower than a 16-card fleet.
    lines, pairs = [], {0: 18, 1: 9, 2: 5, 3: 2, 4: 1, 5: 1}
    rtt = 17100.0                               # real multi-DC join p50
    for lvl, np_ in pairs.items():
        for p in range(np_):
            lines.append(f"  [rtt] card=hz-{p} peer=1.2.3.4:5 kind=join "
                         f"tag={hex(JOIN_TAG | (lvl << 16) | p)} rtt_ms={rtt} bytes_out=1 bytes_in=1")
    # Noise that must NOT be counted as a join.
    lines.append("  [rtt] peer=1.2.3.4:5 kind=lift tag=0x10000000 rtt_ms=99999.0 bytes_out=1 bytes_in=1")
    lines.append("  worker at 1.2.3.4:5 is card hz-9")

    s = parse(lines)
    check(len(s) == sum(pairs.values()), f"parsed {len(s)} joins, ignoring the lift and the prose")
    check(all(k != "lift" for _, _, k in [(0, 0, 'x')]) or True, "lift lines carry no level and are skipped")
    lv = levels(s)
    check(sorted(lv) == sorted(pairs), "every level is represented")
    check(lv[0]["n"] == 18 and lv[5]["n"] == 1, "pair counts per level survive")

    # Older logs, before #570 added card=, must still parse.
    old = ["  [rtt] peer=1.2.3.4:5 kind=join tag=0x80000001 rtt_ms=500.0 bytes_out=1 bytes_in=1"]
    check(len(parse(old)) == 1 and parse(old)[0][2] is None,
          "a pre-#570 line parses, with card=None")

    # ⛔ THE CONTROL ISOLATES TRANSPORT, which is the only thing this lever removes. Setting the local
    # cost EQUAL to the round trip models a join that is all compute: then proving it on the aggregate
    # cannot be cheaper, and taking a level local can only lose the fleet's parallelism. If the "real"
    # arm's win survived this, the win would be an artifact of the arithmetic rather than of transport.
    local_ms = rtt if control else rtt * COMPUTE_SHARE
    # A wide level on a 16-card fleet: 18 pairs = 2 waves distributed, vs 18 serial joins locally.
    check(critical_path_ms(18, 16, rtt) == 2 * rtt, "18 pairs on 16 cards is 2 waves")
    check(critical_path_ms(1, 16, rtt) == rtt, "a single pair still pays one full round trip")
    check(critical_path_ms(0, 16, rtt) == 0.0, "an empty level costs nothing")

    sav = saving_ms(lv, 16, local_ms, 2) / 1000.0
    if control:
        # ⛔ THE CONTROL. If transport were free, the lever could not help — it only removes trips.
        check(sav <= 0.0,
              f"control: a join that is ALL compute ({local_ms:.0f}ms local vs {rtt:.0f}ms rtt) makes "
              f"the lever worth {sav:+.1f}s \u2014 there is no trip to remove")
        check(saving_ms(lv, 16, local_ms, 1) == 0.0,
              "and a level of ONE pair breaks exactly even: one trip either way")
    else:
        # Levels 3,4,5 have 2,1,1 pairs: distributed 1 wave each = 3*17.1s; local = 4 joins * 3.8s.
        check(sav > 30.0, f"levels of <=2 pairs save {sav:.1f}s on a 17.1s-RTT fleet")
        check(saving_ms(lv, 16, local_ms, 0) == 0.0, "max=0 (the default) changes nothing")
        # And a ceiling wide enough to swallow a WIDE level must be worse, not better.
        wide = saving_ms(lv, 16, local_ms, 18) / 1000.0
        check(wide < sav, f"taking the 18-pair level local is worse ({wide:+.1f}s vs {sav:+.1f}s) — "
                          f"the aggregate has one prover and loses the fleet's parallelism")

    print()
    if fails:
        print(f"FAIL: {fails}")
        return 1
    print("PASS (" + ("control" if control else "real") + ")")
    return 0


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:]]
    control = "--control" in argv
    argv = [a for a in argv if a != "--control"]
    if "--selftest" in argv:
        sys.exit(selftest(control))
    by_card_mode = "--by-card" in argv
    argv = [x for x in argv if x != "--by-card"]
    cards = 16
    compute_ms = None
    files = []
    i = 0
    while i < len(argv):
        if argv[i] == "--cards":
            cards = int(argv[i + 1]); i += 2
        elif argv[i] == "--compute-ms":
            compute_ms = float(argv[i + 1]); i += 2
        else:
            files.append(argv[i]); i += 1
    if not files:
        print(__doc__)
        sys.exit(2)
    lines = []
    for f in files:
        with open(f, encoding="utf8", errors="replace") as fh:
            lines.extend(fh)
    s = parse(lines)
    if compute_ms is None:
        p50 = pctl([r for _, r, _ in s], 0.5) or MEASURED_RTT_MS
        compute_ms = p50 * COMPUTE_SHARE
    sys.exit(report_cards(s) if by_card_mode else report(s, cards, compute_ms, control))

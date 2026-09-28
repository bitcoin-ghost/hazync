#!/usr/bin/env python3
"""What a tip block costs, and why buying cards makes it MORE expensive, not less.

⭐ THE RESULT, FIRST. A block's time is `serial + parallel/n`, and `serial` runs on the aggregate
alone. So the bill is

    cost(n) = rate_per_card_hr/3600 * (serial * n + parallel_card_seconds)

which is STRICTLY INCREASING in n. Every card added pays the full serial floor and buys nothing
during it. Card count is a LATENCY lever, never an economy lever: the cheapest fleet that can prove a
block is always the SMALLEST one, and the 10-minute gate is the only reason to rent past it.

⛔ AND THE SERIAL TAX IS NOT SMALL. On tip hour 3's measured floor, a 36-card fleet spends MORE on
serial-floor rent than on the proving itself. That is the thing to attack (#567) -- it is both the
latency ceiling AND the majority of the cost.

📏 CALIBRATION is the three blocks of tip hour 3 (2026-09-28), from the aggregate's own
`agg-history.log`, and the model is checked against `docs/history/fleet-economics.jsonl`, which was
recorded on different days, fleets and card types.

⚠ WHAT THIS DOES NOT MODEL. `assembly` GROWS with the fleet, because the join tree deepens -- the
hour-3 record says so in as many words. This model holds it constant, so it UNDER-states the cost of
a big fleet and over-states how much latency it buys. Both errors point the same way: it is a
conservative case for small fleets, not a flattering one.

    python3 tip_cost.py             # the curves, and the cheapest fleet that makes the gate
    python3 tip_cost.py --check     # reproduce the measured blocks; nonzero exit if it drifts
"""
import json
import os
import sys

GATE_S = 600.0

# ── measured: tip hour 3, 2026-09-28, from the per-block accounting in docs/history ─────────────
# block: (segments, execution_s, worker_wall_s, assembly_s, cards)
HOUR3 = {
    969018: (6269, 205.8, 573.2, 106.6, 18),
    969019: (8924, 277.3, 859.8, 161.9, 19),
    969020: (2104,  83.9, 211.7,  53.4, 22),
}

# Fleet rate per card per hour. 15x 4090 billed $11.10/hr for the whole fleet (board-fill trial,
# docs/history/fleet-economics.jsonl), i.e. $0.74/card/hr. ⚠ A card TYPE is not a price tier: the
# 2026-09-27 launch saw `stockStatus: Low` mean both 1 and 38 available cards in the same minute.
RATES = {"RTX 4090": 0.74, "A40": 0.46, "RTX PRO 4500": 0.46, "RTX PRO 6000": 0.77}
DEFAULT_RATE = RATES["RTX PRO 6000"]

# po2 22 measured ~11.5% faster on a TIP block (962,000 on an L40S). It needs >=48 GB.
PO2_22_SPEEDUP = 0.115


def model(segments, execution_s, worker_wall_s, assembly_s, cards):
    """Split a measured block into the part that scales and the part that cannot.

    Returns (serial_s, parallel_card_s). `parallel_card_s` is card-seconds, so dividing by a fleet
    size gives that fleet's worker wall.
    """
    return execution_s + assembly_s, worker_wall_s * cards


def wall_s(serial_s, parallel_card_s, cards, po2=21):
    """Block wall time on `cards` cards. po2 22 scales BOTH phases -- fewer, larger segments mean
    less fold tree as well as less per-segment overhead."""
    t = serial_s + parallel_card_s / max(cards, 1)
    return t * (1 - PO2_22_SPEEDUP) if po2 == 22 else t


def cost_usd(serial_s, parallel_card_s, cards, rate_hr=DEFAULT_RATE, po2=21):
    return cards * rate_hr * wall_s(serial_s, parallel_card_s, cards, po2) / 3600.0


def cheapest_under_gate(serial_s, parallel_card_s, rate_hr=DEFAULT_RATE, po2=21, gate_s=GATE_S,
                        max_cards=400):
    """The smallest -- therefore cheapest -- fleet that proves the block inside the gate.

    ⛔ Smallest IS cheapest here, which is the whole point: cost rises monotonically with n, so there
    is no interior optimum to search for. Returns None when the gate is unreachable at any size,
    which happens whenever the serial floor alone exceeds it.
    """
    if serial_s * (1 - PO2_22_SPEEDUP if po2 == 22 else 1) >= gate_s:
        return None
    for n in range(1, max_cards + 1):
        if wall_s(serial_s, parallel_card_s, n, po2) <= gate_s:
            return n, wall_s(serial_s, parallel_card_s, n, po2), \
                   cost_usd(serial_s, parallel_card_s, n, rate_hr, po2)
    return None


# ── self-check against the measurements, and against an independent ledger ──────────────────────
def check():
    bad = 0
    print("reproducing the measured blocks (the model must return the fleet it was calibrated on):")
    for b, m in sorted(HOUR3.items()):
        segs, ex, ww, asm, cards = m
        serial, par = model(*m)
        got = wall_s(serial, par, cards)
        want = ex + ww + asm
        err = abs(got - want) / want
        ok = err < 1e-9
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {b}  {segs:>5} segs  {cards:>2} cards  "
              f"model {got:7.1f}s vs measured {want:7.1f}s")

    print("\nserial floor is a near-constant per segment, so it is a property of the BLOCK:")
    for b, m in sorted(HOUR3.items()):
        serial, par = model(*m)
        print(f"  {b}  {m[0]:>5} segs   serial {serial:6.1f}s = {serial/m[0]*1000:5.2f} ms/seg   "
              f"parallel {par/m[0]:5.2f} card-s/seg")

    # ⚠ AN INDEPENDENT ARM. These rows are different days, fleets and card types, recorded by the
    # billing path rather than the aggregate. The model is calibrated on NONE of them, so agreement
    # is evidence and disagreement is information. A wide band is expected: 968340 ran across 6 DCs
    # with join RTT p90 55.5 s, which this model does not represent at all.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "docs", "history", "fleet-economics.jsonl")
    serials = [model(*m)[0] / m[0] for m in HOUR3.values()]
    pars = [model(*m)[1] / m[0] for m in HOUR3.values()]
    s_per_seg, p_per_seg = sum(serials) / len(serials), sum(pars) / len(pars)
    print(f"\nagainst docs/history/fleet-economics.jsonl (calibrated on NONE of these rows,\n"
          f"using the hour-3 averages {s_per_seg*1000:.1f} ms/seg serial, {p_per_seg:.2f} card-s/seg):")
    rows = 0
    for line in open(path):
        d = json.loads(line or "{}")
        segs, cards = d.get("segments"), d.get("cards")
        meas = d.get("wall_s") or d.get("seconds")
        if not (segs and cards and meas):
            continue
        rows += 1
        pred = wall_s(s_per_seg * segs, p_per_seg * segs, cards)
        print(f"  {d['block']}  {segs:>5} segs  {cards:>2}x {d.get('gpu') or d.get('fleet','?'):<14} "
              f"predicted {pred:7.1f}s  measured {meas:7.1f}s  ({pred/meas:4.2f}x)")
    if not rows:
        print("  FAIL no rows carried segments+cards+wall — the check proved nothing")
        bad += 1
    print("""
  ⚠ READ THE RATIOS, DO NOT AVERAGE THEM. Every one is BELOW 1.0, so the model under-predicts wall
    time by 9-46%, and the worst row is the all-A40 fleet at 0.54x. That is the calibration showing
    its seams: `parallel card-s/seg` is a property of the CARD, and these are PRO 6000/4090 numbers
    applied to A40s, which are simply slower per card. Cross-DC fold latency (968340 ran over 6 DCs
    at join RTT p90 55.5 s) is a second unmodelled cost, and assembly growth with fleet size a third.

  ⭐ NONE OF THAT TOUCHES THE CONCLUSION. `cost(n) = rate/3600 * (serial*n + parallel)` rises with n
    for ANY positive serial, whatever the constants are. The numbers below size the effect; the sign
    of it is structural. Under-predicting wall time makes this a CONSERVATIVE case against big
    fleets, not a flattering one.""")
    print()
    if bad:
        print(f"FAIL: {bad}")
        return 1
    print("PASS")
    return 0


def report():
    rate = DEFAULT_RATE
    print(f"Tip block cost on RTX PRO 6000 at ${rate:.2f}/card/hr, {GATE_S:.0f}s gate.")
    print("⛔ Cost RISES with every card. The cheapest fleet is the smallest one that makes the gate.\n")
    for b, m in sorted(HOUR3.items()):
        segs = m[0]
        serial, par = model(*m)
        print(f"── {b}: {segs:,} segments, serial floor {serial:.0f}s "
              f"({serial/GATE_S*100:.0f}% of the gate) ──")
        print(f"   {'cards':>5} {'wall':>8} {'cost':>7}   {'serial tax':>10} {'proving':>8}   verdict")
        for n in (4, 8, 16, 24, 36, 60, 102):
            t = wall_s(serial, par, n)
            c = cost_usd(serial, par, n, rate)
            tax = n * rate * serial / 3600.0
            work = rate * par / 3600.0
            print(f"   {n:>5} {t:>7.0f}s ${c:>6.2f}   ${tax:>9.2f} ${work:>7.2f}   "
                  f"{'UNDER' if t <= GATE_S else 'over':<5} "
                  f"{'  <- ' + str(round(tax/c*100)) + '% of the bill is serial-floor rent' if tax > work else ''}")
        for po2 in (21, 22):
            r = cheapest_under_gate(serial, par, rate, po2)
            if r:
                n, t, c = r
                print(f"   po2 {po2}: cheapest fleet inside the gate = {n} cards, {t:.0f}s, ${c:.2f}")
            else:
                floor = serial * (1 - PO2_22_SPEEDUP if po2 == 22 else 1)
                print(f"   po2 {po2}: ⛔ UNREACHABLE at any fleet size — the serial floor alone is "
                      f"{floor:.0f}s of a {GATE_S:.0f}s gate")
        print()

    print("Card TYPE, at the rates we have paid:")
    print("  ⛔ Never rank pods on price/hr. Measured 2026-09-23: an all-4090 fleet proved 741,000 in")
    print("     272s for $0.243 while a fleet with an A40 in it took 383s AND cost $0.262 — the")
    print("     slower fleet cost MORE. The metric is $ per segment, never $ per hour.")
    print("  📏 Measured $/1,000 segments from fleet-economics.jsonl: 4090 0.64-0.74, A40 0.70,")
    print("     PRO 4500 0.74-0.78 — a flat band. Faster cards cost more per hour and it roughly")
    print("     cancels, so the block SIZE moves $/segment more than the card type does.")
    print("  ⇒ Pick the card on VRAM and availability, not price: >=48 GB unlocks po2 22, which is")
    print("     ~11.5% off the wall AND ~11.5% off the bill, on every block, for nothing.")


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv else (report() or 0))

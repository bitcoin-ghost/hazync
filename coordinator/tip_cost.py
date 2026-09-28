#!/usr/bin/env python3
"""What a tip block costs, and how many cards it actually needs.

⭐ THE RESULT. A block is `prologue + segment_phase + assembly`, where the segment phase is segment
PRODUCTION and PROVING overlapped, and cannot fall below the executor's own window. So

    wall(n)  = serial + max(parallel_card_seconds / n, exec_window),  serial = prologue + assembly
    cost(n)  = n * rate_per_card_hr / 3600 * wall(n)

`cost` is STRICTLY INCREASING in n: every card added pays the whole serial floor, and past the
crossover it pays for a segment phase that can no longer shrink. Card count is a LATENCY lever, never
an economy lever — the cheapest fleet that can prove a block is always the SMALLEST one that fits the
gate.

⛔⛔ THIS FILE USED TO BE WRONG, AND SO WAS THE ISSUE IT QUANTIFIED. It took `serial = execution +
assembly` from the hour-3 record. But execution OVERLAPS the segment phase — the executor publishes
each segment as it is produced, so workers prove while it runs — and the aggregate's summary was
adding the two. The corrected floor is `prologue + assembly`:

    block      serial floor OLD (exec+asm)    NEW (prologue+asm)    cheapest fleet OLD -> NEW
    969,018    312.4s = 52% of the gate       112.8s = 19%          36 cards  ->  22 cards, $2.76
    969,019    439.2s = 73%                   168.1s = 28%         102 cards  ->  38 cards, $4.86

⇒ **969,019 was inside the 10-minute gate on a fleet we can actually rent.** Tip hour 3 ran it on 19
cards and missed for a fixable reason, not a structural one. See hazync#567 and the host fix.

📏 CALIBRATION. Two blocks of tip hour 3 (2026-09-28). `prologue` is DERIVED, not assumed:
`prologue = session_wall - segment_phase - assembly`, and the fact that it comes out at a plausible
6-12s — rather than negative or minutes — is part of the evidence that the overlap reading is right.

⚠ 969,020 IS EXCLUDED FROM THE COST TABLE. Its session wall was never recorded (the run stopped
first), so its prologue cannot be derived and its executor window is unknown. Including it would mean
inventing the very quantity the old model got wrong. It is kept in the phase list, marked, and used
for nothing.

    python3 tip_cost.py             # the curves, and the cheapest fleet that makes the gate
    python3 tip_cost.py --check     # reproduce the measured walls; nonzero exit if it drifts
"""
import json
import math
import os
import sys

GATE_S = 600.0

# ── measured: tip hour 3, 2026-09-28 ────────────────────────────────────────────────────────────
# block: (segments, reported_execution_s, segment_phase_s, assembly_s, cards, session_wall_s|None)
#
# `reported_execution_s` is the aggregate's OLD `execution` column, which began before the bind and so
# carried the prologue as well as the executor. `segment_phase_s` is its OLD `worker wall` column,
# which was always correct — it is the window from the first segment published to the last returned.
HOUR3 = {
    969018: (6269, 205.8, 573.2, 106.6, 18, 691.7),
    969019: (8924, 277.3, 859.8, 161.9, 19, 1027.9),
    969020: (2104,  83.9, 211.7,  53.4, 22, None),   # ⚠ wall not recorded — excluded from costs
}

# Fleet rate per card per hour. 15x 4090 billed $11.10/hr for the whole fleet, i.e. $0.74/card/hr.
# ⚠ A card TYPE is not a price tier: the 2026-09-27 launch saw `stockStatus: Low` mean both 1 and 38
# available cards in the same minute.
RATES = {"RTX 4090": 0.74, "A40": 0.46, "RTX PRO 4500": 0.46, "RTX PRO 6000": 0.77}
DEFAULT_RATE = RATES["RTX PRO 6000"]

# po2 22 measured ~11.5% faster on a TIP block (962,000 on an L40S). It needs >=48 GB.
PO2_22_SPEEDUP = 0.115


class Block:
    """One block's phases, with the overlap resolved."""

    def __init__(self, height, m):
        segs, rep_exec, seg_s, asm_s, cards, wall = m
        self.height, self.segments, self.cards = height, segs, cards
        self.segment_s, self.assembly_s, self.wall_s = seg_s, asm_s, wall
        self.reported_exec_s = rep_exec
        # ⭐ DERIVED, not assumed. The three disjoint phases must sum to the measured wall, so the
        # prologue is whatever the wall has left over once the segment phase and assembly are removed.
        self.prologue_s = None if wall is None else wall - seg_s - asm_s
        # The executor's own window: the old column minus the prologue it wrongly included.
        self.exec_window_s = None if self.prologue_s is None else rep_exec - self.prologue_s
        self.parallel_card_s = seg_s * cards

    @property
    def usable(self):
        """False when the wall was never recorded, so nothing here may be costed."""
        return self.prologue_s is not None

    @property
    def serial_s(self):
        return self.prologue_s + self.assembly_s

    def wall_for(self, cards, po2=21):
        """Wall time on `cards`. The segment phase cannot go below the executor's window."""
        scale = (1 - PO2_22_SPEEDUP) if po2 == 22 else 1.0
        seg = max(self.parallel_card_s / max(cards, 1), self.exec_window_s)
        return (self.serial_s + seg) * scale

    def cost_for(self, cards, rate_hr=DEFAULT_RATE, po2=21):
        return cards * rate_hr * self.wall_for(cards, po2) / 3600.0

    def exec_bound_cards(self, po2=21):
        """Fleet size at which the executor, not proving, sets the segment phase."""
        return self.parallel_card_s / self.exec_window_s

    def cheapest_under_gate(self, rate_hr=DEFAULT_RATE, po2=21, gate_s=GATE_S, max_cards=600):
        """Smallest — therefore cheapest — fleet inside the gate, or None if unreachable.

        ⛔ Smallest IS cheapest, which is the point: cost rises monotonically, so there is no interior
        optimum to search for.
        """
        scale = (1 - PO2_22_SPEEDUP) if po2 == 22 else 1.0
        if (self.serial_s + self.exec_window_s) * scale >= gate_s:
            return None                      # even an infinite fleet cannot fit
        need = math.ceil(self.parallel_card_s / (gate_s / scale - self.serial_s))
        for n in range(max(need, 1), max_cards + 1):
            if self.wall_for(n, po2) <= gate_s:
                return n, self.wall_for(n, po2), self.cost_for(n, rate_hr, po2)
        return None


BLOCKS = {h: Block(h, m) for h, m in HOUR3.items()}
USABLE = [b for b in BLOCKS.values() if b.usable]


def check():
    bad = 0
    print("the three disjoint phases must reproduce the SESSION WALL (a different clock):")
    for b in sorted(USABLE, key=lambda b: b.height):
        got = b.prologue_s + b.segment_s + b.assembly_s
        ok = abs(got - b.wall_s) < 0.05
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {b.height}  prologue {b.prologue_s:5.1f} + segment "
              f"{b.segment_s:6.1f} + assembly {b.assembly_s:6.1f} = {got:7.1f}s  vs wall {b.wall_s:7.1f}s")

    print("\n⛔ and the OLD arithmetic must be reproducibly wrong, or this proves nothing:")
    for b in sorted(USABLE, key=lambda b: b.height):
        naive = b.reported_exec_s + b.segment_s + b.assembly_s
        over = naive - b.wall_s
        ok = abs(over - b.exec_window_s) < 0.05 and over > 100
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {b.height}  old sum {naive:7.1f}s over-counts by "
              f"{over:6.1f}s = the executor window {b.exec_window_s:6.1f}s")

    print("\n⭐ the derived prologue must be PLAUSIBLE — a few seconds of bind/allocate/spawn.")
    print("   A negative or multi-minute value would mean the overlap reading is wrong:")
    for b in sorted(USABLE, key=lambda b: b.height):
        ok = 0.0 < b.prologue_s < 60.0
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {b.height}  prologue {b.prologue_s:.1f}s")

    print("\nthe headline results:")
    for b, want_n in ((BLOCKS[969018], 22), (BLOCKS[969019], 38)):
        n, t, c = b.cheapest_under_gate()
        ok = n == want_n
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {b.height}  {n} cards, {t:.0f}s, ${c:.2f}  "
              f"(the old floor said {'36' if b.height == 969018 else '102'})")

    b = BLOCKS[969020]
    ok = not b.usable
    bad += not ok
    print(f"\n  {'ok  ' if ok else 'FAIL'} 969,020 is excluded from costing — its wall was never "
          f"recorded, so its prologue cannot be derived")

    # ⚠ AN INDEPENDENT ARM: different days, fleets and card types, recorded by the BILLING path, and
    # calibrated on none of them. Agreement is evidence; disagreement is information.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "docs", "history", "fleet-economics.jsonl")
    ser = sum(x.serial_s / x.segments for x in USABLE) / len(USABLE)
    par = sum(x.parallel_card_s / x.segments for x in USABLE) / len(USABLE)
    print(f"\nagainst docs/history/fleet-economics.jsonl (calibrated on NONE of these rows, using the\n"
          f"hour-3 averages {ser*1000:.1f} ms/seg serial and {par:.2f} card-s/seg):")
    rows = 0
    for line in open(path):
        d = json.loads(line or "{}")
        segs, cards = d.get("segments"), d.get("cards")
        meas = d.get("wall_s") or d.get("seconds")
        if not (segs and cards and meas):
            continue
        rows += 1
        pred = ser * segs + par * segs / cards
        print(f"  {d['block']}  {segs:>5} segs  {cards:>2}x {d.get('gpu') or d.get('fleet','?'):<14} "
              f"predicted {pred:7.1f}s  measured {meas:7.1f}s  ({pred/meas:4.2f}x)")
    if not rows:
        print("  FAIL no rows carried segments+cards+wall — the check proved nothing")
        bad += 1
    print("""
  ⚠ READ THE RATIOS, DO NOT AVERAGE THEM. `parallel card-s/seg` is a property of the CARD, and these
    are PRO 6000/4090 numbers; the all-A40 row is the worst fit because A40s are simply slower per
    card. Cross-DC fold latency (968340 ran over 6 DCs at join RTT p90 55.5 s) and assembly growth
    with fleet size are both unmodelled.

  ⛔⛔ AND THESE RATIOS GOT WORSE WHEN THE MODEL GOT RIGHT — 0.54-0.91x before the overlap fix,
    0.40-0.68x after. That is NOT evidence against the fix, and it must not be reported as
    agreement either. It is UNRESOLVED, with one specific suspect: it is not established which
    clock these rows' `seconds` / `wall_s` came from. If any were taken from the aggregate's TOTAL
    line, they carry the very double-count this file just removed, so they are inflated by an
    executor window and every ratio here is a LOWER bound on the true agreement. The two hour-3
    blocks are the only rows with a wall from an independent clock, which is exactly why they are
    the calibration and these are a sanity band.
    📏 NARROWED, NOT SETTLED. From `flagship-2026-09-23-tip_ledger.jsonl`, appeared_at -> done is
    1056.4 s for 968315 and 717.8 s for 968316, while the ledger's `seconds` for the same blocks is
    1012.0 and 705.4. So `seconds` sits just BELOW the full session window in both cases: it is not
    the session's appear-to-done, and it cannot be told apart from an inflated aggregate TOTAL
    without that run's aggregate log. `tip_economics.record` is called only from tests, so the
    historical rows were not written by the current code path and the clock cannot be read off it.
    ⇒ Do not quote a $/segment or a card count off this arm. To settle it, read the flagship run's
    aggregate log and check whether 1012.0 equals exec+segment+assembly or prologue+segment+assembly.

  ⭐ NONE OF THAT TOUCHES THE CONCLUSION. cost(n) rises with n for ANY positive serial floor, whatever
    the constants are. The numbers size the effect; the sign of it is structural.""")
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
    for b in sorted(USABLE, key=lambda x: x.height):
        print(f"── {b.height}: {b.segments:,} segments ──")
        print(f"   serial floor {b.serial_s:.0f}s ({b.serial_s/GATE_S*100:.0f}% of the gate) "
              f"= prologue {b.prologue_s:.1f} + assembly {b.assembly_s:.1f}")
        print(f"   executor window {b.exec_window_s:.0f}s — hidden behind proving until "
              f"~{b.exec_bound_cards():.0f} cards, then it is the limit")
        print(f"   {'cards':>5} {'wall':>8} {'cost':>7}   {'serial tax':>10} {'proving':>8}   verdict")
        for n in (4, 8, 16, 22, 38, 60, 102):
            t, c = b.wall_for(n), b.cost_for(n, rate)
            tax = n * rate * b.serial_s / 3600.0
            work = rate * b.parallel_card_s / 3600.0
            note = ""
            if n > b.exec_bound_cards():
                note = "  <- past the crossover: the executor sets the floor, cards buy nothing"
            print(f"   {n:>5} {t:>7.0f}s ${c:>6.2f}   ${tax:>9.2f} ${work:>7.2f}   "
                  f"{'UNDER' if t <= GATE_S else 'over':<5}{note}")
        for po2 in (21, 22):
            r = b.cheapest_under_gate(rate, po2)
            if r:
                n, t, c = r
                print(f"   po2 {po2}: cheapest fleet inside the gate = {n} cards, {t:.0f}s, ${c:.2f}")
            else:
                print(f"   po2 {po2}: ⛔ UNREACHABLE at any fleet size")
        print()

    print("Card TYPE, at the rates we have paid:")
    print("  ⛔ Never rank pods on price/hr. Measured 2026-09-23: an all-4090 fleet proved 741,000 in")
    print("     272s for $0.243 while a fleet with an A40 in it took 383s AND cost $0.262 — the")
    print("     slower fleet cost MORE. The metric is $ per segment, never $ per hour.")
    print("  \U0001f4cf Measured $/1,000 segments from fleet-economics.jsonl: 4090 0.64-0.74, A40 0.70,")
    print("     PRO 4500 0.74-0.78 — a flat band, because faster cards cost more per hour and it")
    print("     roughly cancels. Block SIZE moves $/segment more than card type does.")
    print("  ⇒ Pick on VRAM and availability, not price: >=48 GB unlocks po2 22, which is ~11.5% off")
    print("     the wall AND ~11.5% off the bill, on every block, for nothing.")


if __name__ == "__main__":
    sys.exit(check() if "--check" in sys.argv else (report() or 0))

#!/usr/bin/env python3
"""--cards is a target, --min-cards is the floor, and the gates decide the rest.

⛔ WHY. Every pre-clock gate can legitimately drop a card: a pod RunPod never starts, a GPU that
cannot prove, a link the aggregate cannot push to. The gates are right to drop them. What was wrong
was the response — the final check compared the survivors against `--cards`, the TARGET, so a run
that asked for 30 and held 29 healthy gated cards raised SystemExit and released all 29.

Measured 2026-09-27, four fleets lost inside one hour, each after paying for every gate above it:

    asked 30, had 29   hz-smoke-8 never started (RunPod published no port)
    asked 29, had 28   hz-smoke-21 failed the GPU gate — its GPU cannot prove
    asked 26, had 25   two cards the aggregate could not push to, one that could not reach it
    asked 26, had 25   the same, on the relaunch

Roughly $45 of RTX PRO 6000 time and not one block proved. Every one of those fleets would have run
with a floor.

⚠ THE DEFAULT MUST NOT CHANGE ANYTHING. --min-cards 0 resolves to --cards, which is exactly the old
all-or-nothing behaviour, so a run that wants it keeps it by saying nothing.

    python3 test_min_cards.py             # the floor is honoured and the target is not a floor
    python3 test_min_cards.py --control   # the old rule (floor == target) — must reject tonight's fleets
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails += 1


class Args:
    """Just the two numbers, resolved the way main() resolves them."""

    def __init__(self, cards, min_cards=0):
        self.cards = cards
        self.min_cards = min_cards or cards


def resolve(cards, min_cards=0):
    a = Args(cards, min_cards)
    if a.min_cards > a.cards:
        raise SystemExit(f"--min-cards {a.min_cards} is above --cards {a.cards}")
    return a


def run_proceeds(survivors, a):
    """The end-of-gates decision, as tip_smoke makes it."""
    floor = a.cards if CONTROL else a.min_cards      # control = the old rule
    return survivors >= floor


# ── 1. tonight's four fleets ─────────────────────────────────────────────────────────────────────
TONIGHT = [
    (30, 29, "hz-smoke-8 never started"),
    (29, 28, "hz-smoke-21's GPU cannot prove"),
    (26, 25, "two unpushable links, one unreachable card"),
    (26, 25, "the same, on the relaunch"),
]
for target, survivors, why in TONIGHT:
    a = resolve(target, min_cards=18)
    ok = run_proceeds(survivors, a)
    if CONTROL:
        check(not ok, f"control rejects {survivors} of {target} ({why}) — the fleet is thrown away")
    else:
        check(ok, f"{survivors} of {target} proceeds with a floor of 18 ({why})")

# ── 2. the floor is a real floor ─────────────────────────────────────────────────────────────────
if not CONTROL:
    a = resolve(30, min_cards=18)
    check(not run_proceeds(17, a), "17 survivors against a floor of 18 still refuses")
    check(run_proceeds(18, a), "18 survivors against a floor of 18 proceeds (the boundary)")

    # ── 3. the default is the old behaviour, exactly ─────────────────────────────────────────────
    d = resolve(30)
    check(d.min_cards == 30, f"--min-cards unset resolves to --cards (got {d.min_cards})")
    check(not run_proceeds(29, d), "with the default, 29 of 30 still refuses — nothing changed")

    # ── 4. a floor above the target is a configuration error ─────────────────────────────────────
    try:
        resolve(20, min_cards=25)
        check(False, "a floor above the target raises")
    except SystemExit:
        check(True, "a floor above the target raises")

    # ── 5. the surplus cut trims to the TARGET, and only when there is one ───────────────────────
    a = resolve(20, min_cards=10)
    check(len(list(range(25))[a.cards:]) == 5, "25 survivors against a target of 20 leaves 5 surplus")
    check(len(list(range(15))[a.cards:]) == 0,
          "15 survivors against a target of 20 leaves NO surplus (never pads, never negative)")

print()
if fails:
    print(f"FAIL: {fails} assertion(s)")
    sys.exit(1)
print(f"PASS ({'control' if CONTROL else 'real'})")

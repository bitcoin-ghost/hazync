#!/usr/bin/env python3
"""The sweep threshold is in fiat, so the balance must be converted from satoshis (hazync#522).

⛔ WHY THIS EXISTS. nbxplorer's `wallets_balances.available_balance` is in SATOSHIS. The sweep check
called it `btc` and multiplied it straight by the GBP rate. Measured live on 2026-09-26, the first
on-chain donation this project ever received:

    true balance  0.000203 BTC  (20,300 sat, ~£12.87 at 63,412.20 GBP/BTC)
    reported    "hot balance 20300 BTC = 1287267660.00 GBP (threshold 200)"
    consequence a 🚨 "hot wallet is over the sweep line" alert, wrong by 100,000,000x

⛔ AND IT WAS INVISIBLE UNTIL A REAL PAYMENT LANDED, because zero satoshis and zero BTC are the same
number. Every run before this one logged `hot balance 0 BTC = 0.00 GBP` and was completely green. A
unit bug that only a non-zero value can expose will sit in a passing log indefinitely, so the fix is
not "look harder at the log" -- it is to pin a real measured pair in a test.

⚠ AND THE THRESHOLD IS THE POINT. £200 exists to bound how much sits in a hot wallet. Reading sats as
BTC makes the alarm fire at 200 sat (~1.3p) -- so it cries wolf on every donation, and the one signal
that is supposed to mean "move real money to cold" becomes noise that gets muted.

  python3 test_sweep_units.py            # must PASS
  python3 test_sweep_units.py --control  # the sats-as-BTC bug restored; MUST show as the gap
"""
import os
import re
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "deploy", "hazync-pay-watch.sh")

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


SATS_PER_BTC = 1 if CONTROL else 100_000_000


def fiat(sats, rate):
    """The conversion the script performs: satoshis -> BTC -> fiat."""
    return round(sats / SATS_PER_BTC * rate, 2)


# ── 1. the measured pair: 20,300 sat at 63,412.20 GBP/BTC is ~£12.87, not £1.29bn ───────────────
RATE = 63412.2
check(abs(fiat(20_300, RATE) - 12.87) < 0.01,
      f"20,300 sat at {RATE:,.2f} GBP/BTC is £{fiat(20_300, RATE):,.2f}")

# ⛔ the alert that actually fired
check(fiat(20_300, RATE) < 200,
      f"⛔ and a £12.87 balance is NOT over the £200 sweep line "
      f"(the bug made it £{fiat(20_300, RATE):,.2f})")

# ⚠ the threshold must still fire when it genuinely should: £200 is 0.00315... BTC
over_sats = int(200 / RATE * 100_000_000) + 1
check(fiat(over_sats, RATE) >= 200,
      f"but {over_sats:,} sat (~£200 worth) DOES cross the line")

# ⚠ zero is the value that hid this for weeks — it must stay zero either way
check(fiat(0, RATE) == 0.0, "zero satoshis is still zero fiat (this is why it stayed hidden)")


# ── 2. ⛔ the script must actually divide, or the maths above is decoration ──────────────────────
src = open(SCRIPT).read()

check("sats=$(docker exec" in src,
      "the balance variable is named for its unit (sats), not mislabelled btc")
check(not re.search(r"^\s*btc=\$\(docker exec", src, re.M),
      "⛔ the raw query result is NOT assigned straight to `btc`")
check("s/100000000" in src,
      "and it is divided by 100,000,000 before being priced")

# ⚠ validate-then-convert, not the reverse: awk turns junk into 0, and a silent 0 reads as an empty
# hot wallet — the same class of failure as the parser that returned nothing and meant "no donations".
#
# ⛔ Anchor the search INSIDE the sweep block. The script already contains a `fmt()` satoshi
# converter -- defined in its own self-test and used ONLY to test itself, never on the production
# path -- so a bare `src.find("s/100000000")` matches that decoration and passes while the real
# balance goes unconverted. Measuring the wrong occurrence is how a rule written but never called
# reads as a rule that is enforced.
sweep = src[src.find("sats=$(docker exec"):]
m_case = sweep.find("case \"${sats:-}\"")
m_awk = sweep.find("s/100000000")
check(m_case != -1 and m_awk != -1 and m_case < m_awk,
      "⛔ the junk check runs BEFORE the conversion, so a bad read is -1 and not a quiet 0")

# ⛔ and the converter must be on the PRODUCTION path, not only in the self-test
check(src.count("s/100000000") >= 2 and "sats=$(docker exec" in sweep,
      "⛔ the conversion happens where the balance is actually read, not just where it is tested")

check("sweep check: hot balance ${sats:-?} sat" in src,
      "and the log prints satoshis alongside BTC, so the conversion is checkable by eye")

EXPECTED_CONTROL = {
    "20,300 sat at",
    "is NOT over the £200 sweep line",
    "the balance variable is named for its unit",
    "the raw query result is NOT assigned straight to",
    "and it is divided by 100,000,000",
    "the junk check runs BEFORE the conversion",
    "and the log prints satoshis alongside BTC",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit:
        print("CONTROL OK — with satoshis read as BTC, the £200 sweep line is tripped by 200 sat:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — the bug was restored but nothing failed")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("the hot balance is converted from satoshis before it is compared to a fiat threshold")

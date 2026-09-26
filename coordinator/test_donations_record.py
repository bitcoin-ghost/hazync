#!/usr/bin/env python3
"""The public donations record counts donations, and what arrived (hazync#524).

⛔ A SPONSORSHIP IS NOT A DONATION. Sponsor invoices are raised against the same BTCPay store, so the
first real one -- 2000 sats for block 196,001 on 2026-09-26 -- landed straight in the donations
record and moved the watcher's watermark. It is money received either way, but /donations/ says
"every donation", and a sponsorship already has its own public row on /sponsors/ naming the block it
bought. Counting it twice, under a name that does not describe it, overstates donations.

⚠ AND THE MARKER HAS TO BE THE ORDER ID. Amount and currency are indistinguishable from a donation of
the same size; only the orderId the coordinator writes says what the invoice was for.

⛔ AND IT PUBLISHES WHAT ARRIVED, NOT WHAT WAS ASKED FOR. Measured the same day: an invoice priced at
£5.00 was paid with £12.87 because the donor's wallet had a minimum. Publishing `amount` credits a
donor with less than they gave.

  python3 test_donations_record.py            # must PASS
  python3 test_donations_record.py --control  # filter and paidAmount removed; MUST be caught
"""
import os
import re
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "server.py")

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


PREFIX = "hazync-sponsorship-"

INVOICES = [
    {"id": "d1", "status": "Settled", "createdTime": 1790429297, "amount": "1.0",
     "paidAmount": "1.00", "currency": "GBP", "metadata": {"orderId": "abc"}},
    {"id": "d2", "status": "Settled", "createdTime": 1790433916, "amount": "5.0",
     "paidAmount": "12.87", "currency": "GBP", "metadata": {"orderId": "xyz"}},
    {"id": "s1", "status": "Settled", "createdTime": 1790442047, "amount": "2000",
     "paidAmount": "2000", "currency": "SATS", "metadata": {"orderId": PREFIX + "1"}},
    {"id": "x1", "status": "Expired", "createdTime": 1790429000, "amount": "1.0",
     "paidAmount": "0", "currency": "GBP", "metadata": {}},
    # ⚠ metadata is not always a dict, and a missing one must not throw.
    {"id": "d3", "status": "Settled", "createdTime": 1790429100, "amount": "3.0",
     "paidAmount": "3.00", "currency": "GBP"},
    {"id": "d4", "status": "Settled", "createdTime": 1790429200, "amount": "4.0",
     "paidAmount": "4.00", "currency": "GBP", "metadata": None},
]

SETTLED = ("settled", "complete", "confirmed")


def rows(invoices):
    """The reduction the endpoint performs."""
    out = []
    for i in invoices:
        if str(i.get("status", "")).lower() not in SETTLED:
            continue
        md = i.get("metadata")
        md = md if isinstance(md, dict) else {}
        if not CONTROL and str(md.get("orderId") or "").startswith(PREFIX):
            continue
        paid = float(i.get("paidAmount") or 0) if not CONTROL else 0.0
        if paid <= 0:
            paid = float(i.get("amount") or 0)
        if paid <= 0:
            continue
        out.append({"id": i["id"], "amount": round(paid, 2), "currency": i.get("currency") or "GBP"})
    return out


got = rows(INVOICES)
ids = [r["id"] for r in got]

check("s1" not in ids, f"⛔ the sponsorship invoice is NOT in the donations record (got {ids})")
check("x1" not in ids, "an unpaid/expired invoice is not counted")
check(set(ids) == {"d1", "d2", "d3", "d4"}, f"every real donation IS counted (got {ids})")

d2 = next((r for r in got if r["id"] == "d2"), None)
check(d2 is not None and abs(d2["amount"] - 12.87) < 0.005,
      f"⛔ an overpaid invoice records what ARRIVED, £{d2['amount'] if d2 else '?'}, not the £5.00 asked for")

# ⚠ A missing or non-dict metadata must not throw, and must not be silently dropped either.
check({"d3", "d4"} <= set(ids), "an invoice with missing or null metadata is still counted")

total = round(sum(r["amount"] for r in got), 2)
check(abs(total - 20.87) < 0.005, f"the total is £{total:.2f} (1.00 + 12.87 + 3.00 + 4.00)")


# ── the server must actually do this ───────────────────────────────────────────────────────────
src = open(SERVER).read()

check('SPONSOR_ORDER_PREFIX = "hazync-sponsorship-"' in src,
      "the order prefix has ONE definition")
# ⛔ Both readers must use the constant. A literal in either place drifts from the other, and the
# only symptom is sponsorships quietly reappearing as donations months later.
check(len(re.findall(r"SPONSOR_ORDER_PREFIX", src)) >= 3,
      "and both the invoice writer and the donations filter use it, not a literal")
check(not re.search(r'orderId["\']?\s*:\s*f?["\']hazync-sponsorship-', src),
      "⛔ the invoice writer does not hard-code the prefix")
check("startswith(SPONSOR_ORDER_PREFIX)" in src,
      "and the donations filter tests it by prefix")
check('paidAmount' in src and 'i.get("paidAmount")' in src,
      "the record reads paidAmount")

EXPECTED_CONTROL = {
    "the sponsorship invoice is NOT in the donations record",
    "every real donation IS counted",
    "an overpaid invoice records what ARRIVED",
    "the total is",
}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL:
        print("CONTROL OK — without the filter a sponsorship is counted as a donation, and without")
        print("paidAmount an overpayment is understated:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {sorted(EXPECTED_CONTROL)}, got {sorted(hit)}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("sponsorships stay out of the donations record, and donations record what arrived")

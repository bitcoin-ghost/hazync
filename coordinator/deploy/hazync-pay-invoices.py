#!/usr/bin/env python3
"""Read BTCPay's invoice list on stdin, print one line per SETTLED invoice.

    ts|amount|currency|id

⛔ WHY THIS IS A FILE AND NOT A PYTHON ONE-LINER INSIDE THE SHELL SCRIPT. It was a one-liner, inside
an unquoted heredoc, and the `\\"` escaping mangled on the way through:

    SyntaxError: unexpected character after line continuation character

So the parser never ran once. Its stderr went to /dev/null, so nothing said so, and the empty output
was read by the caller as "0 new settled invoices" -- indistinguishable from "no donations have
arrived". A real £1 Lightning payment settled and nobody was told.

⛔ AND THE EXIT STATUS IS THE POINT. This exits 2 if the input is not a list of invoices, so the
caller can tell "nothing settled" (exit 0, no lines) from "I could not read this" (exit 2). Those
must never look the same again: one is the normal state, the other is the failure this whole
watcher exists to avoid.
"""
import json
import sys

SETTLED = ("settled", "complete", "confirmed")


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        print("no input", file=sys.stderr)
        return 2
    try:
        inv = json.loads(raw)
    except ValueError as exc:
        print(f"not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(inv, list):
        # ⚠ BTCPay answers an auth failure with a JSON OBJECT, not a list. Treating that as "no
        # invoices" is how a revoked API key would read as a quiet month.
        print(f"expected a list of invoices, got {type(inv).__name__}: {str(inv)[:120]}",
              file=sys.stderr)
        return 2
    for i in inv:
        if not isinstance(i, dict):
            continue
        if str(i.get("status", "")).lower() not in SETTLED:
            continue
        try:
            ts = int(i.get("createdTime") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        # ⛔ WHAT ARRIVED, NOT WHAT WAS ASKED FOR. `amount` is the invoice's price; `paidAmount` is what
        # was actually received. Measured 2026-09-26: an invoice priced at £5.00 was paid with £12.87
        # because the donor's wallet had a minimum, and the ledger recorded "5.0". A record that
        # credits a donor with less than they gave is worse than no record, and the alert understates
        # the money too. Fall back to `amount` only when paidAmount is missing or unusable.
        try:
            amt = float(i.get("paidAmount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            try:
                amt = float(i.get("amount") or 0)
            except (TypeError, ValueError):
                continue
        if amt <= 0:
            continue
        amt = f"{amt:.2f}"
        cur = i.get("currency") or ""
        iid = i.get("id") or ""
        # ⚠ Pipe-separated because the caller reads it with `IFS='|' read`. Strip any pipe out of
        # the fields rather than letting one shift every column to its right.
        fields = [str(x).replace("|", "/") for x in (ts, amt, cur, iid)]
        print("|".join(fields))
    return 0


if __name__ == "__main__":
    sys.exit(main())

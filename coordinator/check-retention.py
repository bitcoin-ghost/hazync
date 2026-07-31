#!/usr/bin/env python3
"""G1 gate: every height the board calls proven must have its own retained receipt.

G1 is "a zk proof per block" — not "a proof of a range containing that block". A sceptic must be able
to be handed one block and check it alone. That property is not established by the proving code; it is
established by RETENTION, and retention is the part with no test.

It has already failed once. `coordinator/hazync` proves `range_{h}.bin` per height and discards the
leaves after folding, so any multi-block unit produced per-block receipts and threw them away. 800
blocks (38,500..39,299) were proven that way and have no individual receipt. Free-running proving
(#37) makes one block the unit, which prevents a recurrence but does not DETECT one — opportunistic
folding could reintroduce it silently, and nothing would notice until someone asked for a block's
proof and it was not there.

Run on the coordinator:

    ./coordinator/check-retention.py                 # exits 1 if any proven height lacks a receipt
    ./coordinator/check-retention.py --allow 38500-39299   # accept a known, recorded hole

Environment (matching the coordinator unit):
    COORD_DB      default /root/coordinator.db
    COORD_PROOFS  default /root/hazync-proofs
"""
import os
import sqlite3
import sys

DB = os.environ.get("COORD_DB", "/root/coordinator.db")
PROOFS = os.environ.get("COORD_PROOFS", "/root/hazync-proofs")


def parse_allow(args):
    """Ranges the operator has explicitly accepted, so a KNOWN hole does not mask a NEW one."""
    allowed = set()
    for i, a in enumerate(args):
        if a == "--allow" and i + 1 < len(args):
            for part in args[i + 1].split(","):
                if "-" in part:
                    lo, hi = part.split("-", 1)
                    allowed.update(range(int(lo), int(hi) + 1))
                else:
                    allowed.add(int(part))
    return allowed


def runs(sorted_heights):
    """Collapse a sorted height list into contiguous (lo, hi) runs — 800 individual numbers is noise."""
    out = []
    if not sorted_heights:
        return out
    lo = prev = sorted_heights[0]
    for h in sorted_heights[1:]:
        if h != prev + 1:
            out.append((lo, prev))
            lo = h
        prev = h
    out.append((lo, prev))
    return out


def main():
    allowed = parse_allow(sys.argv[1:])

    if not os.path.isdir(PROOFS):
        sys.exit(f"proof directory {PROOFS} does not exist — refusing to report a clean run")
    have = {
        int(f[6:-4])
        for f in os.listdir(PROOFS)
        if f.startswith("proof_") and f.endswith(".bin") and f[6:-4].isdigit()
    }

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute("SELECT lo, hi FROM ranges WHERE status = 'verified'").fetchall()
    proven = set()
    for lo, hi in rows:
        proven.update(range(lo, hi + 1))

    # An empty ledger has two very different causes and they must not be conflated.
    #
    #   nothing verified AND no receipts  -> a fresh or re-baselined board. Legitimate: there is
    #                                        genuinely nothing proven yet, and the two halves agree.
    #   nothing verified BUT receipts on disk -> the ledger and the proof store disagree about
    #                                        whether anything happened. That is the failure this
    #                                        check exists to catch, pointing the other way.
    #
    # The earlier version exited non-zero on any empty ledger, which meant a correctly re-baselined
    # board failed its own G1 gate every night until someone proved a block — an alarm that is wrong
    # by design gets muted, and then it is not an alarm.
    if not proven:
        if have:
            print(f"  heights marked verified : 0")
            print(f"  per-block receipts held : {len(have):,}")
            print()
            print("LEDGER/STORE MISMATCH: no verified ranges, but receipts exist on disk.")
            print("The ledger may have been reset without clearing the proof store, or restored stale.")
            return 1
        print("  board is empty — nothing verified and no receipts. Fresh or re-baselined board.")
        print("  G1 holds vacuously; this becomes a real check as soon as the first block is proven.")
        return 0

    missing = sorted(proven - have)
    unexpected = [h for h in missing if h not in allowed]

    print(f"  heights marked verified : {len(proven):,}")
    print(f"  per-block receipts held : {len(have):,}")
    print(f"  missing                 : {len(missing):,}")

    if missing:
        for lo, hi in runs(missing):
            tag = "accepted" if all(h in allowed for h in range(lo, hi + 1)) else "UNEXPECTED"
            print(f"    {lo}..{hi}  ({hi - lo + 1} blocks)  {tag}")

    if unexpected:
        print()
        print(f"G1 VIOLATION: {len(unexpected):,} proven heights have no retained per-block receipt.")
        print("The board claims these blocks are proven but cannot hand anyone the proof for one.")
        print("Check the fold path is not discarding per-block leaves after folding.")
        return 1

    print()
    print("G1 holds: every proven height has its own retained receipt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

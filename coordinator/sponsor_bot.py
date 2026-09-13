#!/usr/bin/env python3
"""Sponsorship bot -- NOT BUILT YET. Dry run only.

What it will do once payments exist (docs/SPONSORSHIP.md): take PAID sponsorships in the order they were
paid, start RunPod pods for their blocks, run the normal worker (claim, prove, submit) under the bot's own
key, and move each sponsorship to `proving` and then `proven`.

Today it only reads the queue and prints what it would do. `--live` refuses: there is no RunPod
integration, no spending limit and no payment check yet, and a program that can spend money must not
exist before those do.

  python3 sponsor_bot.py            # print the plan for the current queue
  python3 sponsor_bot.py --live     # refuses
"""
import os
import sqlite3
import sys

DB = os.environ.get("COORD_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator.db"))


def queue(db_path=DB):
    """Paid sponsorships, oldest payment first. Read-only; an older database without the table is empty.

    Paid means paid AT LEAST THE MINIMUM, the same rule that decides whether a name is public
    (server.SPONSOR_PUBLIC_SQL): an `underpaid` row, or a `paid` row below its minimum, is never proven
    on the sponsorship budget."""
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(
            "SELECT id,lo,hi,name,status,paid_at,paid_sats,min_sats FROM sponsorships WHERE status='paid'"
            " AND paid_sats IS NOT NULL AND min_sats IS NOT NULL AND paid_sats >= min_sats"
            " ORDER BY paid_at ASC, id ASC")]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def plan(rows):
    return [f"sponsorship #{r['id']}: would prove blocks {r['lo']} to {r['hi']} "
            f"({r['hi'] - r['lo'] + 1} blocks) for {r['name']}" for r in rows]


def main(argv):
    if "--live" in argv:
        raise SystemExit("sponsor_bot: live mode is not built. There is no RunPod integration, "
                         "spending limit or payment check yet.")
    for line in plan(queue()) or ["no paid sponsorships in the queue"]:
        print(line)


if __name__ == "__main__":
    main(sys.argv[1:])

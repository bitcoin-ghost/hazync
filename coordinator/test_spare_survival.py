#!/usr/bin/env python3
"""Spares outlive the gates, and a slow download is not a dead one (hazync#479).

⛔ WHY THIS EXISTS — a run died on it tonight, 2026-09-22:

    20:31:30  releasing 3 unused spare(s): ['hz-smoke-4','hz-smoke-5','hz-smoke-6']
    20:43:41  hz-smoke-1: INCOMPLETE 348,332,032 / 410,441,528 bytes
              elapsed 13.0 min, ~$0.483 on 3 cards, zero blocks proved

Two independent defects put those lines next to each other:

  1. the spares were released 23 s after the SSH gate — BEFORE the three gates that actually find a
     bad card — so when one failed there was nothing to swap in, and three healthy pods had already
     been terminated
  2. `fetch_binary` gave up on a flat 600 s clock. That card was pulling at 0.57 MB/s, so 410 MB
     needed 724 s; it was 116 s short. The guard's own docstring cites ~1 MB/s as the rate it exists
     for, and 410 MB at 1 MB/s only just fits in 600 s — the budget never covered its own worst case

  python3 test_spare_survival.py            # must PASS
  python3 test_spare_survival.py --control  # the flat clock is restored; MUST FAIL
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_smoke                                                             # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


WANT = 410_441_528


class Card:
    def __init__(self, cid):
        self.cid = cid


class FakeSSH:
    """A card whose download advances `per_poll` bytes each time it is asked, then stops at `cap`."""

    def __init__(self, per_poll, cap=None):
        self.per_poll, self.cap = per_poll, (WANT if cap is None else cap)
        self.n, self.polls = 0, 0

    def run(self, card, body, env=None, timeout=None):
        if "stat -c%s" in body:
            self.polls += 1
            out = str(self.n)
            self.n = min(self.cap, self.n + self.per_poll)
            return out + "\n"
        return "OK\n"


# ── 1. ⛔ TONIGHT'S CARD. 0.57 MB/s for 410 MB: slow, finishes, must NOT be abandoned ─────────────
# 15 s per poll at 0.57 MB/s = ~8.6 MB a poll, so it needs ~48 polls — well past the old 40.
slow = FakeSSH(per_poll=8_600_000)
if CONTROL:
    # ⛔ THE CONTROL IS THE ORIGINAL RULE: a flat 40 polls, whatever the bytes are doing.
    def flat(ssh, card, want, **kw):
        got = 0
        for _ in range(40):
            got = int(ssh.run(card, "stat -c%s x") or 0)
            if got == want:
                return True, got
        return False, got
    fetch = flat
else:
    fetch = tip_smoke.fetch_binary

ok, got = fetch(slow, Card("hz-smoke-1"), WANT, wait_s=0)
check(ok, f"a card pulling at 0.57 MB/s is allowed to finish ({got:,}/{WANT:,} after {slow.polls} polls)")

# ── 2. and a card that has genuinely STOPPED is still abandoned, promptly ────────────────────────
dead = FakeSSH(per_poll=8_600_000, cap=100_000_000)          # stalls at 100 MB and never moves
ok, got = tip_smoke.fetch_binary(dead, Card("hz-smoke-2"), WANT, wait_s=0, stall_polls=8)
check(not ok, f"a card whose byte count stops moving IS abandoned ({got:,} bytes)")
check(dead.polls < 40,
      f"and abandoned on the STALL rather than riding out the old clock ({dead.polls} polls, not 40)")

# ── 3. a card that never starts at all is abandoned too ──────────────────────────────────────────
never = FakeSSH(per_poll=0)
ok, got = tip_smoke.fetch_binary(never, Card("hz-smoke-3"), WANT, wait_s=0, stall_polls=8)
check(not ok and got == 0, f"a card that never starts downloading is abandoned (got {got})")


# ── 4. the structural fix: a dropped card releases, and the survivors carry on ───────────────────
released, recorded = [], []


def release(p):
    released.append(p["name"])


def record(kept):
    recorded.append([x["name"] for x in kept])


order = [Card(f"hz-smoke-{i}") for i in range(1, 7)]
created = [{"name": f"hz-smoke-{i}", "id": f"id{i}"} for i in range(1, 7)]

order2, created2 = tip_smoke._drop_cards(order, created, ["hz-smoke-1"], "the prover never finished",
                                         release=release, record=record)
check([c.cid for c in order2] == [f"hz-smoke-{i}" for i in range(2, 7)],
      f"the failing card leaves the run and five carry on ({[c.cid for c in order2]})")
check(released == ["hz-smoke-1"], f"and it is actually released, not just forgotten ({released})")
check(recorded and [x for x in recorded[-1]] == [f"hz-smoke-{i}" for i in range(2, 7)],
      "and rented.json is rewritten, so a driver that dies hard still knows what it owes")

# ⚠ Nothing to drop must not write anything: a no-op that rewrites rented.json is a chance to lose it.
released.clear(); recorded.clear()
o3, c3 = tip_smoke._drop_cards(order2, created2, [], "nothing", release=release, record=record)
check(released == [] and recorded == [] and len(o3) == 5,
      "dropping nothing releases nothing and rewrites nothing")

EXPECTED_CONTROL = {"is allowed to finish"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — with the flat 600 s clock restored, tonight's card is abandoned again:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected only the slow-card assertion to fail; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("slow is not dead, and a bad card costs a card rather than the run")

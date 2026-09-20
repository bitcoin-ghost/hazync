#!/usr/bin/env python3
"""Tests for tip_lifecycle.py — which cards the tip rig may use, and which it lets go (Phase 5 ⑧).

TWO FAILURES THIS EXISTS TO PREVENT, both of which cost more than the card did.

A card that cannot produce a verifiable receipt is WORSE than no card: it takes a chunk, returns
something, and the aggregate fails at the end — after every other card's work has been spent. So
qualification is a gate, and its centre is the METHOD_ID.

And the tip rig sharing the sponsor bot's RunPod key would let a runaway tip fleet spend the
sponsorship budget silently, with neither side's accounting true. The roadmap says separate keys and
separate budgets; this asserts it rather than trusting it.

  python3 test_tip_lifecycle.py            # assertions; exit 0 on success
  python3 test_tip_lifecycle.py --control  # the METHOD_ID gate is removed; MUST fail
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_lifecycle as tl  # noqa: E402

if CONTROL:
    # THE GATE REMOVED: accept any METHOD_ID. This is the fleet that spends 27 cards and fails at the
    # aggregate, which is precisely what the gate is for.
    _real_qualify = tl.qualify

    def _blind(report):
        r = dict(report)
        r["method_id"] = tl.CANONICAL_METHOD_ID
        return _real_qualify(r)

    tl.qualify = _blind

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


GOOD = {"reachable": True, "method_id": tl.CANONICAL_METHOD_ID, "smoke_ok": True}

# ── 1. the METHOD_ID gate ──────────────────────────────────────────────────────────────────────────
ok, why = tl.qualify(GOOD)
check(ok, f"a canonical, reachable, smoke-tested card qualifies ({why})")

ok, why = tl.qualify({**GOOD, "method_id": "deadbeef" * 8})
check(not ok and "canonical" in why, f"a wrong METHOD_ID is refused ({why})")

# ⚠ The bridge guest is a DIFFERENT image on purpose. Refused, but the reason must say so, or someone
# will spend an evening hunting a bug that is not there.
ok, why = tl.qualify({**GOOD, "method_id": tl.BRIDGE_METHOD_ID + "ab" * 30})
check(not ok, "the bridge guest is refused for proving")
check("by design" in why, f"and the reason says it is deliberate, not a fault ({why})")

ok, why = tl.qualify({**GOOD, "method_id": ""})
check(not ok and "no METHOD_ID" in why, f"a card that reports no METHOD_ID is refused ({why})")

# ── 2. the other gates, in order ───────────────────────────────────────────────────────────────────
ok, why = tl.qualify({**GOOD, "reachable": False})
check(not ok and why == "unreachable", f"an unreachable card is refused first ({why})")

ok, why = tl.qualify({**GOOD, "smoke_ok": False})
check(not ok and "smoke" in why,
      f"a card that answers but cannot prove is refused ({why})")

ok, why = tl.qualify({**GOOD, "binary_sha": "aaa", "expect_binary_sha": "bbb"})
check(not ok and "signed release" in why, f"a binary that is not the signed release is refused ({why})")

# ── 3. ranking ─────────────────────────────────────────────────────────────────────────────────────
cards = [
    {"id": "slow",  "qualified": True,  "rate": 1.0, "price": 0.20},
    {"id": "fast",  "qualified": True,  "rate": 5.0, "price": 0.50},
    {"id": "mid",   "qualified": True,  "rate": 3.0, "price": 0.34},
    {"id": "bad",   "qualified": False, "rate": 9.9, "price": 0.10},
    {"id": "new",   "qualified": True,  "rate": None, "price": 0.30},
]
r = tl.rank(cards)
check([c["id"] for c in r][:3] == ["fast", "mid", "slow"], f"fastest first ({[c['id'] for c in r]})")
check("bad" not in [c["id"] for c in r], "an unqualified card is never ranked, however fast it claims to be")
check(r[-1]["id"] == "new", "a card with no measured rate sorts last rather than being discarded")

tie = [{"id": "cheap", "qualified": True, "rate": 3.0, "price": 0.20},
       {"id": "dear",  "qualified": True, "rate": 3.0, "price": 0.90}]
check([c["id"] for c in tl.rank(tie)] == ["cheap", "dear"], "price breaks a tie on rate")

# ── 4. keep and release ────────────────────────────────────────────────────────────────────────────
d = tl.keep_and_release(cards, want=2)
check([c["id"] for c in d["keep"]] == ["fast", "mid"], f"the fastest `want` are kept ({d['keep']})")
check("bad" in [c["id"] for c in d["release"]], "an unqualified card is always released")

# ⛔ The slow tail is judged against the MEDIAN. A mean would be dragged by one very slow card and
# condemn healthy ones with it.
tail = [{"id": "a", "qualified": True, "rate": 10.0, "price": 0.3},
        {"id": "b", "qualified": True, "rate": 9.0, "price": 0.3},
        {"id": "c", "qualified": True, "rate": 8.0, "price": 0.3},
        {"id": "crawler", "qualified": True, "rate": 0.5, "price": 0.3}]
d = tl.keep_and_release(tail, want=4)
check("crawler" in [c["id"] for c in d["release"]],
      "a card far below the median is released even when there is room for it")
check({"a", "b", "c"} <= {c["id"] for c in d["keep"]},
      "the healthy cards are kept — one crawler does not condemn the fleet")

# ⛔ An unmeasured card is never in the slow tail.
unm = [{"id": "a", "qualified": True, "rate": 10.0, "price": 0.3},
       {"id": "b", "qualified": True, "rate": 9.0, "price": 0.3},
       {"id": "fresh", "qualified": True, "rate": None, "price": 0.3}]
d = tl.keep_and_release(unm, want=3)
check("fresh" in [c["id"] for c in d["keep"]],
      "a card with no rate yet is kept long enough to be measured, not churned")

# ── 5. the key must never be the sponsor bot's ─────────────────────────────────────────────────────
saved = dict(os.environ)
try:
    os.environ.pop("TIP_RUNPOD_KEY_FILE", None)
    os.environ["SPONSOR_BOT_HOME"] = "/srv/sponsor"
    try:
        tl.key_path()
        check(False, "an absent TIP key must refuse, not fall back")
    except SystemExit as e:
        check("must not be reused" in str(e),
              "with no TIP key set it refuses rather than borrowing the sponsor bot's")

    os.environ["TIP_RUNPOD_KEY_FILE"] = "/srv/sponsor/runpod.key"
    try:
        tl.key_path()
        check(False, "a key inside SPONSOR_BOT_HOME must be refused")
    except SystemExit as e:
        check("must not share" in str(e),
              "a TIP key pointing inside SPONSOR_BOT_HOME is refused — separate budgets")

    os.environ["TIP_RUNPOD_KEY_FILE"] = "/srv/tip/runpod.key"
    check(tl.key_path() == "/srv/tip/runpod.key", "a key of its own is accepted")
finally:
    os.environ.clear()
    os.environ.update(saved)

# ── 6. spend ───────────────────────────────────────────────────────────────────────────────────────
# 27 cards x $0.34/hr x 544.0 s. run4's summary.json records cost_usd 1.387 -- the same figure, which
# is the point: the accounting here reproduces the historical record rather than approximating it.
check(tl.spend_so_far([{"price": 0.34}] * 27, 544.0) == 1.387,
      f"run 4's cost is reproduced exactly: $"
      f"{tl.spend_so_far([{'price': 0.34}] * 27, 544.0)} vs $1.387 recorded")

EXPECTED_CONTROL_FAILURES = {
    "a wrong METHOD_ID is refused (ok)",
    "the bridge guest is refused for proving",
}

print()
if CONTROL:
    got = {f.split(" (")[0] for f in fails}
    want = {f.split(" (")[0] for f in EXPECTED_CONTROL_FAILURES}
    if want <= got:
        print(f"CONTROL OK — the METHOD_ID gate was removed and the {len(want)} assertion(s) that "
              "depend on it failed, as they must:")
        for f in sorted(fails):
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the METHOD_ID gate went undetected.")
    for f in sorted(want - got):
        print(f"  should have failed and did not: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails))
    sys.exit(1)
print("all good")

#!/usr/bin/env python3
"""A reachability failure is reported grouped by network block, before the clock (hazync#508).

⛔ WHY THIS EXISTS — measured 2026-09-24 01:54. A tip trial lost 7 of 17 workers at the
card-to-card reachability gate and said only:

    reachability: 7 of 17 card(s) cannot reach the aggregate: hz-smoke-12, hz-smoke-18, ...

Seven unlucky pods, apparently. Grouping the SAME result by network block is a different fact:

    194.68.245.x   reached 10, failed 0
    69.30.85.x     reached  0, failed 4    <- the aggregate's own block
    63.141.33.x    reached  0, failed 3

Every block is all-reached or all-failed. That is the network, not the pods — and it took a
log-grep after the run to see, from data the gate already had in its hand.

⛔ IT REPORTS, IT DOES NOT DIAGNOSE, and the distinction matters here more than usual: the shape
says the intuitive fix is backwards. "Rent within one data centre so the cards can talk" is what
one reaches for, and on this run the block that failed completely was the AGGREGATE'S OWN.

  python3 test_reachability_report.py            # must PASS
  python3 test_reachability_report.py --control  # grouping removed; MUST show as the gap
"""
import os
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tip_lifecycle as tl                                                   # noqa: E402

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# The run as it actually happened: hz-smoke-1 was the aggregate, at 69.30.85.141.
HOSTS = {
    "hz-smoke-1": "69.30.85.141",  "hz-smoke-2": "194.68.245.232",
    "hz-smoke-3": "194.68.245.113", "hz-smoke-4": "63.141.33.129",
    "hz-smoke-5": "194.68.245.125", "hz-smoke-6": "69.30.85.16",
    "hz-smoke-7": "69.30.85.50",    "hz-smoke-8": "69.30.85.249",
    "hz-smoke-9": "63.141.33.109",  "hz-smoke-10": "194.68.245.30",
    "hz-smoke-11": "194.68.245.40", "hz-smoke-12": "69.30.85.59",
    "hz-smoke-13": "194.68.245.46", "hz-smoke-14": "194.68.245.46",
    "hz-smoke-15": "194.68.245.51", "hz-smoke-16": "194.68.245.208",
    "hz-smoke-17": "194.68.245.208", "hz-smoke-18": "63.141.33.115",
}
DEAD = {"hz-smoke-4", "hz-smoke-6", "hz-smoke-7", "hz-smoke-8",
        "hz-smoke-9", "hz-smoke-12", "hz-smoke-18"}
RESULT = {c: (c not in DEAD) for c in HOSTS if c != "hz-smoke-1"}   # the agg is not a worker

if CONTROL:
    # ⛔ THE OLD BEHAVIOUR: a flat count, no grouping. Everything lands in one bucket.
    tl.reachability_by_block = lambda results, hosts: {
        "?": {"ok": sum(1 for v in results.values() if v),
              "bad": sum(1 for v in results.values() if not v),
              "cards": sorted(results)}}

by = tl.reachability_by_block(RESULT, HOSTS)

check(len(by) == 3, f"the fleet resolves into 3 network blocks (got {len(by)}: {sorted(by)})")
check(by.get("194.68.245", {}).get("ok") == 10 and by.get("194.68.245", {}).get("bad") == 0,
      f"194.68.245 reached 10, failed 0 (got {by.get('194.68.245')})")
check(by.get("69.30.85", {}).get("ok") == 0 and by.get("69.30.85", {}).get("bad") == 4,
      f"69.30.85 — the aggregate's own block — reached 0, failed 4 (got {by.get('69.30.85')})")
check(by.get("63.141.33", {}).get("ok") == 0 and by.get("63.141.33", {}).get("bad") == 3,
      f"63.141.33 reached 0, failed 3 (got {by.get('63.141.33')})")

check(tl.reachability_is_structural(by),
      "⛔ every block is all-reached or all-failed — reported as the NETWORK, not seven bad pods")

# ── the shape must NOT be claimed when it is not there ──────────────────────────────────────────
# ⚠ A detector that says "structural" for every failure is a detector that says nothing. Ordinary
# bad luck — losses scattered inside a block — must read as ordinary bad luck.
mixed = tl.reachability_by_block(
    {"a": True, "b": False, "c": True, "d": False},
    {"a": "10.0.0.1", "b": "10.0.0.2", "c": "10.0.0.3", "d": "10.0.0.4"})
check(not tl.reachability_is_structural(mixed),
      "scattered losses inside one block are NOT reported as structural")
one_block_all_bad = tl.reachability_by_block(
    {"a": False, "b": False}, {"a": "10.0.0.1", "b": "10.0.0.2"})
check(not tl.reachability_is_structural(one_block_all_bad),
      "and a single block with nothing to compare against is not either")

# ── ⛔ AND THE DRIVER MUST ACTUALLY CALL IT, with a host source that exists ─────────────────────
_src = open(os.path.join(HERE, "tip_smoke.py")).read()
check("tip_lifecycle.reachability_by_block(" in _src,
      "the driver groups the result rather than only counting it")
check('hosts = {c.cid: getattr(c, "ip", "") for c in order}' in _src,
      "⛔ and reads Card.ip — portmap is keyed BY PORT NUMBER, so .get('host') on it is always ''")
check("reachability_is_structural" in _src,
      "and says so out loud when the shape is structural")

EXPECTED_CONTROL = {"the fleet resolves into 3 network blocks",
                    "194.68.245 reached 10",
                    "69.30.85 — the aggregate's own block",
                    "63.141.33 reached 0",
                    "⛔ every block is all-reached or all-failed"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit == EXPECTED_CONTROL and len(fails) == len(EXPECTED_CONTROL):
        print("CONTROL OK — without grouping, 7 of 17 is seven unlucky pods and the pattern is "
              "invisible:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print(f"CONTROL FAILED — expected {len(EXPECTED_CONTROL)}; got {len(fails)}:")
    for f in fails:
        print(f"  {f}")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("a reachability failure says which NETWORKS failed, while the fleet can still be re-planned")

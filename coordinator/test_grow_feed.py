#!/usr/bin/env python3
"""A grown fleet reaches the dashboard: every admitted card has a price and a GPU name.

⛔ WHAT THIS EXISTS FOR. `fleet` is DERIVED from `created`, once, before the session loop:

    fleet = [{"id": p["name"], "price": p["price"], "gpu": p["gpu_type"], "pod_id": p["id"]}
             for p in created]

`--grow-to` appended the recruit to `created` and to `order`, but not to `fleet`. feed_records then
had a card it knew no price or GPU name for and refused — rightly, because both ways of papering
over it are wrong: `price=0.0` under-reports the spend on a run that has NO budget cap by decision,
and dropping the card hides a pod that is proving and being billed.

Measured live 2026-09-28, RTX PRO 4500:

    10:45:43   ⚠ could not rewrite the feed after growing: card hz-grow-1 is in the run but not in
               the fleet list, so it has no price and no GPU name
    10:45:43   fleet grew by 1 to 3 cards (hz-grow-1) — they join from the next block
    10:47:08   block 131769 verified in 76.7s on 3 cards

The card was proving and being billed while the dashboard still showed two. This is the same shape
as the frozen `assignment` that `--grow-to` had to unfreeze to work at all: a structure built once
from a fleet that can now change.

    python3 test_grow_feed.py             # a grown fleet produces a complete feed
    python3 test_grow_feed.py --control   # fleet left stale — feed_records must refuse
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_dashboard  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


class Card:
    def __init__(self, cid, ip="1.2.3.4", port=22):
        self.cid, self.ip, self.port = cid, ip, port


def fleet_entry(pod):
    return {"id": pod["name"], "price": pod["price"], "gpu": pod["gpu_type"], "pod_id": pod["id"]}


# the fleet as it starts: two gated cards
pods = [{"name": "hz-smoke-1", "price": 0.72, "gpu_type": "RTX PRO 4500", "id": "a"},
        {"name": "hz-smoke-2", "price": 0.72, "gpu_type": "RTX PRO 4500", "id": "b"}]
order = [Card(p["name"]) for p in pods]
fleet = [fleet_entry(p) for p in pods]

joined = tip_dashboard.feed_records(order, fleet)
check(len(joined["records"]) == 2, f"before growing: 2 records (got {len(joined['records'])})")

# ── the recruit is admitted ──────────────────────────────────────────────────────────────────────
recruit = {"name": "hz-grow-1", "price": 0.72, "gpu_type": "RTX PRO 4500", "id": "c"}
order.append(Card(recruit["name"]))
if not CONTROL:
    fleet.append(fleet_entry(recruit))      # the fix

try:
    joined = tip_dashboard.feed_records(order, fleet)
    if CONTROL:
        check(False, "control did NOT refuse a card with no price — it is testing nothing")
    else:
        check(len(joined["records"]) == 3,
              f"after growing: 3 records (got {len(joined['records'])})")
        rec = {r.get("id") or r.get("cid"): r for r in joined["records"]}
        got = rec.get("hz-grow-1")
        check(got is not None, "the recruit appears in the feed")
        if got:
            check(got.get("price") == 0.72, f"the recruit carries its price (got {got.get('price')})")
            check(bool(got.get("gpu")), f"the recruit carries its GPU name (got {got.get('gpu')!r})")
        total = sum(float(r.get("price") or 0) for r in joined["records"])
        check(abs(total - 2.16) < 1e-9,
              f"the fleet rate counts all three cards: ${total:.2f}/hr (2 x 0.72 + 0.72)")
except Exception as exc:                    # noqa: BLE001
    if CONTROL:
        check("hz-grow-1" in str(exc) and "price" in str(exc),
              "control reproduces it: feed_records refuses the card it has no price for")
    else:
        check(False, f"the fix still raised {type(exc).__name__}: {exc}")


# ── the session's own record of its fleet ────────────────────────────────────────────────────────
# ⛔ resume_verdict compares state["fleet"] against the pods that are alive. A recruit missing from it
# is `extra` on every resume — and if the ORIGINAL cards die while the recruits live, recorded & live
# is empty and the session REFUSES to resume the fleet it grew itself. tip_economics also sizes a run
# by len(state["fleet"]).
import tip_session  # noqa: E402

st = tip_session.new_state(started_at=0.0, duration_s=3600.0, fleet_ids=["a", "b"])
if not CONTROL:
    st["fleet"] = sorted(set(st["fleet"]) | {"c"})     # the fix: record the recruit

v = tip_session.resume_verdict(st, live_pod_ids=["a", "b", "c"], now=1.0)
if CONTROL:
    check(v["extra"] == ["c"], f"control: the recruit is 'extra' on resume (got {v['extra']})")
else:
    check(v["extra"] == [], f"a recorded recruit is not 'extra' on resume (got {v['extra']})")

v2 = tip_session.resume_verdict(st, live_pod_ids=["c"], now=1.0)   # originals died, recruit lives
if CONTROL:
    check(v2["ok"] is False,
          "control reproduces it: with only the recruit alive the session refuses to resume")
else:
    check(v2["ok"] is True,
          f"with only the recruit alive the session still resumes (ok={v2['ok']}, why={v2.get('why')})")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ") — feed + session record")

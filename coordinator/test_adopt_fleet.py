#!/usr/bin/env python3
"""--adopt reuses a live fleet, and refuses to pretend a dead record is a pod (hazync#506 supply).

⛔ WHY ADOPTING EXISTS. Capacity, not money, caps a big fleet. Measured 2026-09-27, 45 requested of
each type in SECURE:

    RTX 4090                      1 granted
    RTX PRO 4500 SE               4 granted
    RTX PRO 6000                 38 granted

and the live run then got exactly 30 before the 31st was refused. A fleet that size cannot be
reassembled on demand, so a one-hour session that releases it in its `finally` can destroy something
worth more than the pods cost to hold.

⛔ WHY THE LIVE CHECK IS THE WHOLE TEST. rented.json records what was RENTED, not what still EXISTS:
the previous driver may have released them, RunPod may have reclaimed one, the file may be days old.
A blind adopt hands the run a list of ghosts — it then waits out its full ssh timeout on pods that
are not there and fails at the gate with the fleet's worth of time spent. Dropping the dead ones
must happen HERE, before anything depends on them, and must SAY so rather than quietly shrinking.

    python3 test_adopt_fleet.py              # the real loader
    python3 test_adopt_fleet.py --control    # a blind loader that skips the live check — must FAIL
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tip_smoke  # noqa: E402

CONTROL = "--control" in sys.argv

fails = 0


def check(ok, what):
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails += 1


class FakeAPI:
    """Only the one call adopt_fleet makes."""

    def __init__(self, live_ids):
        self._live = [{"id": i, "name": f"hz-smoke-{n}"} for n, i in enumerate(live_ids, 1)]

    def pods(self):
        return list(self._live)


def blind_adopt(api, record_path, expect_names=None):
    """The control: trust the file, never ask the account. This is the bug."""
    with open(record_path) as fh:
        return json.load(fh), []


load = blind_adopt if CONTROL else tip_smoke.adopt_fleet

RECORD = [
    {"id": "aaa", "name": "hz-smoke-1", "gpu_type": "NVIDIA RTX PRO 6000", "price": 2.09, "dc": "EUR-IS-2"},
    {"id": "bbb", "name": "hz-smoke-2", "gpu_type": "NVIDIA RTX PRO 6000", "price": 2.09, "dc": "US-PA-1"},
    {"id": "ccc", "name": "hz-smoke-3", "gpu_type": "NVIDIA RTX PRO 6000", "price": 2.09, "dc": "US-NE-1"},
]

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "rented.json")
    with open(path, "w") as fh:
        json.dump(RECORD, fh)

    # 1. every pod still alive -> all three adopted, records intact
    adopted, missing = load(FakeAPI(["aaa", "bbb", "ccc"]), path)
    check(len(adopted) == 3, f"all-alive: adopts 3 (got {len(adopted)})")
    check(not missing, f"all-alive: reports nothing missing (got {missing})")
    check(all(p.get("gpu_type") and p.get("price") for p in adopted),
          "all-alive: keeps gpu_type and price from the record (the listing does not carry them)")

    # 2. ⛔ THE ONE THAT MATTERS. One pod is gone from the account. It must be DROPPED and NAMED.
    adopted, missing = load(FakeAPI(["aaa", "ccc"]), path)
    if CONTROL:
        check(len(adopted) == 3 and not missing,
              "control reproduces the bug: a released pod is still adopted as if it were alive")
    else:
        check(len(adopted) == 2, f"one released: adopts only the 2 that exist (got {len(adopted)})")
        check(missing == ["hz-smoke-2"], f"one released: names the missing pod (got {missing})")

    # 3. the whole fleet gone -> nothing adopted, all named
    adopted, missing = load(FakeAPI([]), path)
    if CONTROL:
        check(len(adopted) == 3, "control: adopts a fleet that no longer exists at all")
    else:
        check(adopted == [], f"fleet gone: adopts nothing (got {len(adopted)})")
        check(len(missing) == 3, f"fleet gone: names all three (got {missing})")

    # 4. an unreadable or empty record is a configuration error, not an empty fleet
    if not CONTROL:
        empty = os.path.join(td, "empty.json")
        with open(empty, "w") as fh:
            json.dump([], fh)
        try:
            tip_smoke.adopt_fleet(FakeAPI(["aaa"]), empty)
            check(False, "empty record: raises rather than returning an empty fleet")
        except SystemExit:
            check(True, "empty record: raises rather than returning an empty fleet")
        try:
            tip_smoke.adopt_fleet(FakeAPI(["aaa"]), os.path.join(td, "nope.json"))
            check(False, "missing record: raises")
        except SystemExit:
            check(True, "missing record: raises")

    # 5. the live name wins, so a rename on RunPod's side cannot desync the feed
    if not CONTROL:
        api = FakeAPI(["aaa"])
        api._live[0]["name"] = "renamed-on-runpod"
        adopted, _ = tip_smoke.adopt_fleet(api, path)
        check(adopted and adopted[0]["name"] == "renamed-on-runpod",
              f"takes the name from the live pod (got {adopted[0]['name'] if adopted else None})")

print()
if fails:
    print(f"FAIL: {fails} assertion(s)")
    sys.exit(1)
print(f"PASS ({'control' if CONTROL else 'real'})")

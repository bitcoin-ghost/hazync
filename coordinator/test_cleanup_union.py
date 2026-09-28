#!/usr/bin/env python3
"""`--cleanup` releases every pod the run rented, including ones it grew into (hazync#557).

⛔ WHAT THIS EXISTS FOR. `rented.json` is written by the rent phase; `--grow-to` appends recruits to
`created`, `fleet` and `state["fleet"]` but never to that file. Measured while tearing down tip
hour 2 on 2026-09-28: **rented.json held 17 pods while the fleet had grown to 26.**

A clean teardown hides it — the release loop walks the live `created` list and then sweeps
`recruited` for pods caught mid-gate, so all 26 went. It bites on the RECOVERY path:

    if a.cleanup:
        return cleanup(api, rented_path)      # reads ONLY rented.json

`--cleanup --rundir` is what releases a fleet when the driver is SIGKILLed or the box reboots. With
a grown fleet it would have released 17, reported success, and left **nine RTX PRO 6000 billing at
~$18.81/hr (~$451/day)** — and there is no budget cap by decision, so nothing else would stop them.

⚠ This is the FOURTH structure derived from the fleet and written once before the session loop:
`assignment` (#540), `fleet` and `state["fleet"]` (#547), now `rented.json`.

    python3 test_cleanup_union.py             # both records are read; every pod is released
    python3 test_cleanup_union.py --control   # rented.json only — recruits must survive
"""
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sponsor_bot  # noqa: E402
import tip_smoke  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


RENTED = [{"id": f"r{i}", "name": f"hz-smoke-{i}", "price": 2.09} for i in range(1, 18)]   # 17
GROWN = [{"id": f"g{i}", "name": f"hz-grow-{i}", "price": 2.09} for i in range(1, 10)]     # 9
STRANGER = {"id": "x1", "name": "hz-spine-1", "price": 0.0}   # someone else's, must be left alone


class FakeAPI:
    def __init__(self, live):
        self.live = {p["id"]: dict(p) for p in live}
        self.killed = []

    def pods(self):
        return list(self.live.values())


def fake_terminate(api, pid):
    api.killed.append(pid)
    api.live.pop(pid, None)
    return True


def run_cleanup(write_recruited):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "rented.json"), "w") as fh:
        json.dump(RENTED, fh)
    if write_recruited:
        with open(os.path.join(d, "recruited.json"), "w") as fh:
            json.dump(GROWN, fh)
    api = FakeAPI(RENTED + GROWN + [STRANGER])
    real, sponsor_bot.terminate_confirmed = sponsor_bot.terminate_confirmed, fake_terminate
    try:
        rc = tip_smoke.cleanup(api, os.path.join(d, "rented.json"))
    finally:
        sponsor_bot.terminate_confirmed = real
    return rc, api, d


# ⛔ The control is the OLD cleanup: one record, no union. Modelled as "recruited.json is never
# read", which is exactly what the shipped code did.
rc, api, d = run_cleanup(write_recruited=not CONTROL)

still_billing = [p["name"] for p in api.live.values() if p["name"].startswith("hz-")
                 and p["name"] != "hz-spine-1"]

if CONTROL:
    check(len(api.killed) == 17, f"control released {len(api.killed)} pod(s), not 26")
    check(len(still_billing) == 9,
          f"control reproduces it: {len(still_billing)} recruit(s) LEFT BILLING — "
          f"~${9 * 2.09:.2f}/hr, ~${9 * 2.09 * 24:.2f}/day")
    check(rc == 0, "…and it returned SUCCESS while doing so, which is the dangerous part")
else:
    check(len(api.killed) == 26, f"all 26 pods released (got {len(api.killed)})")
    check(still_billing == [], f"nothing left billing (got {still_billing})")
    check(rc == 0, "and it reports success truthfully")
    # ⛔ A pod neither record knows about is reported, never terminated: this command may only
    # release what it can show it rented. hz-spine-1 was live on the account on 2026-09-28.
    check("hz-spine-1" in [p["name"] for p in api.live.values()],
          "a pod neither record knows about is NOT terminated")
    check("x1" not in api.killed, "…and was never even attempted")
    check(not os.path.exists(os.path.join(d, "recruited.json")),
          "both records are removed once the account is clean, so neither can mislead later")

# ── the structural half: every fleet-derived record is updated where a recruit is admitted ───────
# ⚠ The four structures in one place, anchored to the admit block. Three of these were shipped
# broken one at a time; enumerating them is what stops a fifth.
if not CONTROL:
    src = open(os.path.join(HERE, "tip_smoke.py"), encoding="utf8").read()
    m = re.search(r"for item in joined:(.*?)\n                try:\n                    feed\.start",
                  src, re.S)
    block = m.group(1) if m else ""
    check(bool(block), "found the block where a recruit is admitted")
    for needle, what in (("created.append", "created (the live fleet list)"),
                         ("fleet.append", "fleet (the dashboard feed)"),
                         ('state["fleet"]', "state['fleet'] (the resume record)"),
                         ("rented_path", "rented.json (the recovery record)")):
        check(needle in block, f"admitting a recruit updates {what}")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

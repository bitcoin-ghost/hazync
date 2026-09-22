#!/usr/bin/env python3
"""The fleet ledger must group fleets correctly and refuse to store misleading facts (hazync#448).

⛔ WHY THIS EXISTS. The 4090-only default rests on nine runs on one block on one night, and nothing
kept that result in a form that outlived the run. Re-checking the claim meant re-reading a session
transcript — which is precisely how 142 s of CARD-TYPE spread got published as a geography effect.

The ledger only helps if two things hold: the same fleet always produces the same key (or rows
cannot be grouped), and a mixed fleet never collides with a uniform one (or the confound comes
straight back). Both are asserted here, with controls.

Run with --control to confirm these checks can fail.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tip_economics  # noqa: E402

G4, A40 = "NVIDIA GeForce RTX 4090", "NVIDIA A40"


def check_fleet_key_is_stable(control=False):
    """Same fleet, any order → same key. Otherwise rows never group."""
    def key(types):
        return " ".join(types) if control else tip_economics.fleet_key(types)
    a = key([G4, A40, G4])
    b = key([G4, G4, A40])
    if a != b:
        return False, f"arrival order changed the key: {a!r} vs {b!r} — rows would never group"
    if not control and "2x RTX 4090" not in a:
        return False, f"the key does not count cards: {a!r}"
    return True, f"order-independent and counted ({a})"


def check_mixed_never_collides_with_uniform(control=False):
    """⛔ THE WHOLE POINT. A mixed fleet must not be filed under the same key as a uniform one."""
    uni = tip_economics.fleet_key([G4, G4, G4])
    mix = tip_economics.fleet_key([G4, G4, A40])
    if control:
        uni = mix = "fleet"          # the control throws the distinction away
    if uni == mix:
        return False, ("a mixed fleet and a uniform one share a key — this is exactly the confound "
                       "that published 142 s of card-type spread as geography")
    return True, f"{uni!r} != {mix!r}"


def check_refuses_misleading_rows(control=False):
    """A run that produced no proof says nothing about cost per proof."""
    with tempfile.TemporaryDirectory() as d:
        led = os.path.join(d, "l.jsonl")

        def rec(**kw):
            if control:                      # the control stores whatever it is handed
                return {"stored": True}
            return tip_economics.record(ledger=led, **kw)

        bad = [
            dict(block=1, gpu_types=[G4], seconds=0, usd=0.2),       # never ran
            dict(block=1, gpu_types=[G4], seconds=None, usd=0.2),    # no wall-clock
            dict(block=1, gpu_types=[], seconds=100, usd=0.2),       # no fleet
            dict(block=1, gpu_types=[G4], seconds=100, usd=None),    # no cost
            dict(block=1, gpu_types=[G4], seconds=-5, usd=0.2),      # nonsense
        ]
        for kw in bad:
            if rec(**kw) is not None:
                return False, f"a misleading row was stored: {kw}"
        if tip_economics.record(ledger=led, block=1, gpu_types=[G4], seconds=100, usd=0.2) is None:
            return False, "a GOOD row was refused — the guard is too strict to be useful"
        return True, "5 misleading shapes refused, a real run accepted"


def check_one_bad_line_does_not_destroy_the_ledger(control=False):
    """A partial write from a killed run must not cost every other row."""
    with tempfile.TemporaryDirectory() as d:
        led = os.path.join(d, "l.jsonl")
        tip_economics.record(ledger=led, block=1, gpu_types=[G4], seconds=100, usd=0.2)
        with open(led, "a") as fh:
            fh.write('{"t": 1, "bloc\n')                    # killed mid-write
        tip_economics.record(ledger=led, block=2, gpu_types=[A40], seconds=200, usd=0.3)

        if control:
            try:
                rows = [json.loads(l) for l in open(led) if l.strip()]
            except json.JSONDecodeError:
                return False, "a truncated line raised and lost every other row"
        else:
            rows = tip_economics.load(led)
        if len(rows) != 2:
            return False, f"expected the 2 good rows, got {len(rows)}"
        return True, "a truncated line is skipped, the real rows survive"


def check_report_names_the_counterintuitive_result(control=False):
    """The faster fleet also being no dearer is the finding; a bare table lets a reader miss it."""
    rows = [
        {"fleet": "3x RTX 4090", "seconds": 272.0, "usd": 0.255},
        {"fleet": "2x A40 + 1x RTX 4090", "seconds": 395.0, "usd": 0.261},
    ]
    s = tip_economics.summarise(rows)
    if s[0]["fleet"] != "3x RTX 4090":
        return False, "the summary is not ordered fastest-first"
    txt = "" if control else tip_economics.report(rows)
    if "price per HOUR" not in txt:
        return False, ("the report does not call out that the fastest fleet is no dearer — the one "
                       "conclusion this ledger exists to keep visible")
    return True, "the counter-intuitive result is stated, not just tabulated"


CHECKS = [check_fleet_key_is_stable, check_mixed_never_collides_with_uniform,
          check_refuses_misleading_rows, check_one_bad_line_does_not_destroy_the_ledger,
          check_report_names_the_counterintuitive_result]


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        try:
            ok, why = fn(control=control)
        except Exception as e:                # noqa: BLE001
            ok, why = False, f"{type(e).__name__}: {e}"
        if control:
            if ok:
                print(f"  CONTROL DID NOT FAIL: {fn.__name__} -- {why}")
                bad += 1
            else:
                print(f"  control ok: {fn.__name__} caught it ({why})")
        else:
            print(f"  {'ok  ' if ok else 'FAIL'} {fn.__name__}: {why}")
            bad += 0 if ok else 1
    if bad:
        print(f"\n{bad} check(s) wrong")
        return 1
    print("\nthe ledger groups fleets and refuses misleading rows" if not control
          else "\ncontrols all failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

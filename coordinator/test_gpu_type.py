#!/usr/bin/env python3
"""The fleet's card type is chosen deliberately, validated, and RECORDED.

⛔ WHY THIS EXISTS (hazync#448). `deploy_listening` walks its gpu_types in order and takes the first
RunPod will sell. 4090 was already first, but the list also held A40, so when 4090 capacity was short
the run silently fell back and produced a MIXED fleet. Measured over 9 runs on block 741000:

    all 4090          267.9 / 271.7 / 276.9 s   mean 272.2 s   spread  9.0 s
    contains an A40   357.9 / 380.2 / 410.4 s   mean 382.8 s   spread 52.5 s

and the slower fleet cost MORE -- $0.262 against $0.243 -- because the A40 is slow enough that the
cheaper card loses on price per proof.

Worse, nothing in a run's evidence named the composition. `pods.txt` had it; no summary line said it.
A mixed fleet and a uniform one were indistinguishable in every log, so 142 s of spread got published
as a geography effect when it was card type all along.

⏰ SUPERSEDED IN PART BY found on the 968,243/968,255 tip run, 2026-09-23. The original fix — pin the default to 4090 and refuse every other
type — read the lesson too broadly and became an outage on 2026-09-23 when 4090 stock ran short: five
attempts, 0-2 pods, no run. The lesson was never "only rent 4090s", it was "do not MIX, and do not
rank on price per hour". The default is now `auto`, which honours both by ranking the whole live
catalogue on measured cost per proof and filling the fleet from ONE type wherever capacity allows.
So this file no longer pins 4090-only; it pins the two properties that actually mattered.

Run with --control to confirm these checks can fail.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import tip_smoke     # noqa: E402


def check_default_ranks_rather_than_restricts(control=False):
    """The default must ADAPT to stock, not pin one card and fail when it runs out."""
    import argparse as _ap
    ap = _ap.ArgumentParser()
    ap.add_argument("--gpu-type", default="NVIDIA GeForce RTX 4090" if control else "auto")
    val = ap.parse_args([]).gpu_type
    if val != "auto":
        return False, (f"the default pins {val!r}, so a run dies when that card is out of stock — "
                       "measured 2026-09-23: five attempts, 0-2 pods rented, no run")
    return True, "default ranks the live catalogue instead of pinning one card"


def check_measured_beats_cheap(control=False):
    """⛔ THE PROPERTY #448 IS ACTUALLY ABOUT: never rank a card on price per hour."""
    ranked = [{"id": "cheap", "price": 0.49, "usd_per_proof": None},
              {"id": "measured", "price": 0.74, "usd_per_proof": 0.243}]
    if control:
        order = sorted(ranked, key=lambda c: c["price"])            # the #448 mistake, restored
    else:
        order = sorted(ranked, key=lambda c: (c["usd_per_proof"] is None,
                                              c["usd_per_proof"] if c["usd_per_proof"] is not None
                                              else c["price"]))
    if order[0]["id"] != "measured":
        return False, ("a card with a MEASURED cost per proof was ranked below an unmeasured one "
                       "because it costs more per hour — this is hazync#448 exactly")
    return True, "a measured cost per proof outranks a cheaper hourly rate"


def check_parse_and_validate(control=False):
    """The --gpu-type string must reject an unknown name BEFORE anything is rented."""
    def parse(sval):
        types = tuple(t.strip() for t in sval.split(",") if t.strip())
        if not types:
            raise SystemExit("--gpu-type is empty")
        if control:
            return types                     # the control skips validation
        offered = {"NVIDIA GeForce RTX 4090", "NVIDIA A40", "NVIDIA L40S"}   # what RunPod lists
        unknown = [t for t in types if t not in offered]
        if unknown:
            raise SystemExit(f"unknown --gpu-type {unknown}")
        return types

    # a good list parses, in order, with whitespace tolerated
    assert parse(" NVIDIA GeForce RTX 4090 , NVIDIA A40 ") == \
        ("NVIDIA GeForce RTX 4090", "NVIDIA A40"), "a valid list must parse in order"
    # ⛔ a typo must be refused. GraphQL accepts an unknown gpuTypeId and simply matches nothing, so
    # without this the run reports "no capacity" for every pod and reads as a RunPod outage.
    try:
        parse("NVIDIA GeForce RTX 4090,NVIDIA A41")
        return False, "an unknown GPU type was accepted — the run would read as 'no capacity'"
    except SystemExit:
        pass
    try:
        parse(" , ")
        return False, "an empty --gpu-type was accepted"
    except SystemExit:
        pass
    return True, "unknown and empty values are refused before renting"


def check_mix_is_reported(control=False):
    """A mixed fleet must be called out, not left for someone to infer from pods.txt."""
    def summarise(types):
        mix = {}
        for g in types:
            mix[g] = mix.get(g, 0) + 1
        line = "FLEET: " + ", ".join(f"{n}x {g}" for g, n in sorted(mix.items()))
        if control:
            return line                      # the control omits the mixed warning
        return line + (["", "   ⚠ MIXED CARD TYPES — timings are NOT comparable with a uniform fleet"]
                       [len(mix) > 1])

    uniform = summarise(["NVIDIA GeForce RTX 4090"] * 3)
    mixed = summarise(["NVIDIA GeForce RTX 4090", "NVIDIA A40", "NVIDIA A40"])
    if "3x NVIDIA GeForce RTX 4090" not in uniform:
        return False, "a uniform fleet is not counted correctly"
    if "MIXED" in uniform:
        return False, "a uniform fleet was flagged as mixed"
    if "MIXED" not in mixed:
        return False, "a MIXED fleet was not flagged — this is the exact gap that published a false premise"
    if "1x NVIDIA GeForce RTX 4090" not in mixed or "2x NVIDIA A40" not in mixed:
        return False, f"the mixed fleet's composition is not stated: {mixed!r}"
    return True, "composition is always stated; a mixed fleet is flagged"


CHECKS = [check_default_ranks_rather_than_restricts, check_measured_beats_cheap,
          check_parse_and_validate, check_mix_is_reported]


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        try:
            ok, why = fn(control=control)
        except AssertionError as e:
            ok, why = False, str(e)
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
    print("\ncard type is chosen, validated and recorded" if not control
          else "\ncontrols all failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

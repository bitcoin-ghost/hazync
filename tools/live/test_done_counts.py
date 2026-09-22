#!/usr/bin/env python3
"""A verified block must COUNT — the headline figure cannot disagree with the banner.

⛔ WHAT THIS CAUGHT. `done` was inferred from five seconds of silence: "nothing has reported this
height for 5 s". The collector stops when the run ends, BEFORE its own grace period elapses, so the
LAST block of every run never counted. Captured live 2026-09-21 — `frame.png` read

    Hazync zkVM bitcoin block proofs : 0 blocks · 3 cards · $0.21
    ...
    VERIFIED block 741000 in 267.9s on 3 cards

The headline said zero while the banner beside it said verified. On a 24-hour run that is an
off-by-one on the number people actually read, at the moment they read it.

The run already writes the authoritative answer to `$RUNDIR/phase`. Silence remains a FALLBACK for a
height that has scrolled out of that line — late is fine, wrong is not.

  python3 test_done_counts.py            # assertions; exit 0 on success
  python3 test_done_counts.py --control  # the VERIFIED signal is ignored; MUST fail
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import collect  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def done_for(height, done_at, now, phase, control=False):
    """The shipped rule, or (under --control) the old silence-only one."""
    verified = set() if control else collect.verified_heights(phase)
    return int(height) in verified or (now - done_at) > 5


def check_real_capture(control=False):
    """The exact snapshot that rendered '0 blocks'. It must now count 1."""
    snap_path = os.path.join(HERE, "snapshot.json")
    if not os.path.exists(snap_path):
        return None, "snapshot.json not present (skipped)"
    snap = json.load(open(snap_path))
    blocks = snap.get("blocks") or []
    if not blocks:
        return None, "snapshot has no blocks (skipped)"
    b, now, phase = blocks[0], snap["t"], snap.get("phase")
    if "VERIFIED" not in (phase or ""):
        return None, "captured snapshot is not a verified run (skipped)"
    if not done_for(b["h"], b["done_at"], now, phase, control):
        return False, (f"block {b['h']} is VERIFIED in the phase line but does not count — "
                       "this is the '0 blocks' bug")
    return True, f"block {b['h']} counts ({phase!r})"


def check_silence_still_works(control=False):
    """A height absent from the phase line must still count once it has gone quiet."""
    # 60 s since the last sample, and a phase line naming a DIFFERENT block.
    if not done_for(881457, now := 1_000_000.0, now + 60, "VERIFIED block 999999 in 1s", control):
        return False, "a long-quiet height stopped counting — the fallback was lost"
    return True, "silence fallback intact for heights outside the phase line"


def check_unfinished_does_not_count(control=False):
    """A block still being proved must NOT count, or the headline runs ahead of reality."""
    # 1 s since the last sample, nothing verified.
    if done_for(881457, now := 1_000_000.0, now + 1, "PROVING block 881457", control):
        return False, "a block still in flight was counted as done"
    return True, "in-flight blocks do not count"


def check_parser_is_strict(control=False):
    """It must not invent heights from unrelated text."""
    if collect.verified_heights(None) or collect.verified_heights(""):
        return False, "empty input produced heights"
    if collect.verified_heights("PREPARING · renting 3 cards (+3 spare)"):
        return False, "a non-VERIFIED phase produced heights"
    if collect.verified_heights("VERIFIED block 967501 in 9s") != {967501}:
        return False, "a single VERIFIED line did not parse"
    if collect.verified_heights("VERIFIED block 1 · VERIFIED block 2") != {1, 2}:
        return False, "multiple VERIFIED lines did not all parse"
    return True, "parses only real VERIFIED lines, single and multiple"


CHECKS = [check_real_capture, check_silence_still_works,
          check_unfinished_does_not_count, check_parser_is_strict]
# Under --control only the checks that depend on the VERIFIED signal must flip.
CONTROL_MUST_FAIL = {check_real_capture}


def main():
    control = "--control" in sys.argv
    bad = 0
    for fn in CHECKS:
        ok, why = fn(control=control)
        if ok is None:
            print(f"  skip {fn.__name__}: {why}")
            continue
        if control and fn in CONTROL_MUST_FAIL:
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
    print("\na verified block counts" if not control else "\ncontrol failed as required")
    return 0


if __name__ == "__main__":
    sys.exit(main())

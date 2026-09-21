#!/usr/bin/env python3
"""The fleet's prover pin must not fall behind the releases in this repo.

⛔ WHY. `tip_smoke.HOST_URL` pinned **v0.21.0** and nothing noticed for seven releases. Every tip run
proved correctly and produced none of the evidence the fleet exists for, because the instrumentation
shipped after the pin:

    #236  v0.21.1  stream segments as produced  -> no `(streamed)` marker
    #254  v0.21.2  seg-connect task timestamps  -> no epochs, no overlap computable
    #402  v0.21.7  seg-connect RECONNECTS       -> a dropped worker is simply gone

It cost a real run to find (block 741,000, 3 cards, $0.21, VERIFIED, zero measurements).

⚠ NO NETWORK. It compares the pin against the newest `RELEASE_NOTES_v*.md` in `docs/history/releases/`,
which is in the repo — so it works offline, in CI, and it cannot flake on GitHub being slow. It is a
FLOOR, not an equality: the pin may be newer than any notes file (a release whose notes are not merged
yet), it may simply not be older.

⚠ A deliberate downgrade is not impossible, it just has to be loud — change this test with it, and the
next reader sees a decision instead of an accident.

  python3 test_host_pin.py            # must PASS
  python3 test_host_pin.py --control  # pretends the pin is v0.21.0 again; MUST FAIL
"""
import os
import pathlib
import re
import sys

HERE = pathlib.Path(os.path.dirname(os.path.abspath(__file__)))
NOTES = HERE.parent / "docs" / "history" / "releases"
CONTROL = "--control" in sys.argv


def ver(s):
    """'v0.21.7' -> (0, 21, 7). Returns None for anything that is not a release tag."""
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", str(s).strip())
    return tuple(int(x) for x in m.groups()) if m else None


def newest_release_in_repo():
    """The highest version with release notes committed here, or None if there are none."""
    seen = []
    if NOTES.is_dir():
        for p in NOTES.glob("RELEASE_NOTES_v*.md"):
            m = re.search(r"(v\d+\.\d+\.\d+)", p.name)
            if m and ver(m.group(1)):
                seen.append(ver(m.group(1)))
    return max(seen) if seen else None


def main():
    fails = []

    def check(ok, what):
        print(f"  {'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            fails.append(what)

    sys.path.insert(0, str(HERE))
    import tip_smoke

    pin = "v0.21.0" if CONTROL else getattr(tip_smoke, "HOST_RELEASE", None)
    check(pin is not None,
          "tip_smoke exposes HOST_RELEASE — a bare URL cannot be checked, so the version is named")
    pv = ver(pin)
    check(pv is not None, f"HOST_RELEASE parses as a release tag ({pin!r})")

    check(pin and pin in getattr(tip_smoke, "HOST_URL", ""),
          "HOST_URL is built FROM HOST_RELEASE — otherwise the two drift and the check is theatre")

    newest = newest_release_in_repo()
    check(newest is not None,
          f"found release notes to compare against in {NOTES}")

    if pv and newest:
        pretty = "v%d.%d.%d" % newest
        check(pv >= newest,
              f"⛔ the fleet's prover pin {pin} is NOT older than the newest release in this repo "
              f"({pretty}) — a stale pin proves blocks correctly and produces no evidence")

    print()
    if CONTROL:
        hit = [f for f in fails if "NOT older than" in f]
        if hit:
            print("CONTROL OK — the pin was set back to v0.21.0 and the check caught it:")
            for f in hit:
                print(f"  - {f}")
            return 0
        print("CONTROL FAILED — v0.21.0 went undetected, which is exactly how this shipped.")
        return 1
    if fails:
        print(f"FAILED {len(fails)}")
        return 1
    print("all good")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Tests for hazync-prune-bundles.py -- the production caller (hazync#347, #397).

WHY SEPARATELY FROM test_prune_bundles.py. That file tests the DECISION (which bundles may go). This
tests the thing that decides whether the decision is allowed to run at all: prune_bundles refuses
without `confirmed_names`, so the caller's only job is to obtain that honestly, and its failure mode is
the dangerous direction -- hand over a PARTIAL or EMPTY set and every unlisted receipt looks "not
backed up", which is safe, but a half-read that SUCCEEDS is indistinguishable from a complete one.

So what is pinned here is refusal: either store unreadable, or either store listing zero, must stop
everything with exit 2 and never reach prune.

  python3 test_prune_caller.py            # must PASS
  python3 test_prune_caller.py --control  # the both-or-nothing guard removed; MUST FAIL
"""
import importlib.util
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "deploy", "hazync-prune-bundles.py")

fails = []
def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)

src = open(SRC).read()
GUARD = "            return None"
if src.count(GUARD) < 2:
    print(f"CANNOT TEST: the both-or-nothing returns are not in {SRC}.")
    sys.exit(2)
work = tempfile.mkdtemp(prefix="prunecaller_")
mod_path = os.path.join(work, "caller.py")
if CONTROL:
    # ⛔ THE MUTATION MUST BE THE HAZARD, NOT A CRASH. Replacing the refusals with `pass` fell through
    # to `if not names:` in the branch where the exception had prevented `names` from ever being
    # assigned, so the control died with UnboundLocalError -- failing for a reason that has nothing to
    # do with the guard, and never producing a comparable failure set. `return out` is the real
    # hazard: prune is handed a PARTIAL confirmation, which is indistinguishable from a complete one.
    src = src.replace(GUARD, "            return out  # CONTROL: partial confirmation passed through")
open(mod_path, "w").write(src)
ld = SourceFileLoader("caller", mod_path)
spec = importlib.util.spec_from_loader("caller", ld)
caller = importlib.util.module_from_spec(spec); ld.exec_module(caller)

keyfile = os.path.join(work, "k.keys")
open(keyfile, "w").write("kid secret https://acct.r2.cloudflarestorage.com\n")
caller.STORES = (("r2", keyfile, "b1"), ("b2", keyfile, "b2"))
caller.REPO = work
os.makedirs(os.path.join(work, "reproduce"), exist_ok=True)
open(os.path.join(work, "reproduce", "METHOD_ID"), "w").write("# id\n" + "37987b85" + "0"*56 + "\n")

class FakeOff:
    """Stands in for hazync-offsite-proofs: returns a chosen listing per bucket."""
    def __init__(self, listings): self.listings = listings
    def make_client(self, *a, **k): return "client"
    def method_prefix(self, repo): return "37987b85"
    def list_remote(self, s3, bucket, prefix):
        v = self.listings.get(bucket)
        if isinstance(v, Exception): raise v
        return {n: 1 for n in v}

class SpyPrune:
    def __init__(self): self.called_with = None
    def main(self, argv, confirmed_names=None):
        self.called_with = confirmed_names
        return 0

def run(listings):
    p = SpyPrune()
    rc = caller.main([], off=FakeOff(listings), prune=p, log=lambda *a: None)
    return rc, p

# 1. The happy path: both stores answer, prune is reached with both sets.
rc, p = run({"b1": ["proof_1.bin", "proof_2.bin"], "b2": ["proof_1.bin"]})
check(rc == 0 and p.called_with is not None and set(p.called_with) == {"r2", "b2"},
      f"both stores confirmed -> prune runs with both sets (rc={rc})")

# 2. ⛔ THE CENTRAL CASE. One store unreadable must stop everything, and prune must NEVER be reached.
rc, p = run({"b1": ["proof_1.bin"], "b2": RuntimeError("network")})
check(rc == 2 and p.called_with is None,
      f"a store that cannot be listed exits 2 and prune is never called (rc={rc})")

# 3. An EMPTY listing is not 'nothing is backed up' -- it is a failed read wearing a success costume.
rc, p = run({"b1": ["proof_1.bin"], "b2": []})
check(rc == 2 and p.called_with is None,
      f"a store listing zero objects exits 2 rather than pruning everything (rc={rc})")

# 4. The first store failing stops it just as hard as the second.
rc, p = run({"b1": RuntimeError("403"), "b2": ["proof_1.bin"]})
check(rc == 2 and p.called_with is None,
      f"the FIRST store failing also exits 2 (rc={rc})")

EXPECTED_CONTROL_FAILURES = {
    "a store that cannot be listed exits 2 and prune is never called (rc=0)",
    "a store listing zero objects exits 2 rather than pruning everything (rc=0)",
    "the FIRST store failing also exits 2 (rc=0)",
}

print()
if CONTROL:
    got = set(fails)
    if got == EXPECTED_CONTROL_FAILURES:
        print(f"CONTROL OK — the both-or-nothing guard was removed and exactly the {len(got)} "
              "assertion(s) that depend on it failed:")
        for f in sorted(got): print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — removing the guard did not produce the expected failures.")
    for f in sorted(EXPECTED_CONTROL_FAILURES - got): print(f"  should have failed and did not: {f}")
    for f in sorted(got - EXPECTED_CONTROL_FAILURES): print(f"  failed unexpectedly: {f}")
    sys.exit(1)

if fails:
    print(f"FAILED {len(fails)}: " + "; ".join(fails)); sys.exit(1)
print("all good")

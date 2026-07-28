#!/usr/bin/env bash
# Every mirror of RangeState must have the SAME FIELDS IN THE SAME ORDER.
#
# The journal is decoded POSITIONALLY. A reordering in any mirror does not fail — it silently
# misinterprets a valid proof, which is worse than a crash: a verifier reading `out_leaves` where
# `in_leaves` belongs reports confident nonsense and nothing flags it.
#
# The struct lives in four places and cannot simply be deduplicated:
#   prover/methods/guest/src/main.rs   AUTHORITATIVE — this is what is committed to the journal
#   prover/host/src/main.rs            mirror
#   verifier/src/main.rs               mirror
#   rangestate/src/lib.rs              the shared crate, for NEW consumers (ffi, ghostd)
#
# The guest deliberately does NOT depend on the crate: moving code into a crate the guest links
# changes the compiled ELF and therefore METHOD_ID, forcing a full re-baseline. So the guest keeps its
# copy and this check keeps everyone else honest. Unifying them is a ride-along for the next
# re-baseline (coordinator/deploy/RUNBOOK.md).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
exec python3 - <<'PY'
import re, sys

SRC = {
    "guest":    "prover/methods/guest/src/main.rs",
    "host":     "prover/host/src/main.rs",
    "verifier": "verifier/src/main.rs",
    "crate":    "rangestate/src/lib.rs",
}

def fields(path):
    """Field names, in declaration order, from `struct RangeState { … }`.

    Fields are NOT one per line — the guest and host pack several onto a line (`lo: u32, hi: u32,`),
    which an earlier line-anchored regex silently truncated to 10 of 19 and made every mirror look
    broken. Match every `ident:` that is not part of a `::` path instead.
    """
    src = open(path).read()
    m = re.search(r"struct RangeState\s*\{(.*?)\n\}", src, re.S)
    if not m:
        return None
    body = re.sub(r"//.*", "", m.group(1))                 # strip line comments
    return re.findall(r"(?:^|[,{\s])(?:pub\s+)?([a-z_][a-z0-9_]*)\s*:(?!:)", body)

ref = fields(SRC["guest"])
if not ref or len(ref) < 15:
    print(f"FAIL could not extract RangeState from the guest ({SRC['guest']}) — got "
          f"{len(ref) if ref else 0} fields.")
    print("     This check is the only thing keeping four hand-maintained mirrors in step;")
    print("     it must never pass by failing to find anything.")
    sys.exit(1)
print(f"authoritative (guest): {len(ref)} fields")

bad = 0
for who in ("host", "verifier", "crate"):
    got = fields(SRC[who])
    if got == ref:
        print(f"  ok   {who:<9} matches the guest field-for-field ({len(got)})")
    else:
        print(f"FAIL {who} ({SRC[who]}) does not match the guest's RangeState")
        print(f"       guest: {ref}")
        print(f"       {who}: {got}")
        print("     The journal decodes POSITIONALLY — this would MISREAD a valid proof, not reject it.")
        bad = 1
sys.exit(bad)
PY

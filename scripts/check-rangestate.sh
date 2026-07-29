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
    """(name, type) pairs in declaration order, from `struct RangeState { … }`.

    TYPES ARE COMPARED, not just names. An earlier version matched names and order only, and a change
    of `in_leaves: u64` to `u32` passed cleanly — which would silently corrupt every decode, i.e.
    exactly the failure this check exists to prevent. Names alone are not enough.

    Fields are NOT one per line — the guest and host pack several onto a line (`lo: u32, hi: u32,`),
    which a line-anchored regex truncated to 10 of 19 and made every mirror look broken.
    """
    src = open(path).read()
    m = re.search(r"struct RangeState\s*\{(.*?)\n\}", src, re.S)
    if not m:
        return None
    body = re.sub(r"//.*", "", m.group(1))                 # strip line comments
    out = []
    # name: type, where type runs to the next top-level comma (depth-aware: Vec<Option<[u8; 32]>>
    # contains both commas and brackets).
    for mm in re.finditer(r"(?:^|[,{\s])(?:pub\s+)?([a-z_][a-z0-9_]*)\s*:(?!:)", body):
        name, i, depth, buf = mm.group(1), mm.end(), 0, []
        while i < len(body):
            c = body[i]
            if c in "<([": depth += 1
            elif c in ">)]": depth -= 1
            elif c == "," and depth <= 0: break
            buf.append(c); i += 1
        out.append((name, re.sub(r"\s+", "", "".join(buf))))
    return out

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
        for a, b in zip(ref, got):
            if a != b:
                print(f"       guest has {a[0]}: {a[1]}   but {who} has {b[0]}: {b[1]}")
        if len(ref) != len(got):
            print(f"       field COUNT differs: guest {len(ref)}, {who} {len(got)}")
        print("     The journal decodes POSITIONALLY — this would MISREAD a valid proof, not reject it.")
        bad = 1
sys.exit(bad)
PY

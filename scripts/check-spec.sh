#!/bin/bash
# docs/SPEC.md states formats normatively. A spec that has drifted from the code is worse than no
# spec: a reviewer implements against it, their verifier disagrees with ours, and the disagreement
# looks like OUR bug. So the concrete values in it are checked against the source they describe.
#
# Only mechanically checkable claims are covered — constants, tags, and the full RangeState field
# list. Prose is not, and cannot be.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
exec python3 - <<'PY'
import re, sys

spec = open('docs/SPEC.md').read()
rs   = open('rangestate/src/lib.rs').read()
acc  = open('accumulator/src/lib.rs').read()
bad  = 0

def check(name, ok, detail=""):
    global bad
    if ok:
        print(f"  ok   {name}")
    else:
        print(f"FAIL {name} {detail}")
        bad = 1

m = re.search(r'KIND_RANGE: u32 = (0x[0-9A-Fa-f_]+)', rs)
check("KIND_RANGE matches rangestate",
      bool(m) and m.group(1).replace('_','').lower() in spec.lower().replace('_',''))

for name, pat in [("GENESIS_BITS", r'GENESIS_BITS: u32 = (0x[0-9a-f_]+)'),
                  ("GENESIS_TIME", r'GENESIS_TIME: u32 = ([0-9_]+)')]:
    m = re.search(pat, rs)
    check(f"{name} matches rangestate", bool(m) and m.group(1).replace('_','') in spec.replace('_',''))

for tag, val in [("TAG_LEAF", "0x00"), ("TAG_NODE", "0x01")]:
    check(f"{tag} matches the accumulator",
          f'{tag}: u8 = {val}' in acc and f'{tag} = {val}' in spec)

fields = re.findall(r'pub ([a-z_0-9]+):', rs[rs.index('pub struct RangeState'):])

# Scope to the journal's own code block. Field names like `lo` and `hi` also appear in the prose of
# earlier sections, and matching those would compare positions in the wrong text entirely.
sec = spec[spec.index('## 8. Journal'):]
block = sec[sec.index('```') + 3:]
block = block[:block.index('```')]

missing = [f for f in fields if not re.search(rf'^{f}\b', block, re.M)]
check(f"all {len(fields)} RangeState fields listed in the journal block",
      not missing, f"missing: {missing}")

# The journal decodes positionally, so the listing must follow DECLARATION order.
pos = [block.index(f) for f in fields if f in block]
check("journal block lists fields in declaration order", pos == sorted(pos))

sys.exit(bad)
PY

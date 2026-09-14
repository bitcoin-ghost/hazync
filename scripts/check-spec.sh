#!/bin/bash
# docs/SPEC.md states formats normatively. A spec that has drifted from the code is worse than no
# spec: a reviewer implements against it, their verifier disagrees with ours, and the disagreement
# looks like OUR bug. So the concrete values in it are checked against the source they describe.
#
# Only mechanically checkable claims are covered — constants, tags, the full RangeState field list,
# and the in-boundary fields §9 says a genesis-anchored proof pins. Prose is not, and cannot be.
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

# §9 — genesis anchoring. Audit #3 F-2/F-3 was a verifier that pinned every in-boundary field except
# `in_smt_root`, the one #54 added, and §9's own list had the same hole. So the field list is taken
# from the predicate every verifier now calls, `RangeState::is_genesis_anchored`, rather than typed.
start = rs.index('fn is_genesis_anchored')
body = rs[start:rs.index('\n    }\n', start)]
gfields = sorted(set(re.findall(r'self\.([a-z_0-9]+)', body)))
# A predicate the regex cannot read yields an empty list, and every check below would pass on it.
check("is_genesis_anchored parsed (it must at least pin lo and in_tip_hash)",
      'lo' in gfields and 'in_tip_hash' in gfields, f"got {gfields}")

g = spec[spec.index('## 9. Genesis anchoring'):spec.index('## 10.')]
# Scope to the condition LIST, not the whole section. The prose under the list names `in_smt_root`
# while explaining why it matters, so a section-wide search passed with that bullet deleted — found by
# running exactly that control.
m = re.search(r'(?m)^- .*(?:\n(?:- |  ).*)*', g)
conds = m.group(0) if m else ''
gmissing = [f for f in gfields if not re.search(rf'`{f}\b', conds)]
check(f"§9's condition list names all {len(gfields)} fields is_genesis_anchored pins",
      bool(conds) and not gmissing, f"missing: {gmissing}")

# "All N MUST be checked" has to count the list it sits under, or adding a condition leaves the
# sentence asserting fewer checks than the spec requires.
words = {'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8,
         'nine': 9, 'ten': 10}
bullets = len(re.findall(r'^- ', conds, re.M))
m = re.search(r'All (\w+) MUST be checked', g)
check("§9's stated condition count matches its list",
      bool(m) and words.get(m.group(1).lower()) == bullets,
      f"says {m.group(1) if m else '(no count)'}, lists {bullets}")

sys.exit(bad)
PY

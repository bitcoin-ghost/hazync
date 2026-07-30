#!/bin/bash
# G4 gate: a UTXO snapshot must bind to the proof that attests to it, and must FAIL to bind when it
# is not that set.
#
# The positive case alone is close to worthless here. At a height with no spends the accumulator is
# just leaves appended in order, so a snapshot "binds" without the ordering ever having been
# exercised — which is precisely the property this was built to enforce, and the one I originally got
# wrong. So the gate runs at a height WITH spends and asserts the negatives too.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
H="${HAZYNC_HOST:-./prover/target/release/host}"
SNAP="${1:-/root/snap500/utxo.snap}"
PROOF="${2:-/root/fixgen/range_500.bin}"

[ -x "$H" ] || { echo "SKIP: no host binary at $H"; exit 0; }
[ -f "$SNAP" ] && [ -f "$PROOF" ] || { echo "SKIP: snapshot or proof not present"; exit 0; }

fail=0
# `|| rc=$?` — a bare `cmd; rc=$?` dies under set -e before the assignment runs, and reading $? after
# a pipe reports the LAST command's status, not the one under test.
rc=0; "$H" snapshot-verify "$SNAP" "$PROOF" >/dev/null 2>&1 || rc=$?
[ $rc -eq 0 ] && echo "  ✓ the real snapshot binds" \
              || { echo "  ✗ the real snapshot did NOT bind (exit $rc) — every negative below is vacuous"; fail=1; }

# Same SET, different ORDER. This is the whole point: the forest is an ordered array with swap-and-pop
# deletion, so a set alone does not determine the roots.
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
python3 - "$SNAP" "$TMP" <<'PY'
import struct, sys
b = open(sys.argv[1], 'rb').read()
assert b[:8] == b'HZSNAP01'
count = struct.unpack('<Q', b[12:20])[0]
recs, p = [], 20
for _ in range(count):
    s = p; p += 44
    sl = struct.unpack('<I', b[p:p+4])[0]; p += 4 + sl + 5
    recs.append(b[s:p])
assert p == len(b), "snapshot parse desync"
assert count >= 2, "need at least two records to reorder"
recs[0], recs[1] = recs[1], recs[0]
open(sys.argv[2], 'wb').write(b[:20] + b''.join(recs))
PY
rc=0; "$H" snapshot-verify "$TMP" "$PROOF" >/dev/null 2>&1 || rc=$?
[ $rc -ne 0 ] && echo "  ✓ the same set in a different ORDER does not bind" \
              || { echo "  ✗ reordering still bound — leaf order is not being enforced"; fail=1; }

exit $fail

#!/bin/bash
# Re-point every embedded METHOD_ID after a guest re-baseline.
#
# The id lives in more places than anyone remembers, which is exactly why check-versions.sh exists.
# Doing it by hand is how a doc ends up naming a superseded id as current — the failure that script
# was written for. This does the mechanical half; it deliberately does NOT touch reproduce/METHOD_ID's
# prose, because the *reason* for a re-baseline has to be written by whoever made the change.
#
#   ./scripts/rebaseline-id.sh <new-64-hex-id>
#
# WHAT THIS CANNOT DO: the SNARK fixtures under prover/testdata/snark/ are PROOFS produced by the old
# guest. They do not get re-pointed, they get REGENERATED — a proof carries its guest id inside it.
# Until they are, four CI steps fail (verifier exit codes, FFI smoke, WASM parity), and they SHOULD
# fail: a verifier pinned to a new id genuinely cannot accept an old proof. That is the check working.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

NEW="${1:-}"
if ! [[ "$NEW" =~ ^[0-9a-f]{64}$ ]]; then
    echo "usage: $0 <new-64-hex-METHOD_ID>" >&2
    exit 2
fi

OLD=$(grep -oE '^[0-9a-f]{64}$' reproduce/METHOD_ID | tail -1)
if [ -z "$OLD" ]; then
    echo "could not read the current canonical id from reproduce/METHOD_ID" >&2
    exit 1
fi
if [ "$OLD" = "$NEW" ]; then
    echo "already at $NEW — nothing to do"
    exit 0
fi

echo "  $OLD"
echo "  -> $NEW"
echo

# Every tracked text file that names the old id. Deliberately repo-wide rather than a hardcoded list:
# the hardcoded list is what goes stale.
mapfile -t FILES < <(git ls-files | xargs grep -l "$OLD" 2>/dev/null)
for f in "${FILES[@]}"; do
    n=$(grep -c "$OLD" "$f")
    sed -i "s/$OLD/$NEW/g" "$f"
    printf '  %-44s %d occurrence(s)\n' "$f" "$n"
done

# reproduce/METHOD_ID is the source of truth and its trailing line must be the bare id.
tail -1 reproduce/METHOD_ID | grep -qE "^$NEW$" \
    || { echo "::error::reproduce/METHOD_ID does not end with the new bare id"; exit 1; }

echo
echo "Now, and in this order:"
echo "  1. write WHY into reproduce/METHOD_ID (the supersession note) — it is the audit trail"
echo "  2. regenerate prover/testdata/snark/*.snark with the NEW guest (they are old proofs)"
echo "  3. ./scripts/check-versions.sh && ./scripts/check-utreexo.sh"

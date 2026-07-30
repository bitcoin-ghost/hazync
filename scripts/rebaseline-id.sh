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

# Every tracked TEXT file that names the old id, excluding two categories that must never be rewritten.
# Repo-wide rather than a hardcoded list, because the hardcoded list is what goes stale.
#
#   prover/evidence/**  are RECORDS OF PAST RUNS. Those runs really did happen under the old guest;
#                       rewriting the id in them does not update a reference, it falsifies the
#                       measurement. The first version of this script rewrote five evidence files.
#   short forms         `3f52baff…` appears in the supersession CHAIN in ROADMAP.md and in past-tense
#                       statements ("v0.10.0's 3f52baff changes the guest ELF again"). Those are
#                       history and must survive. Only full 64-hex occurrences are rewritten; the
#                       short-form sites need judgement per site, and check-versions.sh is what
#                       catches the ones that genuinely needed updating.
#   binaries            embed the id in compiled code. The substitution is length-preserving so it
#                       does not visibly corrupt the file — it produces a binary that CLAIMS the new
#                       id while containing the old build, which is worse than a corrupt one. The
#                       aarch64 verifier in verifier/dist/ has to be REBUILT, not edited.
mapfile -t FILES < <(git ls-files | grep -v '^prover/evidence/' | while read -r f; do
    grep -Iq . "$f" 2>/dev/null || continue          # -I: skip binary files
    grep -lq "$OLD" "$f" 2>/dev/null && echo "$f"
done)
for f in "${FILES[@]}"; do
    n=$(grep -c "$OLD" "$f")
    sed -i "s/$OLD/$NEW/g" "$f"
    printf '  %-44s %d occurrence(s)\n' "$f" "$n"
done

# reproduce/METHOD_ID is the source of truth and its trailing line must be the bare id.
tail -1 reproduce/METHOD_ID | grep -qE "^$NEW$" \
    || { echo "::error::reproduce/METHOD_ID does not end with the new bare id"; exit 1; }

echo
echo "NOT touched, deliberately:"
echo "  SHORT-form ids (3f52baff…)          they appear in HISTORY as well as in current claims"
echo "  prover/evidence/**                  records of runs made under the OLD guest — history, not references"
echo "  verifier/dist/hazync-verify-aarch64 a binary; it must be REBUILT, not edited"
echo
echo "Now, and in this order:"
echo "  1. write WHY into reproduce/METHOD_ID (the supersession note) — it is the audit trail"
echo "  2. regenerate prover/testdata/snark/*.snark with the NEW guest (they are old proofs)"
echo "  3. rebuild verifier/dist/hazync-verify-aarch64 and refresh its .sha256"
echo "  4. ./scripts/check-versions.sh && ./scripts/check-utreexo.sh && ./scripts/check-spec.sh"

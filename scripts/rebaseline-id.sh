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
#   docs/history/**,    same category: dated records (the development record, release bodies, the
#   docs/RELEASE_NOTES_*, changelog). A dated log is history by construction. The overnight log, then at
#   CHANGELOG.md        tasks/overnight_2026-08-03.md, was missed here: the full-hex pass did not
#                       exclude tasks/, so the b161735a re-baseline (a4b8918) rewrote the line recording
#                       what the container id WAS that night, dfc9eeda, into b161735a — turning a
#                       measurement into a false one — and the 4722cec8 re-baseline only relabelled it
#                       "at the time". The list is HISTORY_RE below; check-versions.sh exempts the
#                       dated-record part of it from its currency checks, so the gate never demands a
#                       rewrite there that this script refuses to make.
#   prover/testdata/**  describes which guest PRODUCED the committed fixtures, and a proof carries its
#                       guest id inside it. Re-pointing the prose does not re-point the .snark files —
#                       it just makes the README claim they were made by a guest that never touched
#                       them, hiding the very fact that they now need regenerating. Same re-baseline,
#                       same falsification, undone by hand alongside tasks/.
#   short forms         `3f52baff…` appears in supersession CHAINS (reproduce/METHOD_ID,
#                       docs/history/ROADMAP.md) and in past-tense
#                       statements ("v0.10.0's 3f52baff changes the guest ELF again"). Those are
#                       history and must survive. Only full 64-hex occurrences are rewritten; the
#                       short-form sites need judgement per site, and check-versions.sh is what
#                       catches the ones that genuinely needed updating.
#   binaries            embed the id in compiled code. The substitution is length-preserving so it
#                       does not visibly corrupt the file — it produces a binary that CLAIMS the new
#                       id while containing the old build, which is worse than a corrupt one. No
#                       guest-dependent binary is committed any more (hazync#85): release-sign.yml builds
#                       hazync-verify-aarch64 from the tag and asserts its embedded id.
HISTORY_RE='^(prover/evidence/|prover/testdata/|docs/history/|docs/RELEASE_NOTES_|CHANGELOG\.md$)'
mapfile -t FILES < <(git ls-files | grep -Ev "$HISTORY_RE" | while read -r f; do
    grep -Iq . "$f" 2>/dev/null || continue          # -I: skip binary files
    grep -lq "$OLD" "$f" 2>/dev/null && echo "$f"
done)
for f in "${FILES[@]}"; do
    n=$(grep -c "$OLD" "$f")
    sed -i "s/$OLD/$NEW/g" "$f"
    printf '  %-44s %d occurrence(s)\n' "$f" "$n"
done

# SHORT-FORM IDS ON CLAIM LINES (hazync#86).
#
# This script used to skip short ids entirely, reasoning that "3f52baff…" may be history rather than a
# current claim — sound, and it left a hole the size of the problem. The docs state the CURRENT id in
# short form ("the current canonical id is `3f52baff…`"), and check-versions REQUIRES the canonical
# short id to appear in them. So the script and the gate disagreed, and every re-baseline became a
# round of hand-editing driven by gate failures. The #54 re-baseline took five rounds across eleven
# sites, and docs/ROADMAP.md (now docs/history/ROADMAP.md) still named a retired id as canonical through TWO
# of them.
#
# The fix is not to replace short ids blindly — that would corrupt the supersession chains, which is
# what the old reasoning was protecting. It is to replace them on exactly the lines the GATE would
# flag: a line that claims currency (current|canonical|latest) and does not label itself as history
# (superseded). Same rule, same place, so the two agree by construction rather than by discipline.
OLD8=${OLD:0:8}; NEW8=${NEW:0:8}
#
# SCANNED SEPARATELY FROM $FILES, and that is the whole point. $FILES is discovered by grepping for the
# FULL 64-hex id — so a doc that only ever writes the SHORT form is not in it, and iterating $FILES
# here reaches nothing. That is exactly how docs/PROVING.md, SECURITY.md and docs/ROADMAP.md were
# missed (ROADMAP.md has since moved to docs/history/): check-versions required the canonical SHORT id in
# each of them, and none of the three
# contains the long form at all.
echo
echo "short-form ids on lines that CLAIM currency (same rule check-versions applies):"
while read -r f; do
    [ -f "$f" ] || continue
    tmp=$(mktemp)
    # Same claim rule as check-versions.sh check 8, including a claim that WRAPS: a claim line naming no id,
    # whose open sentence ends by introducing one ("... id is", "METHOD_ID:"), carries to the next line up
    # to its first sentence break.
    awk -v old="$OLD8" -v new="$NEW8" '
        /^[[:space:]]*```/ { infence = !infence; carry = 0; print; next }
        infence { carry = 0; print; next }
        {
            was = carry; carry = 0
            if (!/[Ss]upersed/) {
                if (/[Cc]urrent|[Cc]anonical|[Ll]atest/) {
                    if ($0 !~ /[0-9a-f]{8}/ && /([Cc]urrent|[Cc]anonical|[Ll]atest)[^.;!?]*([Ii][Dd]|METHOD_ID|is|was|:)[`*]*[[:space:]]*$/) carry = 1
                    gsub(old, new)
                } else if (was) {
                    head = $0; tail = ""
                    if (match($0, /[.;!?]( |$)/)) { head = substr($0, 1, RSTART); tail = substr($0, RSTART + 1) }
                    gsub(old, new, head); $0 = head tail
                }
            }
            print
        }' "$f" > "$tmp"
    if cmp -s "$tmp" "$f"; then rm -f "$tmp"; continue; fi
    n=$(diff "$f" "$tmp" | grep -c '^<')
    mv "$tmp" "$f"
    printf '  %-44s %d claim line(s)\n' "$f" "$n"
done < <(git ls-files '*.md' | grep -Ev "$HISTORY_RE")
echo "  (HISTORY_RE records and fenced evidence left alone — those legitimately name retired ids)"
echo

# reproduce/METHOD_ID is the source of truth and its trailing line must be the bare id.
tail -1 reproduce/METHOD_ID | grep -qE "^$NEW$" \
    || { echo "::error::reproduce/METHOD_ID does not end with the new bare id"; exit 1; }

echo
echo "NOT touched, deliberately:"
echo "  prover/evidence/**, docs/history/**  records made under the OLD guest — history, not references"
echo "  (and the rest of HISTORY_RE)"
echo "  verifier binaries                   none committed; release-sign.yml builds them from the tag (#85)"
echo "  the DEPLOYED site                   a live host; step 5 below, and nothing here can do it for you"
echo
echo "Now, and in this order:"
echo "  1. write WHY into reproduce/METHOD_ID (the supersession note) — it is the audit trail"
echo "  2. regenerate prover/testdata/snark/*.snark with the NEW guest (they are old proofs)"
echo "  3. nothing to rebuild under verifier/dist/ — the release workflow builds and id-checks the aarch64"
echo "     verifier; check-versions.sh confirms verifier/ and verifier-ffi/ embed the new id"
echo "  4. ./scripts/check-versions.sh && ./scripts/check-utreexo.sh && ./scripts/check-spec.sh"
echo "  5. cut the release, then DEPLOY the new hazync-verify.wasm to the web box — the browser"
echo "     verifier is pinned to the guest and a re-baseline silently invalidates the deployed copy."
echo "     Verify the release asset against SHA256SUMS.txt.asc first, then:"
echo "       sudo install -o www-data -g www-data -m 644 hazync-verify.wasm \\"
echo "            /var/www/bitcoinghost/hazync/verify/hazync-verify.wasm"
echo "  6. ./scripts/check-deployed-verifier.sh   <- the only step that proves a reader can still"
echo "     check a proof. Skipping 5 leaves the site telling visitors the spine is FORGED, and the"
echo "     stale and correct modules are byte-for-byte the SAME SIZE, so nothing else notices."

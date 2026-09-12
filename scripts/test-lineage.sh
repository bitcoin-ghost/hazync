#!/usr/bin/env bash
# The append-only lineage gate (#244).
#
# What this has to prove is not that the check passes today -- it does, on a correct file -- but that
# it FAILS on each way the back catalogue actually gets destroyed:
#
#   a row deleted     a blind re-baseline sweep taking a superseded section with it. This is the
#                     motivating failure: check-versions.sh validates ids that ARE mentioned, so a
#                     deleted row looks exactly like an id that never existed.
#   a row rewritten   the same count, different contents. A check that compares LENGTHS passes this,
#                     which is why the gate diffs.
#   a stale view      the file no longer ends at the id the repo ships.
#   a shallow clone   the reconstruction is TRUNCATED, so every unreachable row reads as deleted.
#                     Measured, not hypothetical: this repo was grafted at 412 commits and the first
#                     run of the generator silently lost the two oldest ids, d1fc4065 among them.
#                     A gate that cries deletion on a shallow clone gets switched off.
#
#   scripts/test-lineage.sh            # must PASS
#   scripts/test-lineage.sh --control  # count comparison instead of a diff, in a COPY; must FAIL
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
CONTROL=0; [ "${1:-}" = "--control" ] && CONTROL=1

TSV=reproduce/LINEAGE.tsv
T=$(mktemp -d)
# The control copy has to sit inside scripts/ because lineage.sh resolves the repo root from its own
# location -- run it from /tmp and it reconstructs from no git history at all, which would "pass" by
# being unable to look.
CTL=scripts/.lineage-control.sh
RESTORE=""
trap 'rm -f "$CTL"; [ -n "$RESTORE" ] && cp "$RESTORE" reproduce/METHOD_ID; rm -rf "$T"' EXIT INT TERM

SH=./scripts/lineage.sh
if [ "$CONTROL" = 1 ]; then
    # Put back the weaker check: is the file the right LENGTH. Rows can then be swapped freely.
    sed 's|if diff -u "$TSV" "$now" > "$now.diff" 2>&1; then|if [ "$(wc -l < "$TSV")" = "$(wc -l < "$now")" ]; then|' \
        scripts/lineage.sh > "$CTL"
    chmod +x "$CTL"; SH=./$CTL
    echo "CONTROL: the gate compares row COUNT instead of contents -- the checks below MUST fail"
fi

fails=0
check() { if [ "$1" = 1 ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi; }
run() { LINEAGE_TSV="$1" "$SH" --check >/dev/null 2>&1; }   # exit status only

# ---- the committed file is correct, or nothing below means anything --------------------------
run "$TSV"; check "$([ $? = 0 ] && echo 1)" "the committed lineage passes"

# ---- a row deleted ----------------------------------------------------------------------------
head -n -1 "$TSV" > "$T/dropped_last"
grep -v '^d1fc4065' "$TSV" > "$T/dropped_first"
sed '5d' "$TSV" > "$T/dropped_middle"
run "$T/dropped_last";   check "$([ $? != 0 ] && echo 1)" "the newest row deleted: refused"
run "$T/dropped_first";  check "$([ $? != 0 ] && echo 1)" "the OLDEST row deleted: refused (the one nobody would miss)"
run "$T/dropped_middle"; check "$([ $? != 0 ] && echo 1)" "a row deleted from the middle: refused"

# ---- a row rewritten, same row count ----------------------------------------------------------
# The id itself, then the toolchain pin beside it. A lineage that records the wrong risc0 version is
# worse than one that records none: it says the verifier is rebuildable when it is not.
sed 's/^3f52baff[0-9a-f]*/3f52baff0000000000000000000000000000000000000000000000000000000/' "$TSV" > "$T/mutated_id"
sed 's/\t3\.0\.5\t3\.0\.5/\t2.0.0\t2.0.0/' "$TSV" > "$T/mutated_ver"
awk 'NR==2{d=$0} NR==3{print d; next} {print}' "$TSV" > "$T/duplicated"
run "$T/mutated_id";  check "$([ $? != 0 ] && echo 1)" "an id rewritten in place: refused"
run "$T/mutated_ver"; check "$([ $? != 0 ] && echo 1)" "a risc0 version rewritten in place: refused"
run "$T/duplicated";  check "$([ $? != 0 ] && echo 1)" "a row overwritten by a copy of its neighbour: refused"

# ---- a stale view ------------------------------------------------------------------------------
# Distinct from "newest row deleted": here the file is internally consistent and simply does not
# reach the id the repo ships. Only the canonical cross-check catches it.
{ cat "$TSV"; } | head -n -1 > "$T/stale"
run "$T/stale"; check "$([ $? != 0 ] && echo 1)" "a view that stops short of the canonical id: refused"

# ---- an empty or missing file -------------------------------------------------------------------
: > "$T/empty"
run "$T/empty";     check "$([ $? != 0 ] && echo 1)" "an empty lineage: refused, not treated as nothing to check"
run "$T/no-such";   check "$([ $? != 0 ] && echo 1)" "a missing lineage: refused (not a pass by absence)"

# ---- the prose half ------------------------------------------------------------------------------
# A row can survive in the table while the section explaining it is swept out of reproduce/METHOD_ID.
# The table would still pass every check above and the reason that id ever moved would be gone.
# This is the one case that has to damage a REAL tracked file, because the reconstruction reads
# reproduce/METHOD_ID out of git and an override would test a path nothing runs. The restore is armed
# in the trap BEFORE the damage, so a crash here cannot leave the checkout edited.
cp reproduce/METHOD_ID "$T/METHOD_ID.bak"
RESTORE="$T/METHOD_ID.bak"
grep -v '1d6c3792' reproduce/METHOD_ID > "$T/stripped" && cp "$T/stripped" reproduce/METHOD_ID
run "$TSV"; rc=$?
cp "$RESTORE" reproduce/METHOD_ID; RESTORE=""
check "$([ $rc != 0 ] && echo 1)" "an id whose section was swept out of METHOD_ID: refused"

# ---- a shallow clone REFUSES rather than reporting 17 deletions ----------------------------------
# Built for real: a synthetic repo shaped like this one, cloned at depth 1, so the guard is exercised
# by an actually-shallow git and not by a stubbed answer.
(
  set -e
  mkdir -p "$T/src/reproduce" "$T/src/scripts"
  cd "$T/src"; git init -q .; git config user.email t@t; git config user.name t
  for i in 1 2 3; do
      printf '# synthetic\n%064d\n' "$i" > reproduce/METHOD_ID
      git add -A; git commit -qm "id $i"
  done
) >/dev/null 2>&1 || { echo "  FAIL could not build the synthetic repo"; fails=$((fails+1)); }
if git clone -q --depth 1 "file://$T/src" "$T/shallow" >/dev/null 2>&1; then
    mkdir -p "$T/shallow/scripts"
    cp scripts/lineage.sh "$T/shallow/scripts/"
    printf 'method_id\tbecame_canonical\tcommit\trisc0_zkvm\trzup_cargo_risczero\trzup_rust\trzup_cpp\n' \
        > "$T/shallow/reproduce/LINEAGE.tsv"
    out=$("$T/shallow/scripts/lineage.sh" --check 2>&1); rc=$?
    check "$([ $rc != 0 ] && echo 1)" "a shallow clone: the check refuses to run (exit $rc)"
    check "$(grep -qi 'shallow' <<<"$out" && echo 1)" "  ...and says SHALLOW, not 'rows were deleted'"
    check "$(grep -qi 'deleted\|removed' <<<"$out" || echo 1)" "  ...and reports no deletion it cannot know about"
else
    echo "  FAIL could not build a shallow clone to test the guard"; fails=$((fails+1))
fi

if [ "$CONTROL" = 1 ]; then echo "CONTROL: $fails failure(s)"; else echo "$fails failure(s)"; fi
if [ "$CONTROL" = 1 ]; then [ "$fails" != 0 ]; else [ "$fails" = 0 ]; fi

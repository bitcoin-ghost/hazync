#!/usr/bin/env bash
# The guest lineage: every id that has ever been the canonical METHOD_ID, in order, with the risc0
# toolchain each was built against.
#
# WHY THIS IS RECONSTRUCTED AND NOT TYPED. A receipt stays verifiable for as long as we keep its
# method id AND can rebuild the risc0 verifier that checked it, so what actually destroys a back
# catalogue is losing the record of which id was current when. reproduce/METHOD_ID carries that
# record as PROSE -- a chain of "Superseded the earlier id X..." lines -- and prose is not a store:
#
#   * the chain is incomplete. 7a8b29e0 was canonical for part of 2026-07-26 and no section
#     supersedes it, so reading the links alone loses it.
#   * nothing pins the risc0 version beside the id, and the id alone is not enough to rebuild a
#     verifier.
#   * a blind re-baseline sweep edits the current id and can take a superseded section with it.
#     check-versions.sh cannot see that: it validates ids that ARE mentioned, so a deleted row is
#     indistinguishable from an id that never existed.
#
# GIT HISTORY IS ALREADY AN APPEND-ONLY STORE, and it recorded this whether or not anyone wrote it
# down: every commit that changed reproduce/METHOD_ID's bare id line is one row. So the lineage is
# DERIVED, reproduce/LINEAGE.tsv is a materialised view of it, and the gate is "view == history".
# That is what makes it append-only in the only sense that matters -- deleting a row from the file
# does not delete it from the evidence, and the check fails.
#
#   scripts/lineage.sh            # print the reconstruction
#   scripts/lineage.sh --check    # compare it to reproduce/LINEAGE.tsv (the CI gate)
#   scripts/lineage.sh --write    # regenerate the file after a re-baseline
#
# ⛔ Needs FULL history: a shallow clone reconstructs a truncated lineage and every missing row
# reads as a deleted one. The check refuses to run rather than report a hole it invented.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# Overridable so scripts/test-lineage.sh can point the check at a deliberately damaged copy
# without touching the real one. The RECONSTRUCTION always comes from this repo's history.
TSV="${LINEAGE_TSV:-reproduce/LINEAGE.tsv}"
SRC=reproduce/METHOD_ID
MODE="${1:-print}"

die() { echo "FAIL $*" >&2; exit 1; }

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git checkout — the lineage lives in history"
[ "$(git rev-parse --is-shallow-repository)" = "false" ] \
    || die "shallow clone: the lineage cannot be reconstructed (need fetch-depth 0)"

# The canonical id is the one bare 64-hex line; everything else in the file is a comment. Reading it
# the same way check-versions.sh does keeps the two from disagreeing about what "canonical" means.
id_at() { git show "$1:$SRC" 2>/dev/null | grep -vE '^[[:space:]]*#' | grep -oE '^[0-9a-f]{64}$' | head -1; }

# Both spellings have been used: `rzup install --force <what> <ver>` and the later wrapper
# `rzup_install <what> <ver>`. A one-form grep silently returns nothing for half the history and
# would write "unrecorded" over rows whose pin is sitting right there.
pin_at() {  # $1 = commit, $2 = rzup component
    git show "$1:provision-vps.sh" 2>/dev/null \
      | grep -oE "(rzup install --force|rzup_install) $2 [0-9][0-9a-z.]*" \
      | grep -oE '[0-9][0-9a-z.]*$' | head -1
}
zkvm_at() {
    git show "$1:prover/methods/guest/Cargo.toml" 2>/dev/null \
      | grep -E '^risc0-zkvm' | grep -oE 'version = "=?[0-9][0-9a-z.]*"' \
      | grep -oE '[0-9][0-9a-z.]*' | head -1
}

reconstruct() {
    printf 'method_id\tbecame_canonical\tcommit\trisc0_zkvm\trzup_cargo_risczero\trzup_rust\trzup_cpp\n'
    local prev="" c id
    while read -r c; do
        id=$(id_at "$c")
        [ -n "$id" ] || continue          # before the file carried a bare id line
        [ "$id" = "$prev" ] && continue   # a commit that edited the prose, not the id
        prev=$id
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$id" \
            "$(git show -s --format=%ad --date=short "$c")" \
            "$(git rev-parse --short=9 "$c")" \
            "$(zkvm_at "$c" || true)" \
            "$(pin_at "$c" cargo-risczero || true)" \
            "$(pin_at "$c" rust || true)" \
            "$(pin_at "$c" cpp || true)"
    done < <(git log --format=%H --reverse --follow -- "$SRC") \
    | awk -F'\t' -v OFS='\t' '{ for (i=4;i<=7;i++) if ($i == "") $i = "unrecorded"; print }'
}

case "$MODE" in
    print) reconstruct ;;
    --write)
        reconstruct > "$TSV.tmp" && mv "$TSV.tmp" "$TSV"
        echo "wrote $TSV ($(($(wc -l < "$TSV") - 1)) rows)"
        ;;
    --check)
        [ -f "$TSV" ] || die "$TSV is missing — the lineage has no materialised view"
        fails=0
        now=$(mktemp); trap 'rm -f "$now" "$now.diff"' EXIT
        reconstruct > "$now"
        # 1. No row removed, no row rewritten. A diff, not a count: a row swapped for another keeps
        #    the count identical and is exactly the mutation this exists to catch.
        if diff -u "$TSV" "$now" > "$now.diff" 2>&1; then
            echo "  ok   lineage matches git history ($(($(wc -l < "$TSV") - 1)) rows, none removed or rewritten)"
        else
            echo "  FAIL $TSV disagrees with the lineage reconstructed from git history:"
            sed 's/^/       /' "$now.diff"
            echo "       A '-' line is a row that history still has and the file no longer does."
            echo "       Re-baselining? run scripts/lineage.sh --write, which only ever appends."
            fails=$((fails+1))
        fi
        # 2. The newest row must be what the repo currently ships, or the view is stale in the one
        #    place a reader would trust it most.
        canon=$(grep -vE '^[[:space:]]*#' "$SRC" | grep -oE '^[0-9a-f]{64}$' | head -1)
        last=$(tail -1 "$TSV" | cut -f1)
        if [ "$canon" = "$last" ]; then
            echo "  ok   the newest lineage row is the canonical id (${canon:0:8})"
        else
            echo "  FAIL newest lineage row is ${last:0:8} but $SRC says ${canon:0:8}"
            fails=$((fails+1))
        fi
        # 3. Every id must be rebuildable, which takes the toolchain and not just the id. A row
        #    reading "unrecorded" is a row whose verifier we could not reconstruct from the repo.
        un=$(awk -F'\t' 'NR>1 && /unrecorded/ {print "       " substr($1,1,8) " (" $2 ", " $3 ")"}' "$TSV")
        if [ -z "$un" ]; then
            echo "  ok   every lineage row pins the risc0 toolchain it was built against"
        else
            echo "  !    these rows predate a recorded toolchain pin, so the id alone is not enough"
            echo "       to rebuild their verifier. Recorded as unrecorded, not guessed:"
            echo "$un"
        fi
        # 4. The table says WHICH ids; reproduce/METHOD_ID says WHY each one moved. A row with no
        #    section left in the prose is a row whose reason for existing has been deleted, which is
        #    the half of the record a table cannot carry.
        orphan=$(awk -F'\t' 'NR>1 {print substr($1,1,8)}' "$TSV" | while read -r short; do
                     grep -q "$short" "$SRC" || echo "       $short"
                 done)
        if [ -z "$orphan" ]; then
            echo "  ok   every lineage row still has its section in $SRC"
        else
            echo "  FAIL these ids are in the lineage but no longer explained anywhere in $SRC:"
            echo "$orphan"
            fails=$((fails+1))
        fi
        [ "$fails" = 0 ]
        ;;
    *) die "usage: lineage.sh [--check|--write]" ;;
esac

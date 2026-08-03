#!/bin/bash
# Every crate the GUEST compiles is a METHOD_ID input, including the ones that do not live under
# prover/methods/guest/.
#
# WHY THIS EXISTS. reproduce/METHOD_ID carries a long, emphatic warning that any edit moving line
# numbers in "guest source" re-baselines the board — including pure comments. Everyone reads "guest
# source" as the guest DIRECTORY. It is not: the guest has path dependencies, and a crate whose tests
# run on the host and whose directory sits nowhere near the guest is compiled into the guest ELF all
# the same.
#
# It bit exactly that way. Adding `empty_root()` to coinbase-smt/src/lib.rs — a five-line helper for a
# host-side genesis pin, in a crate with its own test suite — moved the id from 4ea6567b… to
# 35cfbbed…. Nothing warned, because nothing under prover/methods/guest/ had changed. The author of
# such a change has no signal at all unless it is put where they are looking, which is the file they
# are editing.
#
# So this checks two things:
#   1. every path dependency of the guest carries the marker in its own source, where an editor sees it
#   2. reproduce/METHOD_ID names each one, so adding a guest dependency forces the note to be updated
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

GUEST_TOML=prover/methods/guest/Cargo.toml
CANON_DOC=reproduce/METHOD_ID
MARKER="GUEST-COMPILED CRATE"
bad=0
note(){ echo "  $*"; }
fail(){ echo "FAIL $*"; bad=1; }

[ -f "$GUEST_TOML" ] || { echo "FAIL $GUEST_TOML not found"; exit 1; }

# Path dependencies of the guest, resolved relative to the guest's own directory.
deps=$(grep -oE 'path = "[^"]+"' "$GUEST_TOML" | sed 's/path = "//; s/"$//')
[ -n "$deps" ] || fail "no path dependencies found in $GUEST_TOML — the parser is broken, not the tree"

count=0
for d in $deps; do
    dir=$(cd "prover/methods/guest/$d" 2>/dev/null && pwd) || { fail "guest dep path does not resolve: $d"; continue; }
    rel=${dir#"$(pwd)/"}
    count=$((count + 1))

    src=""
    for cand in "$dir/src/lib.rs" "$dir/src/main.rs"; do
        [ -f "$cand" ] && src="$cand" && break
    done
    if [ -z "$src" ]; then
        fail "$rel has no src/lib.rs or src/main.rs to carry the marker"
        continue
    fi
    if grep -q "$MARKER" "$src"; then
        note "ok   $rel carries the $MARKER marker"
    else
        fail "$rel is compiled into the guest but its source does not say so.
       Add a header noting '$MARKER — editing this file changes METHOD_ID'.
       Without it, someone editing this crate has NO signal that they are re-baselining the board."
    fi

    base=$(basename "$dir")
    if grep -q "$base" "$CANON_DOC"; then
        note "ok   $CANON_DOC names $base as a guest input"
    else
        fail "$CANON_DOC does not mention '$base', which the guest compiles.
       The canonical-id note must list every crate that can move the id."
    fi
done

note "checked $count guest path dependenc$([ "$count" = 1 ] && echo y || echo ies)"
[ "$bad" = 0 ] || { echo; echo "A crate the guest compiles is not flagged as a METHOD_ID input."; exit 1; }
echo "every guest-compiled crate is flagged as a METHOD_ID input."

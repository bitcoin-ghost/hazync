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
# A PATH DEPENDENCY PUTS AN ABSOLUTE PATH IN THE IMAGE ID (hazync#88).
#
# Cargo resolves path deps to absolute paths, and Rust embeds them in panic metadata, so the guest ELF
# carries e.g. "/repo/coinbase-smt/src/lib.rs" and the id changes with the checkout location. The same
# tree gave dfc9eeda at /hazync-zkvm and 7649f929 at /repo — which is how a release host was built that
# would have rejected every proof from its own guest.
#
# The guest's OWN sources are recorded relative, so shared code must be #[path]-included rather than
# depended on. That keeps one copy (no drift) AND keeps the path relative.
#
# This check fails on ANY path dependency. If one is genuinely needed, the fix is to include the file
# by path instead — not to relax this.
if [ -n "$(grep -oE 'path = "[^"]+"' "$GUEST_TOML")" ]; then
    fail "$GUEST_TOML declares a PATH DEPENDENCY. Cargo makes those absolute and the image id then
       depends on where the repo is checked out (#88). #[path]-include the source instead:
       $(grep -oE 'path = \"[^\"]+\"' "$GUEST_TOML" | tr '\n' ' ')"
fi

# Guest inputs are now #[path] INCLUDES, not Cargo path dependencies — see the note above. Resolve
# them relative to the file that declares them, which is how rustc resolves them too.
deps=$(grep -rhoE '#\[path = "[^"]+"\]' prover/methods/guest/src/*.rs 2>/dev/null \
       | sed 's/#\[path = "//; s/"\]//' | sed 's|^|src/|' | sort -u)
[ -n "$deps" ] || fail "no #[path] includes found in the guest — either the parser is broken or the
       shared SMT source is no longer compiled in, which would be a silent consensus change"

# A #[path] include reaches OUTSIDE the methods package, and Cargo's build-script change detection
# does not. Without coverage in prover/methods/build.rs, editing the SMT leaves the previously-built
# guest ELF in place: the build succeeds, and the id silently describes the OLD source. That failure is
# invisible, which is why it is gated here rather than left to reviewers.
#
# Two acceptable shapes: build.rs PARSES the includes (preferred — cannot go stale), or it lists each
# one literally. Anything else is unguarded.
BUILD_RS=prover/methods/build.rs
[ -f "$BUILD_RS" ] || fail "$BUILD_RS is missing — the guest cannot be rebuilt reproducibly"
if grep -q '#\[path' "$BUILD_RS"; then
    note "ok   $BUILD_RS derives its rerun-if-changed set from the guest's #[path] includes"
else
    for d in $deps; do
        base=$(basename "$d")
        grep -q "$base" "$BUILD_RS" || fail "$BUILD_RS does not watch '$base'.
       Cargo only re-runs a build script for changes INSIDE its own package, so editing that file
       would leave a stale guest ELF and a METHOD_ID describing source that is no longer there."
    done
fi

# Emitting any rerun-if-changed DISABLES Cargo's default "watch this whole package". So the moment
# build.rs names anything, it must also name what the default used to cover — otherwise this check
# would have fixed the outside-the-package hole by opening an inside-the-package one.
if grep -q 'rerun-if-changed' "$BUILD_RS"; then
    for restore in guest build.rs; do
        grep -q "rerun-if-changed=$restore" "$BUILD_RS" || fail "$BUILD_RS emits rerun-if-changed but
       never names '$restore'. That switches off Cargo's default package watch without replacing it,
       so edits to the guest itself would no longer trigger a rebuild."
    done
    note "ok   $BUILD_RS restores the package watch its own emissions disable"
fi

count=0
for d in $deps; do
    f=$(cd prover/methods/guest 2>/dev/null && readlink -f "$d" 2>/dev/null)
    [ -n "$f" ] && [ -f "$f" ] || { fail "guest #[path] include does not resolve: $d"; continue; }
    dir=$(cd "$(dirname "$f")/.." && pwd)
    rel=${dir#"$(pwd)/"}
    count=$((count + 1))

    src="$f"
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

note "checked $count guest #[path] include(s)"
[ "$bad" = 0 ] || { echo; echo "A crate the guest compiles is not flagged as a METHOD_ID input."; exit 1; }
echo "every guest-compiled crate is flagged as a METHOD_ID input."

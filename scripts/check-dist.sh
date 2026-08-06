#!/usr/bin/env bash
# Every artifact staged for a release must belong to the CURRENT guest.
#
# WHY THIS EXISTS. `dist/` is not cleaned between releases and `package-release.sh` writes only two of
# the six artifacts, so it accumulates binaries from several baselines. Staging the v0.15.0 release, it
# held a `hazync-verify-aarch64` from 31 July embedding NEITHER the current id nor the previous one —
# two re-baselines stale — alongside two host binaries from the v0.14.0 era. Uploading `dist/*` would
# have shipped a mix, and every one of them would have passed sha256 and PGP: those attest that the
# bytes are the bytes, not that the bytes are right.
#
# SIZE IS NOT THE SIGNAL, in either direction. Swapping one 64-hex literal for another is
# length-preserving, so a stale artifact can be byte-identical in size to a correct one. It can also
# differ, as the WASM did here (1,063,349 -> 1,065,791) because the guest itself changed. You cannot
# tell which case you are in, which is exactly why the embedded id is the only thing worth checking.
#
# NOR IS THE TIMESTAMP. The stale host quarantined from dist/ was dated 2 August — the day v0.14.0 was
# cut — but reports be5e0528, a guest from BEFORE that release. A recent mtime says a file was written
# recently, not that it was built from the current tree.
#
# TWO KINDS OF ARTIFACT, TWO CHECKS. The verifiers embed METHOD_ID as a &str literal, so it survives as
# ASCII and grep finds it. The HOST stores it as [u32; 8], so grep finds nothing in a host binary and a
# grep-based check would report a false failure — ask the binary instead. Learned by testing the
# published v0.14.0 host, which reports its release's canonical id but contains no such string.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

DIST="${1:-dist}"
CANON=$(grep -vE '^[[:space:]]*#' reproduce/METHOD_ID | grep -oE '[0-9a-f]{64}' | head -1)
CANON8=${CANON:0:8}
KNOWN=$(grep -oE '\b[0-9a-f]{8}\b' reproduce/METHOD_ID | sort -u)
fail=0; n=0
# Counted separately because "wrong guest" and "could not check here" are different findings, and the
# script runs under `set -u`, so these must exist before any branch increments them.
wrong_guest=0; unverified=0

[ -d "$DIST" ] || { echo "FAIL $DIST does not exist — nothing staged"; exit 1; }
echo "canonical: $CANON8…"

for f in "$DIST"/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f"); n=$((n + 1))
    case "$b" in
      *verify*)
        # Match the FULL 64-character id, not the 8-character prefix.
        #
        # Both verifier artifacts carry the whole id as ASCII — measured, not assumed — so the short
        # prefix was throwing away 56 characters of the check for nothing. This is the gate that
        # decides whether a verifier ships, and a prefix match would accept an artifact carrying a
        # DIFFERENT id that happened to share its first four bytes. Unlikely is not the standard to
        # hold a release gate to when the exact value is right there.
        #
        # KNOWN stays short: reproduce/METHOD_ID records superseded ids as 8-char prefixes, and that
        # list is only a diagnostic hint about WHICH stale guest an artifact came from.
        if grep -aq "$CANON" "$f"; then
            echo "  ok   $b embeds $CANON8"
        else
            stale=$(for k in $KNOWN; do grep -aq "$k" "$f" && echo "$k"; done | head -1)
            echo "FAIL $b does NOT embed the canonical id${stale:+ (it embeds $stale… — stale)}"
            echo "       Rebuild it. Do not ship it: it will reject every proof from this release."
            fail=1; wrong_guest=$((wrong_guest+1))
        fi ;;
      *host*)
        # Ask it. A host stores the id as [u32; 8]; grep would find nothing and report a false failure.
        #
        # THREE OUTCOMES, NOT TWO, and conflating the last two is a false alarm. A CUDA host cannot run
        # on a box without libcuda.so.1 — it exits before printing anything, which is NOT the same as
        # reporting a wrong id. The first version of this check said "reports <nothing>, not the
        # canonical id — do not ship it" about a perfectly good binary, which is the same defect class
        # this script exists to catch, one level up.
        err=$(timeout 120 "$f" method-id 2>&1 >/dev/null | head -1)
        got=$(timeout 120 "$f" method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1)
        if [ "$got" = "$CANON" ]; then
            echo "  ok   $b reports the canonical id"
        elif [ -z "$got" ] && printf '%s' "$err" | grep -q "error while loading shared libraries"; then
            # Unverifiable HERE. Still a failure — an artifact nobody has checked must not ship — but
            # the operator needs the real reason, which is "check it on a capable host", not "rebuild".
            # Keep the LIBRARY name. "${err##*: }" trimmed it to "No such file or directory",
            # which names the symptom and hides the cause.
            lib=$(printf '%s' "$err" | grep -oE '[a-z0-9_.]+[.]so[0-9.]*' | head -1)
            echo "FAIL $b cannot be verified on this machine (missing ${lib:-a shared library})"
            echo "       This is NOT evidence the artifact is wrong — it cannot run here at all."
            echo "       Verify on a capable host:  $b method-id   (want $CANON8...)"
            echo "       Then record it:            HAZYNC_ATTEST_${b//[^A-Za-z0-9]/_}=<id> $0"
            att="HAZYNC_ATTEST_${b//[^A-Za-z0-9]/_}"
            if [ "${!att:-}" = "$CANON" ]; then
                echo "  ok   $b attested canonical from a capable host (via $att)"
            else
                fail=1; unverified=$((unverified+1))
            fi
        else
            echo "FAIL $b reports '${got:-<nothing>}', not the canonical id — do not ship it"
            fail=1; wrong_guest=$((wrong_guest+1))
        fi ;;
      *) echo "  --   $b (guest-independent, not id-checked)" ;;
    esac
done

# Vacuity guard: an empty dist would otherwise exit 0 having checked nothing.
[ "$n" -gt 0 ] || { echo "FAIL $DIST is empty — nothing was checked"; exit 1; }
# Both outcomes block a publish — an artifact nobody has checked must not ship — but they are NOT the
# same finding, and saying so mattered: run standalone on a box with no CUDA, this printed "belongs to
# a different guest. Do NOT publish." directly under its own line saying "This is NOT evidence the
# artifact is wrong". A reader believes the summary, so the summary said the release was broken when
# the truth was that one binary cannot execute here. That is this repo's recurring failure — telling a
# correct setup it is wrong — and it is worse in a script a reviewer runs to decide whether to trust us.
if [ "${wrong_guest:-0}" -gt 0 ]; then
    echo; echo "A staged artifact belongs to a DIFFERENT GUEST. Do NOT publish."; exit 1
elif [ "${unverified:-0}" -gt 0 ]; then
    echo
    echo "${unverified} artifact(s) COULD NOT BE VERIFIED on this machine — not shown to be wrong."
    echo "Attest each from a capable host (command above), then re-run. Do NOT publish unattested."
    exit 1
elif [ "$fail" != 0 ]; then
    # Backstop. Splitting one flag into two categories means a branch can set `fail` and forget the
    # counter — which is exactly what happened while writing this, and the script then printed a FAIL
    # line and "all artifacts belong to the current guest" together, exiting 0. A gate that can report
    # a failure and still pass is worse than no gate.
    echo; echo "A staged artifact failed its check. Do NOT publish."; exit 1
fi
echo "all $n staged artifact(s) belong to the current guest."

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

[ -d "$DIST" ] || { echo "FAIL $DIST does not exist — nothing staged"; exit 1; }
echo "canonical: $CANON8…"

for f in "$DIST"/*; do
    [ -f "$f" ] || continue
    b=$(basename "$f"); n=$((n + 1))
    case "$b" in
      *verify*)
        if grep -aq "$CANON8" "$f"; then
            echo "  ok   $b embeds $CANON8"
        else
            stale=$(for k in $KNOWN; do grep -aq "$k" "$f" && echo "$k"; done | head -1)
            echo "FAIL $b does NOT embed the canonical id${stale:+ (it embeds $stale… — stale)}"
            echo "       Rebuild it. Do not ship it: it will reject every proof from this release."
            fail=1
        fi ;;
      *host*)
        # Ask it. A host stores the id as [u32; 8]; grep would find nothing and report a false failure.
        got=$(timeout 120 "$f" method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1)
        if [ "$got" = "$CANON" ]; then
            echo "  ok   $b reports the canonical id"
        else
            echo "FAIL $b reports '${got:-<nothing>}', not the canonical id — do not ship it"
            fail=1
        fi ;;
      *) echo "  --   $b (guest-independent, not id-checked)" ;;
    esac
done

# Vacuity guard: an empty dist would otherwise exit 0 having checked nothing.
[ "$n" -gt 0 ] || { echo "FAIL $DIST is empty — nothing was checked"; exit 1; }
[ "$fail" = 0 ] || { echo; echo "A staged artifact belongs to a different guest. Do NOT publish."; exit 1; }
echo "all $n staged artifact(s) belong to the current guest."

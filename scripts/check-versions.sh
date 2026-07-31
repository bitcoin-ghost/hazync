#!/usr/bin/env bash
# Fail if the repo disagrees with itself about which guest id or which release is current.
#
# Every one of these checks exists because the corresponding drift actually happened:
#
#   1. A doc kept naming a superseded METHOD_ID as the current one after a re-baseline. The id lives in
#      six places (reproduce/METHOD_ID, PROVING, SECURITY, ROADMAP, the release notes, the web page) and
#      nothing tied them together, so a bump could land in some and not others.
#   2. A doc named a stale "current release" (PROVING said v0.9.1 after v0.10.0 shipped).
#   3. A release link had its href bumped but not its anchor text: .../tag/v0.10.0">v0.9.0</a>. Reads
#      correctly, links correctly, and tells the reader the wrong version.
#   4. A typo'd or invented id that matches neither the canonical id nor any documented predecessor.
#
# Run locally before pushing docs; CI runs it on every push.
set -uo pipefail

CANON_FILE=reproduce/METHOD_ID
fail=0
note() { printf '  %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; fail=1; }

[ -f "$CANON_FILE" ] || { echo "FAIL $CANON_FILE missing"; exit 1; }

# The canonical id is the single non-comment, non-blank line.
CANON=$(grep -vE '^\s*(#|$)' "$CANON_FILE" | tr -d '[:space:]')
case "$CANON" in
    [0-9a-f]*) [ ${#CANON} -eq 64 ] || { echo "FAIL canonical id is ${#CANON} chars, expected 64"; exit 1; } ;;
    *) echo "FAIL could not parse a canonical id from $CANON_FILE"; exit 1 ;;
esac
CANON8=${CANON:0:8}
echo "canonical METHOD_ID: $CANON  (short $CANON8)"

# Superseded ids are the documented lineage inside the comments — legitimate to mention historically.
KNOWN=$(grep -oE '\b[0-9a-f]{8}\b' "$CANON_FILE" | sort -u)

# ── 1. the canonical id must appear in every doc that states it ────────────────────────────────────
for f in docs/PROVING.md SECURITY.md docs/ROADMAP.md; do
    [ -f "$f" ] || continue
    if grep -q "$CANON8" "$f"; then
        note "ok   $f references the canonical id"
    else
        bad "$f never mentions the canonical id $CANON8 — was it re-baselined without updating this doc?"
    fi
done

# ── 2. any token CLAIMED AS a guest id must be canonical or a documented predecessor ──────────────
# Scoped to lines that actually talk about the guest id. The repo is full of other 8-hex hashes — tip
# hashes, coin leaves, txids, commit shas — and an earlier, broader version of this check flagged all
# of them. Matching on the claim, not the shape, is what makes this signal instead of noise.
while IFS= read -r hit; do
    f=${hit%%:*}; rest=${hit#*:}; ln=${rest%%:*}; tok=${rest##*:}
    [ "$tok" = "$CANON8" ] && continue
    if ! grep -qx "$tok" <<<"$KNOWN"; then
        bad "$f:$ln claims guest id '$tok', which is neither canonical nor listed in $CANON_FILE"
    fi
done < <(git ls-files '*.md' | while read -r f; do
             grep -nE 'METHOD_ID|image id|guest id|canonical id|Superseded' "$f" 2>/dev/null \
             | while IFS= read -r line; do
                 n=${line%%:*}
                 grep -oE '`?\b[0-9a-f]{8}\b(…|\.\.\.)?`?|\b[0-9a-f]{64}\b' <<<"${line#*:}" \
                   | tr -d '`…' | sed 's/\.\.\.$//' | cut -c1-8 | sed "s|^|$f:$n:|"
               done
         done | sort -u)

# ── 3. release-link anchor text must match its own href ───────────────────────────────────────────
while IFS= read -r hit; do
    bad "$hit"
done < <(git ls-files '*.md' '*.html' | while read -r f; do
             grep -oE 'releases/tag/(v[0-9]+\.[0-9]+\.[0-9]+)"[^>]*>(v[0-9]+\.[0-9]+\.[0-9]+)<' "$f" 2>/dev/null \
             | while read -r m; do
                 href=$(sed -E 's|releases/tag/(v[^"]+)".*|\1|' <<<"$m")
                 text=$(sed -E 's|.*>(v[^<]+)<|\1|' <<<"$m")
                 [ "$href" = "$text" ] || echo "$f: release link points at $href but its text says $text"
               done
         done)

# ── 4. the documented "current release" must be the newest tag ────────────────────────────────────
# Skipped when tags are unavailable (shallow clone), rather than failing on missing data.
if git rev-parse --is-inside-work-tree >/dev/null 2>&1 && [ -n "$(git tag -l 'v*' 2>/dev/null)" ]; then
    NEWEST=$(git tag -l 'v*' | sort -V | tail -1)
    claimed=$(grep -ohE 'current release is \*\*(v[0-9]+\.[0-9]+\.[0-9]+)\*\*' docs/PROVING.md 2>/dev/null \
              | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)
    if [ -n "$claimed" ]; then
        # AHEAD is legitimate; BEHIND is the bug. Requiring exact equality made the check fail in both
        # directions, so there was no way to stage a release: updating the doc first failed (doc ahead
        # of tag), and tagging first failed (doc behind tag). The only passing state was doc and tag
        # landing in the same push, which is not how a reviewable change works.
        #
        # A doc naming a version NEWER than the newest tag is a release in preparation. A doc naming an
        # OLDER one is finding #2 above — PROVING said v0.9.1 after v0.10.0 shipped — and still fails.
        newer=$(printf '%s\n%s\n' "$claimed" "$NEWEST" | sort -V | tail -1)
        if [ "$claimed" = "$NEWEST" ]; then
            note "ok   docs/PROVING.md current release ($claimed) == newest tag"
        elif [ "$newer" = "$claimed" ]; then
            note "ok   docs/PROVING.md names $claimed, ahead of the newest tag $NEWEST (release in preparation)"
        else
            bad "docs/PROVING.md says the current release is $claimed but the newest tag is $NEWEST — stale"
        fi
    fi
else
    note "skip release-vs-tag check (no tags in this clone)"
fi

# The standalone verifier EMBEDS the guest image id as a literal — it deliberately does not depend on
# the `methods` crate, because that would drag in the guest build and defeat the point of a 1.6 MB
# artifact. That literal is invisible to the doc scan above, so without this a re-baseline would ship a
# verifier that silently rejects every current proof.
for V in verifier/src/lib.rs verifier-ffi/src/lib.rs; do
if [ -f "$V" ]; then
    emb=$(grep -oE 'METHOD_ID_HEX: &str = "[0-9a-f]{64}"' "$V" | grep -oE '[0-9a-f]{64}')
    if [ -z "$emb" ]; then
        bad "$V has no METHOD_ID_HEX literal to check"
    elif [ "$emb" != "$CANON" ]; then
        bad "$V embeds ${emb:0:8}… but canonical is ${CANON:0:8}… — it would reject every current proof"
    else
        note "ok   $V embeds the canonical id"
    fi
fi
done

if [ "$fail" -ne 0 ]; then
    echo
    echo "Version/id drift detected. If this was an intentional re-baseline, update reproduce/METHOD_ID"
    echo "FIRST (it is the source of truth), then the docs that state the current id and release."
    exit 1
fi
echo "versions consistent."

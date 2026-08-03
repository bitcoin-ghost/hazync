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

# ── 6. guest-dependent BINARIES must not be committed, and CI must verify the one it builds ───────
#
# This check used to grep a committed verifier/dist/hazync-verify-aarch64 for the canonical id, because
# that binary went stale at the be5e0528 re-baseline and shipped a verifier that rejected every current
# proof. The grep was right; keeping the binary in git was not (hazync#85). Refreshing it needs a cross
# toolchain no dev box has, so EVERY re-baseline stranded it — it went stale again at dfc9eeda.
#
# It is now built by the release workflow and attached as an asset. Which moves the risk rather than
# removing it, so this check moved too, and deliberately does NOT become "if the file exists": a check
# whose subject can vanish is a check that stops checking, and the file just vanished.
#
# Two assertions instead, neither of which can go quiet:
#   a) nothing executable is committed under verifier/dist/ — catches it being re-added
#   b) the release workflow still contains the embedded-id assertion — catches it being deleted
#
# Note the staleness is INVISIBLE to size: swapping one 64-hex literal for another is
# length-preserving, so a stale binary is byte-identical in size to a correct one.
found_bin=0
if [ -d verifier/dist ]; then
    while IFS= read -r b; do
        [ -n "$b" ] || continue
        bad "$b is a committed guest-dependent binary — build it in CI and attach it to the release (#85).
       Every re-baseline strands a committed binary, and its staleness is invisible to size."
        found_bin=1
    done < <(find verifier/dist -type f -exec sh -c 'file -b "$1" | grep -qi "ELF\|executable" && echo "$1"' _ {} \; 2>/dev/null)
fi
[ "$found_bin" = 0 ] && note "ok   no guest-dependent binary is committed under verifier/dist"

RELEASE_WF=.github/workflows/release-sign.yml
if [ -f "$RELEASE_WF" ]; then
    if grep -q "does not embed the canonical id" "$RELEASE_WF"; then
        note "ok   the release workflow asserts the built verifier embeds the canonical id"
    else
        bad "$RELEASE_WF no longer asserts the built verifier embeds the canonical id — that assertion is
       the ONLY thing standing between a re-baseline and publishing a verifier for a retired guest."
    fi
else
    bad "$RELEASE_WF is missing — nothing builds or checks the published verifier"
fi

# ── 7. a superseded id must not appear inside a fenced CODE BLOCK ─────────────────────────────────
# Check 2 asks "is this token a real id?" and a documented predecessor passes — correctly, because
# prose legitimately discusses lineage ("supersedes 3f52baff"). What it cannot see is a stale id
# presented as CURRENT EVIDENCE.
#
# That happened: GOALS.md — the document that says "measured, not asserted" — carried a G1 evidence
# block showing `hazync-verify` output citing guest 3f52baff, two re-baselines after it was retired.
# It read as a live verification of the current system and was a transcript of a dead one. Nothing
# flagged it, because 3f52baff is a documented predecessor.
#
# A fenced block is a claim that something was RUN. So: inside one, the only guest id allowed is the
# canonical one. Prose is untouched — discuss history freely there. Measured across the repo when
# added: zero legitimate occurrences, so this is enforceable rather than aspirational.
while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    f=${hit%%:*}; rest=${hit#*:}; ln=${rest%%:*}; tok=${rest##*:}
    bad "$f:$ln shows superseded id '$tok' inside a code block — evidence quoting a retired guest reads as current"
done < <(git ls-files '*.md' 2>/dev/null | while read -r f; do
             awk -v known="$KNOWN" -v canon="$CANON8" -v file="$f" '
                 /^[[:space:]]*```/ { infence = !infence; next }
                 infence {
                     while (match($0, /[0-9a-f]{8}/)) {
                         tok = substr($0, RSTART, 8); $0 = substr($0, RSTART + 8)
                         if (tok != canon && index(known, tok) > 0) print file ":" NR ":" tok
                     }
                 }' "$f"
         done)
[ "$fail" -eq 0 ] && note "ok   no superseded id is presented as evidence inside a code block"

# ── 8. a superseded id described as CURRENT, in prose ─────────────────────────────────────────────
#
# Check 7 only inspects fenced blocks, on the reasoning that prose should discuss history freely.
# That left a real gap: docs/PROVING.md carried "the CPU-only reproduce/Dockerfile attests the
# canonical id (`3f52baff`, the current guest)" through TWO re-baselines. Every gate passed — the id
# was outside a fence, and the file did mention the canonical id elsewhere, so checks 4 and 6 were
# satisfied. Found by an external auditor reading the page, which is the wrong way to find it.
#
# So: history is still free, but a retired id sitting next to a word that CLAIMS currency is not
# history. Narrow on purpose — "current|canonical|latest|now" within the same sentence — because a
# broader rule would flag the supersession chains that are supposed to name old ids.
prev_fail=$fail
while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    f=${hit%%:*}; rest=${hit#*:}; ln=${rest%%:*}; tok=${rest##*:}
    bad "$f:$ln calls superseded id '$tok' current/canonical in prose — it is history, say so"
done < <(git ls-files '*.md' 2>/dev/null | while read -r f; do
             awk -v known="$KNOWN" -v canon="$CANON8" -v file="$f" '
                 /^[[:space:]]*```/ { infence = !infence; next }
                 # A line that says "superseded" is self-labelling as history — "as of canonical id
                 # X (now superseded by Y)" is exactly the phrasing this repo should keep using, and
                 # flagging it would train people to delete the supersession chain to appease a gate.
                 !infence && /[Cc]urrent|[Cc]anonical|[Ll]atest/ && !/[Ss]upersed/ {
                     line = $0
                     while (match(line, /[0-9a-f]{8}/)) {
                         tok = substr(line, RSTART, 8); line = substr(line, RSTART + 8)
                         if (tok != canon && index(known, tok) > 0) print file ":" NR ":" tok
                     }
                 }' "$f"
         done)
[ "$fail" -eq "$prev_fail" ] && note "ok   no superseded id is described as current in prose"

if [ "$fail" -ne 0 ]; then
    echo
    echo "Version/id drift detected. If this was an intentional re-baseline, update reproduce/METHOD_ID"
    echo "FIRST (it is the source of truth), then the docs that state the current id and release."
    exit 1
fi
echo "versions consistent."

#!/usr/bin/env bash
# Build and publish the reproduce container's DEPENDENCY layer (hazync#146).
#
# WHY. A cold build fetches from five third parties -- apt, rustup, risczero.com and two git remotes
# -- and when any of them stalls the build sits at zero CPU with no output. Measured on three
# identical boxes started within seconds of each other, fetching the same pinned artefacts:
#
#     hz1  2m52s     hz2  2m51s     hz3  7m56s
#
# A 2.8x spread on identical work. The 40-minute stall in #146 is the tail of that distribution.
# Docker's layer cache already makes a WARM rebuild free, so caching buys nothing; what costs time is
# a machine that has never built, and every CI runner and every rented box is one.
#
# WHAT THIS DOES NOT DO, DELIBERATELY. CI keeps building from scratch. `reproducible-image-id` must
# never depend on an image we published, because the property that container exists to provide is
# "build it yourself from published sources and get the same id". A published deps image is a
# CONVENIENCE for people standing up a box; it is not an authority, and it is only valid if it
# produces the same guest id as building from scratch.
#
# THE TAG IS A CONTENT HASH of exactly what stage 1 reads. If any of those inputs change, the tag
# changes, so a stale image cannot be used by accident -- the pin simply will not resolve.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# HAZYNC_-prefixed on purpose: NAME, OWNER and REGISTRY are common environment variables, and a
# generic ${NAME:-...} silently picked up the operator's own $NAME -- producing
# ghcr.io/bitcoin-ghost/defenwycke instead of .../hazync-deps. A default that a stray env var can
# shadow is not a default.
REGISTRY="${HAZYNC_REGISTRY:-ghcr.io}"
OWNER="${HAZYNC_OWNER:-bitcoin-ghost}"
NAME="${HAZYNC_DEPS_NAME:-hazync-deps}"
PUSH="${HAZYNC_PUSH:-0}"

# Exactly the inputs stage 1 consumes: the pinned base digest, plus the three paths COPYed into it.
base=$(grep -oE 'ubuntu:[0-9.]+@sha256:[0-9a-f]{64}' reproduce/Dockerfile | head -1)
[ -n "$base" ] || { echo "no digest-pinned FROM in reproduce/Dockerfile"; exit 2; }
tag=$( { echo "$base"; cat provision-vps.sh; find patches coreshim -type f | sort | xargs cat; } \
       | sha256sum | cut -c1-16 )
ref="$REGISTRY/$OWNER/$NAME:$tag"

echo "base:  $base"
echo "tag:   $tag   (sha256 of the base digest + provision-vps.sh + patches/ + coreshim/)"
echo "ref:   $ref"

echo
echo "building the deps stage…"
# Tagged BOTH ways: a local name for verification, and the registry ref for pushing. Verification
# must use the LOCAL tag -- a bare sha256:... passed through --build-arg is resolved by buildkit as
# a REGISTRY reference (docker.io/library/sha256:...) and fails with "pull access denied", and the
# registry ref would be pulled rather than found locally because it has not been pushed yet.
local_tag="hazync-deps:$tag"
docker build --target deps -t "$local_tag" -t "$ref" -f reproduce/Dockerfile . || { echo "build failed"; exit 3; }
id=$(docker image inspect "$local_tag" --format '{{.Id}}')
echo "local image id: $id"
echo "local tag:      $local_tag"

# The point of the whole exercise: a build FROM this image must produce the canonical guest id. If it
# does not, the image is wrong and publishing it would hand people a broken shortcut.
echo
echo "verifying a full build from it yields the canonical guest id…"
docker build --build-arg DEPS_IMAGE="$local_tag" -t hazync-repro-verify:$tag -f reproduce/Dockerfile . \
  || { echo "build from the deps image failed"; exit 4; }
got=$(docker run --rm hazync-repro-verify:$tag 2>/dev/null | awk '/^METHOD_ID/{print $2}')
want=$(grep -oE '^[0-9a-f]{64}$' reproduce/METHOD_ID | head -1)
echo "  from deps image: ${got:-<none>}"
echo "  canonical pin:   ${want:-<none>}"
[ -n "$got" ] && [ "$got" = "$want" ] || { echo "MISMATCH — not publishing"; exit 5; }
echo "  match ✓"

if [ "$PUSH" != "1" ]; then
  echo
  echo "built and verified but NOT pushed (set HAZYNC_PUSH=1 to publish)."
  echo "to use it locally:"
  echo "  docker build --build-arg DEPS_IMAGE=$local_tag -f reproduce/Dockerfile ."
  exit 0
fi

echo
echo "pushing $ref"
docker push "$ref" || { echo "push failed — is the registry authenticated? (docker login $REGISTRY)"; exit 6; }
echo "published: $ref"
echo
echo "record this in reproduce/METHOD_ID or the runbook so others can pin it:"
echo "  docker build --build-arg DEPS_IMAGE=$ref -f reproduce/Dockerfile ."

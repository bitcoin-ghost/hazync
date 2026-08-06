#!/usr/bin/env bash
# Cut a Hazync release. ONE COMMAND, from a clean checkout to a signed, verified, `latest` release.
#
#   ./scripts/release.sh v0.17.0
#   ./scripts/release.sh v0.17.0 --dry-run     # preflight + report only, changes nothing
#
# WHY THIS EXISTS. Every piece of a release already existed as its own script; nothing drove them.
# The ORDER, and roughly a dozen traps, lived only in coordinator/deploy/RUNBOOK.md — so cutting a
# release meant reading a runbook correctly while tired, and each trap below is one that was actually
# walked into, in production, at least once:
#
#   * dist/ is not cleaned between releases, so it holds the LAST release's binaries
#   * a stale artifact can be BYTE-IDENTICAL IN SIZE to a correct one (the id is a fixed-width hex
#     literal), so size and mtime are both worthless as staleness signals
#   * release-sign.yml signs WHATEVER IS ATTACHED AT PUBLISH TIME and never picks up late additions
#   * publishing does NOT make a release `latest`, and older `gh` has no --latest at all
#   * the /latest/download/ CDN serves the PREVIOUS binary for minutes after the pointer is fixed
#   * a CUDA host cannot self-report its id on a box with no card, so it needs explicit attestation
#
# Every one of those is now a step below that fails loudly instead of a paragraph someone must recall.
#
# RESUMABLE. Each phase checks whether its work is already done and skips it. A failure at phase 5
# does not rebuild the guest; just run the same command again.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

TAG="${1:-}"
DRY=0
[ "${2:-}" = "--dry-run" ] && DRY=1

REPO_SLUG="${REPO_SLUG:-bitcoin-ghost/hazync}"
DIST="${DIST:-dist}"
# Every artifact that must carry the current guest id. The aarch64 verifier is NOT here: CI builds it
# from the tag and attaches it during signing (#85/#90), so it cannot exist before publish.
BINS="hazync-host-x86_64-linux-gnu hazync-host-x86_64-linux-gnu-cuda hazync-verify-x86_64-linux-gnu hazync-verify.wasm hazync-worker"

die()  { echo; echo "FAIL  $*" >&2; exit 1; }
ok()   { echo "  ok   $*"; }
step() { echo; echo "=== $* ==="; }
run()  { [ "$DRY" = 1 ] && { echo "  (dry-run, would run) $*"; return 0; }; "$@"; }

[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$ ]] \
    || die "usage: $0 vMAJOR.MINOR.PATCH [--dry-run]   (release-sign.yml refuses anything else)"

CANON=$(grep -vE '^[[:space:]]*#' reproduce/METHOD_ID | grep -oE '[0-9a-f]{64}' | head -1)
[ -n "$CANON" ] || die "no canonical id in reproduce/METHOD_ID"
echo "releasing $TAG at guest ${CANON:0:8}…"
[ "$DRY" = 1 ] && echo "(DRY RUN — nothing will be built, tagged, or published)"

# ---- 1. preflight ------------------------------------------------------------------------------
# All of these are cheap and all of them have bitten. Do them before a 60-minute build, not after.
step "1. preflight"

[ -z "$(git status --porcelain)" ] || die "working tree is dirty — commit or stash first.
       The container build mounts this tree LIVE and compiles whatever is in it at the moment each
       crate is built, so uncommitted changes end up in the binary with nothing recording it."
ok "working tree clean"

BR=$(git rev-parse --abbrev-ref HEAD)
HEAD_SHA=$(git rev-parse HEAD)
git fetch -q origin main 2>/dev/null
[ "$(git rev-parse origin/main)" = "$HEAD_SHA" ] \
    || die "HEAD ($BR, ${HEAD_SHA:0:8}) is not origin/main ($(git rev-parse --short origin/main)).
       Release from main: the signing workflow checks out THE TAG, and CI results you are trusting
       are for main."
ok "HEAD == origin/main (${HEAD_SHA:0:8})"

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists locally"
git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1 && die "tag $TAG already exists on origin"
ok "tag $TAG is free"

./scripts/check-versions.sh >/dev/null 2>&1 || { ./scripts/check-versions.sh; die "check-versions failed"; }
ok "check-versions: every documented id is canonical"

if command -v gh >/dev/null; then
    CONC=$(gh run list --limit 40 --json headSha,conclusion,name \
           --jq "[.[]|select(.headSha==\"$HEAD_SHA\")|select(.conclusion!=\"success\")]|length" 2>/dev/null || echo "?")
    if [ "$CONC" = "0" ]; then ok "CI green on ${HEAD_SHA:0:8}"
    elif [ "$CONC" = "?" ]; then echo "  --   could not read CI status (continuing)"
    else die "$CONC non-green CI run(s) on ${HEAD_SHA:0:8} — fix or re-run before releasing"; fi
fi

command -v docker >/dev/null || die "docker is required for the canonical container build"
FREE_GB=$(df -BG --output=avail . | tail -1 | tr -dc '0-9')
[ "${FREE_GB:-0}" -ge 25 ] || die "only ${FREE_GB}GB free; a CUDA build needs 25GB.
       It does NOT report as a disk error — nvcc segfaults (exit status: 139) and reads as a
       toolchain bug. Reclaim first: docker system prune -af"
ok "disk ${FREE_GB}GB free (>=25 needed for cuda)"

# ---- 2. build ----------------------------------------------------------------------------------
# Skipped per-artifact when the staged file already carries the canonical id, so a resumed run after
# a late failure does not rebuild the guest.
step "2. build host binaries (canonical container)"
mkdir -p "$DIST"

# The guest id is NECESSARY but NOT SUFFICIENT evidence that a staged host is current, and this was
# walked into: v0.18.0 changed only the HOST (a new dump-snapshot command) and the verifier FFI, so
# METHOD_ID did not move — and the previous release's binary answered `method-id` with the canonical
# id while containing none of the new code. It would have shipped v0.17.0 under a new tag.
#
# Every earlier release was a re-baseline, where a stale artifact failed the id check for free. That
# made the id look like a staleness signal. It is not one: it is a signal about the GUEST, and a host
# release can change everything else while leaving it untouched.
#
# So staleness is judged against the SOURCE an artifact was built from as well. HEAD is used rather
# than a hash of the host's own files: it over-rebuilds on a docs-only commit, which costs one
# container build a release, and it cannot MISS a change, which is the failure that matters. Phase 1
# already pins HEAD to origin/main, so this is a stable identity.
SRC_REV="$(git rev-parse HEAD 2>/dev/null)"

host_is_current() {  # $1 = path; $2 = asset name; hosts store the id as [u32;8], so ASK, do not grep
    [ -f "$1" ] || return 1
    [ -n "$SRC_REV" ] || return 1
    [ "$(cat "$DIST/.built-from-$2" 2>/dev/null)" = "$SRC_REV" ] || return 1
    [ "$("$1" method-id 2>/dev/null | grep -oE '[0-9a-f]{64}' | head -1)" = "$CANON" ]
}
for mode in cpu cuda; do
    asset="hazync-host-x86_64-linux-gnu"; [ "$mode" = cuda ] && asset="$asset-cuda"
    if host_is_current "$DIST/$asset" "$asset"; then
        ok "$asset already canonical and built from this source — skipping rebuild"
    else
        echo "  building $mode (guest + kernels; cuda is tens of minutes)…"
        # SKIP_GROTH16: a runtime rzup component the host does not link against — pure download cost.
        # RZUP_TIMEOUT: the default is far too short for the 488MB toolchain on a domestic link.
        run env SKIP_GROTH16=1 RZUP_TIMEOUT="${RZUP_TIMEOUT:-7200}" ${IMAGE:+IMAGE="$IMAGE"} \
            ./prover/build-release.sh "$mode" || die "$mode build failed"
        # Record what it was built from, so the next release can tell a fresh artifact from a stale
        # one that merely embeds the right guest. Written only after the build succeeds.
        [ "$DRY" = 1 ] || echo "$SRC_REV" > "$DIST/.built-from-$asset"
    fi
done

step "3. package worker, wasm and the x86_64 verifier"
run ./scripts/package-release.sh >/dev/null || die "package-release failed"
ok "worker + wasm packaged"
if ! grep -aq "${CANON:0:8}" "$DIST/hazync-verify-x86_64-linux-gnu" 2>/dev/null; then
    run cargo build -q --release --manifest-path verifier/Cargo.toml || die "verifier build failed"
    run cp verifier/target/release/hazync-verify "$DIST/hazync-verify-x86_64-linux-gnu"
fi
ok "x86_64 verifier staged"

# ---- 4. gate -----------------------------------------------------------------------------------
# The last point at which a wrong artifact is free to fix. After this it is public and signed.
step "4. verify every staged artifact belongs to guest ${CANON:0:8}"
for b in $BINS; do [ -f "$DIST/$b" ] || die "$DIST/$b is missing — nothing to publish"; done

# A CUDA host cannot run without libcuda.so.1. Rather than force a GPU box, read the id out of the
# bytes: the host stores it as [u32;8] LITTLE-ENDIAN, which puts the raw 32 bytes on disk in natural
# order. NB `grep -P '\x..'` silently finds nothing here — it is a check that cannot pass. Use python.
CU="$DIST/hazync-host-x86_64-linux-gnu-cuda"
if ! host_is_current "$CU" && command -v python3 >/dev/null; then
    if [ "$(python3 -c "import sys;print(open(sys.argv[1],'rb').read().count(bytes.fromhex(sys.argv[2])))" "$CU" "$CANON" 2>/dev/null)" = "1" ]; then
        ok "cuda host: cannot execute here, but embeds ${CANON:0:8} in its bytes"
        # Build the variable name EXACTLY as check-dist.sh does. `tr -c 'A-Za-z0-9' '_'` looks
        # equivalent and is not: it rewrites echo's trailing NEWLINE to an underscore too, yielding
        # HAZYNC_ATTEST_..._cuda_ — a variable check-dist.sh never reads, so the attestation would
        # be silently ignored and the gate would fail with the artifact sitting right there.
        _cu=hazync-host-x86_64-linux-gnu-cuda
        export "HAZYNC_ATTEST_${_cu//[^A-Za-z0-9]/_}=$CANON"
    fi
fi
./scripts/check-dist.sh "$DIST" || die "a staged artifact belongs to a different guest — do NOT publish"

if [ "$DRY" = 1 ]; then echo; echo "DRY RUN COMPLETE — preflight and artifacts are good for $TAG."; exit 0; fi

# ---- 5. publish --------------------------------------------------------------------------------
# ⚠ ORDER IS LOAD-BEARING: release-sign.yml signs whatever is attached WHEN THE RELEASE IS PUBLISHED
# and will not pick up anything added afterwards. All assets go up in the create call.
step "5. tag and publish"
NOTES="${NOTES:-}"
[ -n "$NOTES" ] && [ -f "$NOTES" ] || die "set NOTES=<file> to your release notes (a release without
       written notes is not worth cutting — say what moved and what it cost)"

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null \
    || git tag -a "$TAG" "$HEAD_SHA" -m "$TAG" || die "could not create tag"
git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1 \
    || git push origin "refs/tags/$TAG" || die "could not push tag"
ok "tag $TAG pushed"

if ! gh release view "$TAG" >/dev/null 2>&1; then
    echo "  uploading $(echo $BINS | wc -w) assets (~510MB; slow links take a while)…"
    ( cd "$DIST" && gh release create "$TAG" --title "$TAG" --notes-file "$(cd - >/dev/null; realpath "$NOTES")" $BINS ) \
        || die "release create failed. The tag exists, so fix and re-run: this step is resumable."
fi
ok "release published with all assets attached"

# ---- 6. post-publish verification --------------------------------------------------------------
# Everything below answers "what does a DOWNLOADER actually get", which is the only question that
# matters and the one that has been wrong before while every local check passed.
step "6. wait for signing"
for _ in $(seq 1 40); do
    S=$(gh run list --limit 8 --json name,status,conclusion \
        --jq '[.[]|select(.name=="Sign release")][0]|"\(.status)|\(.conclusion)"' 2>/dev/null)
    case "$S" in completed\|success) ok "Sign release completed"; break;;
                 completed\|*) die "Sign release FAILED ($S) — assets are public but unsigned";; esac
    sleep 30
done

step "7. the signed manifest must cover EVERY binary"
# A manifest covering only some artifacts is worse than none: it looks complete.
M=$(mktemp); A=$(mktemp)
curl -sL -o "$M" "https://github.com/$REPO_SLUG/releases/download/$TAG/SHA256SUMS.txt"
curl -sL -o "$A" "https://github.com/$REPO_SLUG/releases/download/$TAG/SHA256SUMS.txt.asc"
for b in $BINS hazync-verify-aarch64; do
    grep -q " $b\$" "$M" || die "$b is NOT in the signed manifest"
done
ok "manifest covers all $(( $(echo $BINS | wc -w) + 1 )) binaries"
gpg --verify "$A" "$M" 2>&1 | grep -q "Good signature" \
    && ok "PGP: good signature" \
    || echo "  --   could not verify PGP locally (is the maintainer key imported?)"

step "8. the latest pointer — checked TWICE, by id and not by size"
# Failure 1 (v0.15.0): a single PATCH setting draft+make_latest published but left `latest` on the
# PREVIOUS tag. Set it explicitly and then read it back rather than assuming it took.
gh api -X PATCH "repos/$REPO_SLUG/releases/$(gh api "repos/$REPO_SLUG/releases/tags/$TAG" --jq .id)" \
    -F make_latest=true >/dev/null 2>&1
GOT=$(gh api "repos/$REPO_SLUG/releases/latest" --jq .tag_name 2>/dev/null)
[ "$GOT" = "$TAG" ] || die "the latest pointer is '$GOT', not $TAG"
ok "API: /releases/latest resolves to $TAG"

# Failure 2 (v0.15.0): the CDN served the OLD binary for minutes AFTER the pointer was fixed. The
# VERSIONED url was correct throughout, so checking that proves nothing about what a user receives.
V=$(mktemp)
for i in $(seq 1 10); do
    curl -sL -H 'Cache-Control: no-cache' -o "$V" \
        "https://github.com/$REPO_SLUG/releases/latest/download/hazync-verify-x86_64-linux-gnu"
    if grep -aq "${CANON:0:8}" "$V"; then ok "CDN: /latest/download/ serves guest ${CANON:0:8}"; break; fi
    [ "$i" = 10 ] && die "after 10 tries /latest/download/ still does not serve ${CANON:0:8}.
       Size is NOT the signal here — a stale verifier is byte-identical in size to a correct one."
    echo "  (CDN still serving a stale copy; retrying in 30s — this took minutes on v0.15.0)"; sleep 30
done
rm -f "$M" "$A" "$V"

echo; echo "DONE — $TAG is published, signed, verified, and latest."
echo "https://github.com/$REPO_SLUG/releases/tag/$TAG"
echo
echo "NOT done automatically, because it needs a machine with the hardware:"
echo "  * smoke-test the CUDA host where a real GPU exists (method-id, regress, prove-block)"
echo "  * repoint any running provers at this guest — run-workers.sh only checks its id at STARTUP,"
echo "    so loops started before a re-baseline keep proving into rejections forever (#99)"

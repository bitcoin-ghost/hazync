#!/usr/bin/env bash
# Fail if the verifier SERVED TO THE PUBLIC disagrees with the canonical guest.
#
# Why this exists. On 2026-08-11 the browser verifier on bitcoinghost.org was two re-baselines behind:
# it reported guest b161735a while the coordinator served proofs under 4722cec8. Anyone following the
# README's "check it in your browser" invitation was told the live spine was
#
#     "not valid for guest b161735a — forged, tampered, corrupt, or produced by a different guest build"
#
# i.e. the site accused its own proof of being forged, on a project whose entire argument is "do not
# trust me, check it yourself". It had been that way for a week.
#
# Nothing caught it, for two reasons worth stating:
#
#   1. Every existing check tests an artifact we BUILD (CI's wasm parity, the release smoke tests).
#      None tests the artifact we SERVE. The release was correct throughout — v0.18.5 shipped a
#      4722cec8 wasm — so the build and release pipeline were never at fault. The web deploy was.
#      The web deploy is a separate manual step that re-baselining does not force.
#   2. The stale and correct modules are EXACTLY the same size: 1,065,791 bytes each, different
#      sha256. So file-exists, HTTP 200, content-length and "the page loads" all pass. The module
#      instantiates, exports are all present, and it answers every question cheerfully — with the
#      wrong id. Only calling method_id() or verifying a real proof catches it.
#
# So this check drives the deployed module the way a reader's browser does, and asserts the whole
# journey rather than any one file's hash. Run it after any web deploy and after every re-baseline.
#
# Env: HAZYNC_SITE (default https://bitcoinghost.org/hazync) to point at a staging host.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SITE="${HAZYNC_SITE:-https://bitcoinghost.org/hazync}"
CANON_FILE=reproduce/METHOD_ID
fail=0
note() { printf '  %s\n' "$*"; }
bad()  { printf 'FAIL %s\n' "$*"; fail=1; }

# A gate that quietly skips is a gate that cannot fail — the exact failure mode this script exists to
# catch. Missing tooling is an error, not a pass. ALLOW_SKIP=1 is the deliberate escape hatch.
for t in node curl; do
    command -v "$t" >/dev/null && continue
    if [ "${ALLOW_SKIP:-0}" = 1 ]; then echo "SKIP: $t not available (ALLOW_SKIP=1)"; exit 0; fi
    echo "FAIL $t not available — cannot check the deployed verifier. Set ALLOW_SKIP=1 to bypass."
    exit 1
done

[ -f "$CANON_FILE" ] || { echo "FAIL $CANON_FILE missing"; exit 1; }
CANON=$(grep -vE '^\s*(#|$)' "$CANON_FILE" | tr -d '[:space:]')
[ ${#CANON} -eq 64 ] || { echo "FAIL could not parse a 64-char canonical id from $CANON_FILE"; exit 1; }
note "canonical guest   ${CANON:0:8}  ($CANON_FILE)"

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
# Cache-bust: a CDN or proxy holding the previous module would make this check pass on a stale deploy.
CB="cb=$(date +%s)$$"

fetch() { curl -sfL --max-time 60 -H 'Cache-Control: no-cache' -o "$2" "$1?$CB"; }

fetch "$SITE/verify/hazync-verify.wasm" "$TMP/live.wasm" || { bad "cannot fetch the deployed wasm from $SITE"; exit 1; }
fetch "$SITE/verify/hazync-verify.js"   "$TMP/live.js"   || { bad "cannot fetch the deployed loader from $SITE"; exit 1; }
fetch "$SITE/api/spine/proof"           "$TMP/spine.bin" || { bad "cannot fetch the live spine proof from $SITE"; exit 1; }

# The loader is deployed separately from the wasm and is NOT in the signed release, so it drifts on its
# own axis. Compare it to the repo's copy, which is the source of truth.
if cmp -s "$TMP/live.js" verifier-wasm/hazync-verify.js; then
    note "ok   deployed loader is byte-identical to verifier-wasm/hazync-verify.js"
else
    bad "deployed hazync-verify.js differs from verifier-wasm/hazync-verify.js"
fi

# ── Sizes: build, release, deployment and the README must agree ──────────────────────────────────
#
# The README hardcodes the module's size and nothing asserted it, so it drifted — it claimed 1,063,570
# bytes long after the build, the release asset and the deployment had all moved to 1,065,791. Harmless
# by itself, but that README is what a reader consults to decide whether the file they downloaded is
# the file we published, and a stale figure there teaches people to ignore a mismatch.
#
# Size is a WEAK check on its own: the 2026-08-11 stale module was byte-for-byte the same length as the
# correct one. It is asserted here because it is nearly free and catches a different failure (a partial
# or wrong-artifact deploy), never as a substitute for the id and verdict checks below.
README=verifier-wasm/README.md
BUILD_WASM=verifier-wasm/target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm
RELEASE_URL=https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-verify.wasm

DEPLOYED_SZ=$(wc -c < "$TMP/live.wasm" | tr -d ' ')
README_SZ=$(grep -oE '^raw[[:space:]]+[0-9,]+' "$README" 2>/dev/null | head -1 | tr -cd '0-9')
RELEASE_SZ=$(curl -sIL --max-time 60 "$RELEASE_URL" \
    | awk 'tolower($1)=="content-length:" {n=$2} END {gsub(/\r/,"",n); print n}')
BUILD_SZ=""
[ -f "$BUILD_WASM" ] && BUILD_SZ=$(wc -c < "$BUILD_WASM" | tr -d ' ')

note "size deployed     ${DEPLOYED_SZ}"
note "size release      ${RELEASE_SZ:-unavailable}"
note "size local build  ${BUILD_SZ:-not built}"
note "size README       ${README_SZ:-unparsed}  ($README)"

if [ -z "$README_SZ" ]; then
    bad "could not parse a 'raw <n> bytes' figure from $README — the size table has moved or gone"
elif [ "$README_SZ" != "$DEPLOYED_SZ" ]; then
    bad "$README says raw $README_SZ bytes, the deployed module is $DEPLOYED_SZ — update the README"
else
    note "ok   README raw size matches the deployed module"
fi

if [ -z "$RELEASE_SZ" ]; then
    note "warn could not read a content-length for the release asset — size cross-check skipped"
elif [ "$RELEASE_SZ" != "$DEPLOYED_SZ" ]; then
    bad "release asset is $RELEASE_SZ bytes, the deployment is $DEPLOYED_SZ — the site is serving something the release did not ship"
else
    note "ok   deployed size matches the release asset"
fi

if [ -n "$BUILD_SZ" ] && [ "$BUILD_SZ" != "$DEPLOYED_SZ" ]; then
    note "warn local build is $BUILD_SZ bytes and the deployment is $DEPLOYED_SZ — your working tree differs from what is live (not a failure)"
fi

# Informational only. gzip output depends on the implementation and level, so asserting equality here
# would fail on somebody else's machine for no reason. The wire figure the README quotes is this one.
#
# Redirect rather than pass the path: `gzip -c FILE` writes the FILENAME into the gzip header, so the
# byte count changes with what the file is called (295,243 via a path, 295,219 via stdin, same bytes).
# A figure that moves when you rename the file is not a figure worth printing.
note "size gzipped      $(gzip -c < "$TMP/live.wasm" | wc -c | tr -d ' ')  (not asserted — implementation-dependent)"

# Perturb the proof body, not the header: a truncated or empty file is refused by parsing alone, which
# would let a verifier that accepts everything still pass this control.
python3 - "$TMP/spine.bin" "$TMP/bitflip.bin" <<'PY'
import sys
d = bytearray(open(sys.argv[1], 'rb').read())
d[-40] ^= 0x01
open(sys.argv[2], 'wb').write(d)
PY

cat > "$TMP/run.mjs" <<'EOF'
import fs from 'fs';
const { loadVerifier } = await import(process.argv[2]);
const v = await loadVerifier(fs.readFileSync(process.argv[3]));
const out = { method_id: v.methodId() };
for (const [k, p] of [['good', process.argv[4]], ['bitflip', process.argv[5]]]) {
    try { out[k] = v.verify(new Uint8Array(fs.readFileSync(p))); }
    catch (e) { out[k] = { status: 'threw', error: String(e && e.message) }; }
}
console.log(JSON.stringify(out));
EOF

OUT=$(node "$TMP/run.mjs" "$TMP/live.js" "$TMP/live.wasm" "$TMP/spine.bin" "$TMP/bitflip.bin" 2>"$TMP/err") \
    || { bad "the deployed module failed to run: $(tr -d '\n' < "$TMP/err" | tail -c 300)"; exit 1; }

jget() { printf '%s' "$OUT" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{const o=JSON.parse(s);console.log(process.argv[1].split(".").reduce((a,k)=>a&&a[k],o)??"")})' "$1"; }

LIVE_ID=$(jget method_id)
if [ "$LIVE_ID" = "$CANON" ]; then
    note "ok   deployed wasm method_id() == canonical (${CANON:0:8})"
else
    bad "deployed wasm reports ${LIVE_ID:0:8}, canonical is ${CANON:0:8} — the site is serving a stale verifier"
fi

# The coordinator is the other half: it must be minting proofs under the same guest the site can check.
META_ID=$(curl -sfL --max-time 30 "$SITE/api/meta" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{console.log(JSON.parse(s).method_id||"")}catch(e){console.log("")}})')
if [ "$META_ID" = "$CANON" ]; then
    note "ok   coordinator /api/meta method_id == canonical"
else
    bad "coordinator /api/meta reports ${META_ID:0:8}, canonical is ${CANON:0:8}"
fi

# The journey a reader actually takes: fetch the headline artifact, check it in the browser, see a
# verdict. This is the assertion that would have fired on 2026-08-11; the id comparison above is the
# diagnostic that says which side is wrong.
GOOD=$(jget good.status); ANCH=$(jget good.genesis_anchored); HI=$(jget good.height)
if [ "$GOOD" = verified ] && [ "$ANCH" = true ]; then
    note "ok   live spine verifies in the deployed module — genesis-anchored through block $HI"
else
    bad "live spine does NOT verify in the deployed module (status=$GOOD anchored=$ANCH) — this is what a reader sees"
fi

# Positive control. Without it a verifier that returned "verified" unconditionally would pass everything
# above, and this whole script would be theatre.
BF=$(jget bitflip.status)
if [ "$BF" = verified ]; then
    bad "the deployed module accepted a bit-flipped proof — it is not verifying anything"
else
    note "ok   bit-flipped proof rejected (status=$BF) — the check can fail"
fi

if [ "$fail" -ne 0 ]; then
    echo
    echo "The public verifier disagrees with this repo. Usually the fix is a deploy, not a rebuild:"
    echo "  1. confirm the release ships the canonical guest:"
    echo "       curl -sfL -o /tmp/v.wasm https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-verify.wasm"
    echo "       verify it against the signed SHA256SUMS.txt.asc before copying it anywhere"
    echo "  2. back up the live module under its OLD guest id, then install the new one:"
    echo "       sudo install -o www-data -g www-data -m 644 /tmp/v.wasm \\"
    echo "            /var/www/bitcoinghost/hazync/verify/hazync-verify.wasm"
    echo "  3. re-run this script — it checks over the wire, so it is the only proof the deploy landed"
    exit 1
fi
echo "deployed verifier consistent."

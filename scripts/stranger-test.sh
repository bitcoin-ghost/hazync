#!/usr/bin/env bash
# What a STRANGER actually gets. Runs the README's own commands verbatim, in a pristine container
# with no checkout, no caches, no keys and no local state — because every internal convenience is a
# way for the published surface to be broken without anyone noticing.
#
#   ./scripts/stranger-test.sh            # run in a container (default, and the point)
#   LOCAL=1 ./scripts/stranger-test.sh    # run here instead, for debugging the script itself
#
# ⛔ It asserts the OUTCOME, never that a command merely ran. A download that 404s still writes a
# file; an HTML error page is a perfectly good 6 KB of bytes. Every check below states what it
# expected and what it got.
set -uo pipefail
PASS=0; FAIL=0; SKIP=0
ok(){   printf '  ✅ %s\n' "$*"; PASS=$((PASS+1)); }
bad(){  printf '  ⛔ %s\n' "$*"; FAIL=$((FAIL+1)); }
skip(){ printf '  ⚠  %s\n' "$*"; SKIP=$((SKIP+1)); }
hdr(){  printf '\n=== %s ===\n' "$*"; }

REPO=${REPO:-bitcoin-ghost/hazync}
SITE=${SITE:-https://bitcoinghost.org}
REL="https://github.com/$REPO/releases/latest/download"

if [ "${LOCAL:-0}" != 1 ] && [ -z "${IN_CONTAINER:-}" ]; then
  command -v docker >/dev/null || { echo "docker required (or LOCAL=1)"; exit 2; }
  echo "running in a pristine ubuntu:22.04 container (no host state)"
  exec docker run --rm -i -e IN_CONTAINER=1 -e REPO="$REPO" -e SITE="$SITE" \
       -v "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/stranger-test.sh:/t.sh:ro" \
       ubuntu:22.04 bash -c 'apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq curl ca-certificates gnupg git jq >/dev/null 2>&1 && bash /t.sh'
fi

cd "$(mktemp -d)" || exit 1
echo "stranger workspace: $PWD"

hdr "1. The README's thirty-second path"
code=$(curl -fLsS -o hazync-verify -w '%{http_code}' "$REL/hazync-verify-x86_64-linux-gnu" 2>/dev/null)
if [ "$code" = 200 ] && [ -s hazync-verify ]; then
  ok "verifier downloaded ($(stat -c%s hazync-verify) bytes)"
  chmod +x hazync-verify
  file_out=$(head -c4 hazync-verify | od -c | head -1)
  case "$file_out" in *177*E*L*F*) ok "it is an ELF binary, not an error page";; *) bad "downloaded bytes are NOT an ELF: $file_out";; esac
else
  bad "verifier download failed (http=$code)"
fi

code=$(curl -fsS -o proof.bin -w '%{http_code}' "$SITE/hazync/api/spine/proof" 2>/dev/null)
if [ "$code" = 200 ] && [ -s proof.bin ]; then ok "proof downloaded ($(stat -c%s proof.bin) bytes)"
else bad "proof download failed (http=$code)"; fi

if [ -x hazync-verify ] && [ -s proof.bin ]; then
  out=$(./hazync-verify proof.bin 2>&1); rc=$?
  echo "$out" | sed 's/^/       /'
  if [ $rc -eq 0 ] && echo "$out" | grep -q "VERIFIED"; then
    ok "THE HEADLINE CLAIM HOLDS: a stranger verified the chain (exit 0)"
  else
    bad "verification FAILED (exit $rc) — this is the front-page promise"
  fi
fi

hdr "2. The released verifier and the live site agree on the guest"
# ⛔ Test for PRESENCE of the coordinator's id, do not "extract the id" from the binary. An earlier
# version did `grep -oa '[0-9a-f]{64}' | sort -u | head -1` and reported the BITCOIN GENESIS HASH
# (000000000019d668…), which sorts before every real id — it failed a binary that was perfectly good.
sid=$(curl -fsS "$SITE/hazync/api/meta" 2>/dev/null | grep -o '"method_id":[[:space:]]*"[0-9a-f]*"' | grep -o '[0-9a-f]\{64\}')
echo "       live coordinator: ${sid:0:16}…"
if [ -n "$sid" ] && grep -qa "$sid" hazync-verify 2>/dev/null; then
  ok "the released verifier embeds the coordinator's guest id — the download can verify the board"
else
  bad "the released verifier does NOT embed ${sid:0:16}… — a stranger's binary rejects the site's own proofs"
fi

hdr "3. Signed checksums (SECURITY.md tells them to check this)"
curl -fsSLo SHA256SUMS.txt "$REL/SHA256SUMS.txt" 2>/dev/null && ok "SHA256SUMS.txt fetched" || bad "no SHA256SUMS.txt"
curl -fsSLo SHA256SUMS.txt.asc "$REL/SHA256SUMS.txt.asc" 2>/dev/null && ok "signature fetched" || bad "no .asc signature"
if [ -s SHA256SUMS.txt ] && [ -s hazync-verify ]; then
  want=$(grep -F 'hazync-verify-x86_64-linux-gnu' SHA256SUMS.txt | awk '{print $1}' | head -1)
  got=$(sha256sum hazync-verify | awk '{print $1}')
  [ -n "$want" ] && [ "$want" = "$got" ] && ok "checksum of the downloaded verifier matches the manifest" \
    || bad "checksum MISMATCH (manifest ${want:0:16}… vs file ${got:0:16}…)"
fi
if [ -s SHA256SUMS.txt.asc ]; then
  # SECURITY.md prints the fingerprint in SPACED groups (777FE81F 8CC077FD …), so strip whitespace
  # before matching — a bare [0-9A-F]{40} finds nothing and reads as "no fingerprint published".
  SEC=$(curl -fsS "https://raw.githubusercontent.com/$REPO/main/SECURITY.md" 2>/dev/null)
  KEY=$(printf '%s' "$SEC" | grep -oE '([0-9A-F]{8}[ ]?){5}' | tr -d ' ' | head -1)
  if [ -n "$KEY" ]; then
    echo "       fingerprint SECURITY.md publishes: $KEY"
    # Import the way SECURITY.md actually tells a stranger to: from the ACCOUNT that publishes releases.
    if curl -fsS "https://github.com/defenwycke.gpg" 2>/dev/null | gpg --batch --import >/dev/null 2>&1; then
      ok "imported the key from github.com/defenwycke.gpg (the documented second source)"
      gpg --batch --fingerprint 2>/dev/null | tr -d ' ' | grep -q "$KEY" \
        && ok "imported key's fingerprint MATCHES the one SECURITY.md publishes" \
        || bad "imported key does NOT match the published fingerprint"
      gpg --batch --verify SHA256SUMS.txt.asc SHA256SUMS.txt >/dev/null 2>&1 \
        && ok "PGP signature VERIFIES — the release is authentic to that key" \
        || bad "PGP signature did NOT verify"
    else skip "github.com/defenwycke.gpg unreachable from here — signature not checked"; fi
  else bad "SECURITY.md publishes no fingerprint a stranger could use"; fi
fi

hdr "4. The browser path"
code=$(curl -fsSL -o /dev/null -w '%{http_code}' "$SITE/hazync/verify/" 2>/dev/null)
[ "$code" = 200 ] && ok "verify page loads (http 200)" || bad "verify page http=$code"
code=$(curl -fsSL -o w.wasm -w '%{http_code}' "$SITE/hazync/verify/hazync-verify.wasm" 2>/dev/null)
if [ "$code" = 200 ] && [ -s w.wasm ]; then
  ok "wasm served ($(stat -c%s w.wasm) bytes)"
  head -c4 w.wasm | grep -q "asm" && ok "it really is a wasm module" || bad "served bytes are not wasm"
  if [ -n "$sid" ] && grep -qa "$sid" w.wasm 2>/dev/null; then
    ok "the served wasm embeds the SAME guest id as the coordinator"
  else
    bad "the served wasm does NOT embed ${sid:0:16}… — the browser path rejects the site's own proofs"
  fi
else bad "wasm not served (http=$code)"; fi

hdr "5. The other URL the README offers"
code=$(curl -fsS -o one.bin -w '%{http_code}' "$SITE/hazync/api/proof/1" 2>/dev/null)
if [ "$code" = 200 ] && [ -s one.bin ]; then
  ok "/api/proof/<height> serves a single-block proof ($(stat -c%s one.bin) bytes)"
  [ -x ./hazync-verify ] && { ./hazync-verify one.bin >/dev/null 2>&1; r=$?; \
    [ $r -eq 0 ] || [ $r -eq 2 ] && ok "and the verifier gives a coherent verdict (exit $r)" || bad "verifier exit $r on it"; }
else bad "/api/proof/1 http=$code"; fi

hdr "6. Every link the README hands them"
curl -fsSL -o README.md "https://raw.githubusercontent.com/$REPO/main/README.md" 2>/dev/null || bad "cannot fetch README"
if [ -s README.md ]; then
  n=0; b=0
  while read -r u; do
    n=$((n+1)); c=$(curl -fsSL -o /dev/null -w '%{http_code}' --max-time 25 "$u" 2>/dev/null)
    case "$c" in 200|301|302) ;; *) echo "       ⛔ $c  $u"; b=$((b+1));; esac
  done < <(grep -oE 'https://[a-zA-Z0-9./_#-]+' README.md | grep -vE 'shields\.io|badge|example\.com' | sort -u | head -25)
  [ "$b" -eq 0 ] && ok "all $n README links resolve" || bad "$b of $n README links are broken"
fi

hdr "RESULT"
printf '  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" -eq 0 ] || echo "  ⛔ a stranger would hit $FAIL problem(s)"
exit $(( FAIL > 0 ? 1 : 0 ))

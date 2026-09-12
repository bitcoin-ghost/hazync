#!/usr/bin/env bash
# release.sh step 4's CUDA attestation (#275).
#
# The bug: the attestation sat under `! host_is_current "$CU" …`, which is TRUE for exactly the
# artifact that needs it. A freshly built CUDA host matches .built-from AND embeds the canonical id,
# so host_is_current reported "current", the export was skipped, and check-dist.sh refused a good
# binary with "belongs to a different guest". Both v0.21.1 and v0.21.2 shipped only because the
# operator passed the attestation by hand.
#
# Two surfaces, because the bug had two halves -- a helper that gave the right answer, and a caller
# that never asked it:
#   1. scripts/embeds-method-id.sh: the byte check itself, incl. the cases it must REFUSE.
#   2. release.sh's step 4: the CONDITION. Asserted structurally, since running step 4 for real needs
#      a git preflight, a green CI run and a container build.
#
#   scripts/test-release-attest.sh            # must PASS
#   scripts/test-release-attest.sh --control  # restores the buggy gate in a COPY; must FAIL
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
CONTROL=0; [ "${1:-}" = "--control" ] && CONTROL=1
CANON=$(grep -vE '^[[:space:]]*#' reproduce/METHOD_ID | grep -oE '[0-9a-f]{64}' | head -1)
[ -n "$CANON" ] || { echo "FAIL no canonical id in reproduce/METHOD_ID"; exit 1; }
OTHER=$(printf '%064d' 7)
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
fails=0
check() { if [ "$1" = 1 ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi; }
hexfile() { python3 -c "import sys;open(sys.argv[1],'wb').write(bytes.fromhex(sys.argv[2]))" "$1" "$2"; }

# ---- 1. the byte check ------------------------------------------------------------------------
hexfile "$T/good" "deadbeef${CANON}cafe"
hexfile "$T/twice" "${CANON}00${CANON}"
hexfile "$T/wrong" "deadbeef${OTHER}cafe"
: > "$T/empty"
./scripts/embeds-method-id.sh "$T/good" "$CANON";  check "$([ $? = 0 ] && echo 1)" "a binary carrying the id once: accepted"
./scripts/embeds-method-id.sh "$T/wrong" "$CANON"; check "$([ $? = 1 ] && echo 1)" "a binary carrying a DIFFERENT id: refused"
./scripts/embeds-method-id.sh "$T/empty" "$CANON"; check "$([ $? = 1 ] && echo 1)" "a binary carrying no id at all: refused"
./scripts/embeds-method-id.sh "$T/twice" "$CANON"; check "$([ $? = 1 ] && echo 1)" "the id TWICE is refused, not rounded up to yes"
./scripts/embeds-method-id.sh "$T/nope" "$CANON";  check "$([ $? = 1 ] && echo 1)" "a missing file: refused (not a pass by absence)"
./scripts/embeds-method-id.sh "$T/good" "not-hex"; check "$([ $? = 1 ] && echo 1)" "a malformed id: refused"

# ---- 2. the condition in release.sh ------------------------------------------------------------
R="$T/release.sh"; cp scripts/release.sh "$R"
if [ "$CONTROL" = 1 ]; then
    # Put the bug back, as it stood before #275.
    python3 - "$R" <<'PY'
import sys
p=sys.argv[1]; s=open(p).read()
s=s.replace('if timeout 120 "$CU" method-id >/dev/null 2>&1; then',
            'if ! host_is_current "$CU" hazync-host-x86_64-linux-gnu-cuda; then')
open(p,'w').write(s)
PY
    echo "CONTROL: step 4 gated on staleness again -- the checks below MUST fail"
fi
step4=$(sed -n '/^step "4\./,/^\.\/scripts\/check-dist\.sh/p' "$R")
# Comments are stripped before asking what the step DOES: the fix's own comment explains the bug it
# removed, and a grep for the word would find the explanation and call it the bug.
step4_code=$(printf '%s\n' "$step4" | grep -vE '^[[:space:]]*#')
check "$(printf '%s' "$step4" | grep -q 'timeout 120 "\$CU" method-id' && echo 1)" \
      "step 4 asks whether the binary CAN RUN here, under a timeout"
check "$(printf '%s' "$step4_code" | grep -q 'host_is_current' || echo 1)" \
      "  ...and not whether it is stale (the branch that never ran)"
check "$(printf '%s' "$step4" | grep -q 'embeds-method-id.sh' && echo 1)" \
      "step 4 uses the shared byte check rather than a pasted copy"
dup=$(grep -c 'count(bytes.fromhex' "$R"); check "$([ "$dup" = 0 ] && echo 1)" \
      "the python one-liner is not duplicated back into release.sh (found $dup)"
check "$(printf '%s' "$step4" | grep -q 'never overwrite it\|using the attestation you supplied' && echo 1)" \
      "an operator-supplied attestation is preferred over the byte check"
check "$(printf '%s' "$step4" | grep -q 'does not carry .* in its bytes exactly once' && echo 1)" \
      "an unreadable artifact gets its own message, not 'different guest'"

# The variable NAME is load-bearing and silent when wrong: check-dist.sh simply never reads
# HAZYNC_ATTEST_..._cuda_ and the gate fails with the artifact sitting right there. Evaluate the
# expansion release.sh actually uses and compare it to the name check-dist.sh computes.
_cu=hazync-host-x86_64-linux-gnu-cuda
built="HAZYNC_ATTEST_${_cu//[^A-Za-z0-9]/_}"
want=$(b=hazync-host-x86_64-linux-gnu-cuda; echo "HAZYNC_ATTEST_${b//[^A-Za-z0-9]/_}")
check "$([ "$built" = "$want" ] && [ "$built" = "HAZYNC_ATTEST_hazync_host_x86_64_linux_gnu_cuda" ] && echo 1)" \
      "the exported name is the one check-dist.sh reads ($built)"

# End to end: the branch exports, check-dist.sh accepts, and the release proceeds -- with a fake host
# that fails to load exactly as a real CUDA host does on a GPU-less box.
cat > "$T/dist_cu" <<'FAKE'
#!/bin/sh
echo "error while loading shared libraries: libcuda.so.1: cannot open shared object file" >&2
exit 127
FAKE
mkdir -p "$T/dist" && cp "$T/dist_cu" "$T/dist/hazync-host-x86_64-linux-gnu-cuda"
chmod +x "$T/dist/hazync-host-x86_64-linux-gnu-cuda"
env "$built=$CANON" ./scripts/check-dist.sh "$T/dist" >/dev/null 2>&1; rc=$?
check "$([ $rc = 0 ] && echo 1)" "  ...and check-dist.sh then PASSES the un-runnable host (exit $rc)"

if [ "$CONTROL" = 1 ]; then echo "CONTROL: $fails failure(s)"; else echo "$fails failure(s)"; fi
if [ "$CONTROL" = 1 ]; then [ "$fails" != 0 ]; else [ "$fails" = 0 ]; fi

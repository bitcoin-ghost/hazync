#!/usr/bin/env bash
# check-dist.sh's CUDA-host attestation path (#248). A host that cannot execute here (no
# libcuda.so.1) must PASS when attested with the canonical id and print NO FAIL line for it -- it used
# to print the FAIL block and then accept the attestation on the next line -- and must still FAIL,
# exit 1, when unattested or attested with the wrong id.
#
# The fake artifact reproduces the real dynamic-loader failure: stderr names the missing library,
# exit 127, no id on stdout.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
CANON=$(grep -vE '^[[:space:]]*#' reproduce/METHOD_ID | grep -oE '[0-9a-f]{64}' | head -1)
[ -n "$CANON" ] || { echo "FAIL no canonical id in reproduce/METHOD_ID"; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/dist"
cat > "$T/dist/hazync-host-x86_64-linux-gnu-cuda" <<'EOF'
#!/bin/sh
echo "hazync-host-x86_64-linux-gnu-cuda: error while loading shared libraries: libcuda.so.1: cannot open shared object file: No such file or directory" >&2
exit 127
EOF
chmod +x "$T/dist/hazync-host-x86_64-linux-gnu-cuda"
ATT=HAZYNC_ATTEST_hazync_host_x86_64_linux_gnu_cuda
fails=0
check() { if [ "$1" = 1 ]; then echo "  ok   $2"; else echo "  FAIL $2"; fails=$((fails+1)); fi; }

out=$(env "$ATT=$CANON" ./scripts/check-dist.sh "$T/dist" 2>&1); rc=$?
check "$([ $rc = 0 ] && echo 1)" "attested canonical: exit 0 (got $rc)"
check "$(printf '%s\n' "$out" | grep -q '^FAIL' || echo 1)" "attested canonical: no FAIL line in the output"
check "$(printf '%s\n' "$out" | grep -q 'attested canonical from a capable host.*libcuda.so.1' && echo 1)" \
      "attested canonical: says it was attested AND why it could not run here"

out=$(env -u "$ATT" ./scripts/check-dist.sh "$T/dist" 2>&1); rc=$?
check "$([ $rc = 1 ] && echo 1)" "unattested: exit 1 (got $rc)"
check "$(printf '%s\n' "$out" | grep -q '^FAIL .*cannot be verified on this machine (missing libcuda.so.1)' && echo 1)" \
      "unattested: FAIL names the missing library"

out=$(env "$ATT=$(printf '%064d' 0)" ./scripts/check-dist.sh "$T/dist" 2>&1); rc=$?
check "$([ $rc = 1 ] && echo 1)" "attested with the WRONG id: still exit 1 (got $rc)"

echo "$fails failure(s)"
[ "$fails" = 0 ]

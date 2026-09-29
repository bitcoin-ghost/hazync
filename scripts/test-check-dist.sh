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
# ⚠ THE WORDING CHANGED ON PURPOSE (hazync#579). This used to pin "attested canonical from a capable
# host", which was printed for BOTH the operator path and release.sh's byte-check fallback -- so a
# release cut on a GPU-less box always took the weaker path while the log claimed the stronger one.
# The two now print different sentences, and this test covers both rather than only the one.
check "$(printf '%s\n' "$out" | grep -q 'attested canonical by an operator who ran it.*libcuda.so.1' && echo 1)" \
      "attested by an OPERATOR: says who attested it AND why it could not run here"
check "$(printf '%s\n' "$out" | grep -q 'IN ITS BYTES' || echo 1)" \
      "⛔ and does NOT claim a byte check when an operator supplied the id"

# ⛔ THE CASE #579 EXISTS FOR: release.sh sets the attestation ITSELF from a byte check, and marks the
# provenance. That must read as a byte check, and must warn, and must not claim anyone ran anything.
# ⚠ BOTH variables, as release.sh sets them: the id AND its provenance. Setting only the provenance
# leaves no attestation at all, so check-dist correctly takes the unattested FAIL path — which is what
# my first version of this case did, and it looked like a code bug rather than a test bug.
out=$(env "$ATT=$CANON" "${ATT}_SOURCE=bytes" ./scripts/check-dist.sh "$T/dist" 2>&1); rc=$?
check "$([ $rc = 0 ] && echo 1)" "byte-sourced: still exit 0 (got $rc)"
check "$(printf '%s\n' "$out" | grep -q 'IN ITS BYTES' && echo 1)" \
      "byte-sourced: says the id was read from the BYTES"
check "$(printf '%s\n' "$out" | grep -q 'NOT a run' && echo 1)" \
      "byte-sourced: warns it is not a run and says how to gate on one"
check "$(printf '%s\n' "$out" | grep -q 'capable host\|operator who ran it' || echo 1)" \
      "⛔⛔ byte-sourced: does NOT claim a capable host or an operator — the whole point of #579"

out=$(env -u "$ATT" ./scripts/check-dist.sh "$T/dist" 2>&1); rc=$?
check "$([ $rc = 1 ] && echo 1)" "unattested: exit 1 (got $rc)"
check "$(printf '%s\n' "$out" | grep -q '^FAIL .*cannot be verified on this machine (missing libcuda.so.1)' && echo 1)" \
      "unattested: FAIL names the missing library"

out=$(env "$ATT=$(printf '%064d' 0)" ./scripts/check-dist.sh "$T/dist" 2>&1); rc=$?
check "$([ $rc = 1 ] && echo 1)" "attested with the WRONG id: still exit 1 (got $rc)"

echo "$fails failure(s)"
[ "$fails" = 0 ]

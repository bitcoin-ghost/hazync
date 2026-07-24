#!/usr/bin/env bash
# Assert every zkVM prove path carries the risc0 segment-boundary guard.
#
# risc0-circuit-rv32im 4.0.5 has a preflight bug: for ~10% of workloads a proving segment packs right up
# to its 2^po2 boundary and the assertion `cycles <= 1 << segment.po2` overflows, so the prove PANICS —
# on CPU AND cuda (it is the shared witgen, not backend-specific). The host works around it by setting
# `segment_limit_po2(seg_po2())` on the ExecutorEnv (env HAZYNC_SEG_PO2) and retrying with progressively
# smaller segments. Because the bug is in the shared witgen it hits EVERY prove path — a new prove path
# added without the guard silently reintroduces the panic on ~10% of blocks. This gate has already caught
# the miss twice (only prove_with_opts guarded, then the plain .prove() composite paths missed).
#
# It fails if any `default_prover().prove(... METHOD_ELF)` / `.prove_with_opts(... METHOD_ELF)` call is
# not preceded, within 25 lines, by a `segment_limit_po2(seg_po2())` on its builder. `forest.prove(pos)`
# (Utreexo accumulator proofs) carry no METHOD_ELF and are correctly ignored.
set -euo pipefail
f="$(dirname "$0")/host/src/main.rs"
bad=$(awk '
  /segment_limit_po2\(seg_po2\(\)\)/         { seg = NR }
  /(prove|prove_with_opts)\(.*METHOD_ELF/    { if (!seg || NR - seg > 25) printf "  line %d:%s\n", NR, $0 }
' "$f")
if [ -n "$bad" ]; then
  echo "ERROR: zkVM prove path(s) missing a segment_limit_po2(seg_po2()) guard:"
  echo "$bad"
  echo "Fix: add 'b.segment_limit_po2(seg_po2());' right after the ExecutorEnv::builder() for each."
  exit 1
fi
n=$(grep -cE '(prove|prove_with_opts)\(.*METHOD_ELF' "$f")
echo "OK: all $n zkVM prove paths carry a segment_limit_po2(seg_po2()) guard."

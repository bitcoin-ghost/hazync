#!/bin/bash
# Negative tests for the coverage-audit fixes (COV-1 time-too-old, COV-2 merkle CVE-2012-2459):
# prove the checks REJECT the malicious cases, not just that valid blocks still pass.
export PATH="$HOME/.risc0/bin:$HOME/.cargo/bin:$PATH" RISC0_HOME="$HOME/.risc0"
H=${HAZYNC_HOST:-./target/release/host}
BLK=${HAZYNC_BLOCK170:-block_170.json}   # any build_full-format block JSON works for COV-1

echo "=== COV-2: merkle mutation (CVE-2012-2459) ==="
echo "Honest [A,B,C] and malleated [A,B,C,C] (last tx duplicated) share the SAME merkle root, but Core's"
echo "ComputeMerkleRoot flags the malleated list mutated=1, and merkle_ok requires mutated==0 -> rejected."
RUST_LOG=error $H test-merkle 2>/dev/null

echo
echo "=== COV-1: time-too-old (block timestamp must exceed MTP of the previous 11 blocks) ==="
echo "--- control (no knob): VALID ---"
HAZYNC_BLOCK=$BLK RUST_LOG=error $H check-full 2>&1 | sed -n '/VALID\|INVALID/p' | tail -1
echo "--- attack (HAZYNC_COV1_BADTIME=1 makes MTP(prev)==this block's time): rejected on time_ok ---"
HAZYNC_COV1_BADTIME=1 HAZYNC_BLOCK=$BLK RUST_LOG=error $H check-full 2>&1 | sed -n '/time_ok=false/p' | tail -1

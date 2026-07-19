#!/usr/bin/env bash
# Continuously-enforced consensus REJECT-path tests for CI.
#
# `host adversarial` already covers holes #1 (height), #3 (double-spend/ordering), #4 (coinbase),
# #5 (unbound prevouts), and `host check-bip30` the BIP30 grandfather case. This script adds the
# coverage-audit reject-paths so a FUTURE guest change can't silently regress one without CI catching it
# — the durability guarantee for a "trust the checks, not us" system. Each asserts the malicious input is
# REJECTED and an honest baseline ACCEPTED.
#
# Run from prover/:  HAZYNC_HOST=./target/release/host ./ci_negative_tests.sh
set -uo pipefail
H="${HAZYNC_HOST:-./target/release/host}"
BLK="${HAZYNC_COV1_BLOCK:-block_130000.json}"   # any check-full block; COV-1 hook overrides its MTP window
fail(){ echo "FAIL: $*" >&2; exit 1; }
pass(){ echo "ok: $*"; }

command -v "$H" >/dev/null 2>&1 || [ -x "$H" ] || fail "host binary not found at $H"

# --- COV-2: merkle mutation (CVE-2012-2459) ---------------------------------------------------------
# Honest [A,B,C] and malleated [A,B,C,C] share the SAME root, but Core's ComputeMerkleRoot flags the
# malleated list mutated=1 and merkle_ok requires mutated==0 → the malleated block is rejected.
out="$("$H" test-merkle 2>/dev/null)" || fail "test-merkle crashed"
echo "$out" | grep -Eq 'mutated \[A,B,C,C\] : .*mutated=1' || fail "COV-2: malleated block not flagged mutated=1"
echo "$out" | grep -q  'SAME root (CVE collision): true'   || fail "COV-2: CVE-2012-2459 collision not reproduced"
echo "$out" | grep -Eq 'normal  \[A,B,C\]   : .*mutated=0' || fail "COV-2: honest block wrongly flagged mutated"
pass "COV-2 merkle mutation rejected (mutated=1); honest accepted (mutated=0)"

# --- COV-1: time-too-old (block time must exceed MTP of the previous 11 blocks) ---------------------
# check-full uses .expect(), so a rejected block exits non-zero. Honest baseline must validate.
HAZYNC_BLOCK="$BLK" "$H" check-full >/dev/null 2>&1 || fail "COV-1 control: honest $BLK should be VALID"
if HAZYNC_COV1_BADTIME=1 HAZYNC_BLOCK="$BLK" "$H" check-full >/dev/null 2>&1; then
  fail "COV-1 attack: a time-too-old block (MTP(prev)==block time) should be REJECTED (non-zero exit)"
fi
pass "COV-1 time-too-old rejected; honest accepted"

# --- SEC-2: forged accumulator inclusion (position lie) -------------------------------------------
# The Utreexo accumulator is the one non-Core trust component. HAZYNC_SEC2_BADPOS corrupts a spend's
# claimed global position while leaving its inclusion proof honest — the exact inconsistency an honest
# witness-builder cannot express. The guest's hardened `delete` must reject it (all_ok=false), so
# check-full exits non-zero. (block_130000 has spends, so the first-spend corruption fires.)
if HAZYNC_SEC2_BADPOS=1 HAZYNC_BLOCK="$BLK" "$H" check-full >/dev/null 2>&1; then
  fail "SEC-2 attack: a forged accumulator position should be REJECTED (non-zero exit)"
fi
pass "SEC-2 forged accumulator inclusion rejected; honest accepted (COV-1 control above)"

# --- SEC-1: BIP141 witness commitment (heavy: block 741000 in execute mode) -------------------------
# Opt-in — the big block is slow to execute (per-input ECDSA emulation). Enable in a nightly/heavy job.
if [ "${HAZYNC_CI_HEAVY:-0}" = "1" ]; then
  HAZYNC_BLOCK=block_741000.json "$H" check-full >/dev/null 2>&1 || fail "SEC-1 control: honest 741000 should be VALID"
  if HAZYNC_BLOCK=block_741000_badwit.json "$H" check-full >/dev/null 2>&1; then
    fail "SEC-1 attack: witness-commitment badwit block should be REJECTED"
  fi
  pass "SEC-1 witness-commitment badwit rejected; honest accepted"
else
  echo "skip: SEC-1 badwit (set HAZYNC_CI_HEAVY=1 to run the heavy block-741000 execute test)"
fi

# NOTE: retarget / block-weight / sigops reject-paths are enforced in the guest (see SOUNDNESS.md §5) but
# lack execute-mode test hooks; adding negative hooks for them is tracked follow-up (host-side, no guest
# change) so they too become continuously enforced.
echo "ALL NEGATIVE TESTS PASSED"

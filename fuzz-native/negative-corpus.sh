#!/usr/bin/env bash
# In-zkVM NEGATIVE corpus: every consensus rule violated ONE AT A TIME, each of which must be REFUSED.
#
# docs/FUZZING.md asks for exactly this and pairs it with a positive corpus that must all prove. The
# positive half is covered elsewhere (fuzz-native/realvector.cpp: 892 real mainnet inputs, all valid;
# and `host regress`). This is the other half — evidence the guest REJECTS what Core rejects, which
# 892 passing inputs say nothing about.
#
# Execute mode only: a rule violation fails during execution, so no proving and no GPU is needed.
#
#   ./fuzz-native/negative-corpus.sh              # uses prover/block_130000.json
#   HAZYNC_BLOCK=... ./fuzz-native/negative-corpus.sh
set -uo pipefail
H=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BIN="${HAZYNC_HOST:-$H/prover/target/release/host}"
BLOCK="${HAZYNC_BLOCK:-block_130000.json}"
[ -x "$BIN" ] || { echo "no host binary at $BIN"; exit 2; }

# var                    what it corrupts                          which guest flag must go false
CASES=(
  "HAZYNC_TEST_BADNBITS|prev nBits on a non-boundary block|retarget_ok"
  "HAZYNC_TEST_BADWEIGHT|+1.1MB OP_RETURN on the coinbase|weight_ok"
  "HAZYNC_TEST_BADSIGOPS|+30001 OP_CHECKSIG on the coinbase|sigops_ok"
  "HAZYNC_TEST_BADSUBSIDY|coinbase value +1 satoshi|subsidy_ok"
  "HAZYNC_TEST_BADMERKLE|one bit of the header merkle root|merkle_ok"
  "HAZYNC_TEST_BADPOW|one bit of the header nonce|pow_ok"
  "HAZYNC_TEST_BADBIP34|the height pushed in the coinbase scriptSig|bip34_ok"
)

pass=0; fail=0
echo "=== POSITIVE control: the unmodified block MUST validate ==="
if ( cd "$H/prover" && HAZYNC_BLOCK="$BLOCK" "$BIN" check-full ) >/tmp/nc_base.log 2>&1; then
  echo "  ✅ clean block validates — the corpus is loadable and the check can pass"
else
  echo "  ⛔ the UNMODIFIED block failed to validate — every rejection below would be meaningless"
  tail -3 /tmp/nc_base.log | sed 's/^/     /'; exit 1
fi

echo "=== NEGATIVE: each rule violated alone must be REFUSED ==="
for c in "${CASES[@]}"; do
  IFS='|' read -r var what flag <<< "$c"
  if ( cd "$H/prover" && env "$var=1" HAZYNC_BLOCK="$BLOCK" "$BIN" check-full ) >/tmp/nc_$var.log 2>&1; then
    echo "  ⛔ $flag: ACCEPTED a block with $what — the rule is not enforced"
    fail=$((fail+1))
  else
    echo "  ✅ $flag: refused ($what)"
    pass=$((pass+1))
  fi
done

echo
echo "  $pass refused, $fail wrongly accepted, of ${#CASES[@]} consensus rules"
[ "$fail" -eq 0 ] || exit 1

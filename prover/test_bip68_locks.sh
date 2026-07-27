#!/bin/bash
# BIP68 relative-locktime test matrix, driven with REAL mainnet median-time-past (MTP) data.
#
# Exercises the real Core-derived check `check_input_locks` (guest mode 8, via `host test-locks`).
# No block is proven: the check only reads tx.version + vin[idx].nSequence + the coin's creation-MTP +
# the spending block's MTP, so we feed it real MTP numbers directly. This is the branch NO tested block
# exercises (time-based relative locks are rare on mainnet), so we test it in isolation.
#
# Real data used (recompute with any explorer; MTP = median of the 11 timestamps ending at that height):
#   block 700000 MTP = 1631331088   (2021-09-11 03:31:28 UTC)  <- the spent coin's creation MTP
#   block 700100 MTP = 1631394107   (2021-09-11 21:01:47 UTC)  <- the spending block's MTP
#   => 63019 s (~17.5 h) elapsed over those 100 blocks
#
# The C3/C4 pair is the point: the SAME 388-day time-locked spend at the SAME block is wrongly VALID
# under the placeholder coin_mtp=0 (the open SEC gap) but correctly REJECTED once the real creation-MTP
# is supplied (what the archive-node bridge provides for free). See SECURITY.md / SOUNDNESS.md.
set -uo pipefail
H=${HAZYNC_HOST:-./target/release/host}
fail=0
CMTP=1631331088   # real block 700000 MTP
SMTP=1631394107   # real block 700100 MTP
TYPE=4194304      # 1<<22  (BIP68 time-based type flag)

# ASSERT the return code — this used to only print it, so a regression would have scrolled past as
# ordinary output and the script would still have exited 0.
run() { # label  want_rc  seq  coin_mtp  [coin_h  spend_h]
  local label="$1" want="$2" out rc
  out=$(HAZYNC_LOCK_SEQ=$3 HAZYNC_LOCK_COINMTP=$4 HAZYNC_LOCK_SPENDMTP=$SMTP \
        HAZYNC_LOCK_COINH=${5:-700000} HAZYNC_LOCK_SPENDH=${6:-700100} HAZYNC_LOCK_CB=0 \
        RUST_LOG=error $H test-locks 2>/dev/null)
  rc=$(sed -E 's/.*rc=(-?[0-9]+).*/\1/' <<<"$out")
  if [ "$rc" = "$want" ]; then echo "  ok:   $label (rc=$rc)"
  else echo "  FAIL: $label — expected rc=$want, got rc=${rc:-?}"; echo "        $out"; fail=1; fi
}

echo "=== BIP68 TIME-BASED (real coin_mtp = block 700000 MTP; spend at block 700100; ~17.5 h elapsed) ==="
run "C1 lock=512s   satisfied"                        1   $((TYPE|1))     $CMTP
run "C2 lock=28.4h  unmet"                          -42   $((TYPE|200))   $CMTP
echo
echo "=== THE GAP: identical 388-day time-locked spend at block 700100 ==="
run "C3 coin_mtp=0  PLACEHOLDER (documents the gap)"   1   $((TYPE|65535)) 0
run "C4 coin_mtp=REAL  bridge-data rejects"         -42   $((TYPE|65535)) $CMTP
echo
echo "=== BIP68 HEIGHT-BASED (coin height 700000, spend height 700100) ==="
run "C5 lock=200 blk unmet"                         -41   200             $CMTP
run "C6 lock=100 blk satisfied"                       1   100             $CMTP

[ "$fail" -eq 0 ] && echo ">>> BIP68 LOCK-MATRIX TEST PASS" || { echo ">>> BIP68 LOCK-MATRIX TEST FAIL"; exit 1; }

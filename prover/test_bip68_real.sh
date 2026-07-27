#!/bin/bash
# BIP68 time-based relative-lock — proven on a REAL mainnet transaction.
#
# The time-based branch of the relative-lock rule is rare on-chain, so no block in the early-history
# test set exercises it. This runs the REAL Core-derived check (`check_input_locks`, guest mode 8) on an
# ACTUAL mainnet transaction that uses a 90-day CSV lock, with the REAL median-time-past values.
#
# Transaction (mainnet):
#   txid    3fa669af8754cb15309875350b88489e80b4f9254d6bc3bd772c56283b6ccfe8   (block 958250, vin[0])
#   nSequence 0x00403b53  -> BIP68 time flag set, value 15187 -> 15187*512 = 7,775,744 s = 90.0 days
#   spends a coin created at height 945409 (a Taproot script-path CSV output)
#
# Real median-time-past data (recompute from mainnet block timestamps; MTP = median of the 11 timestamps
# ending at that height — Core's GetMedianTimePast):
#   coin_mtp  = MTP(945408) = 1776385451     (the coin's creation-block MTP, per BIP68 GetAncestor(h-1))
#   spend_mtp = MTP(958250) = 1784181022     (the spending block's MTP)
#   elapsed   = 7,795,571 s (~90.2 days) >= required 7,775,744 s (90.0 days)  -> mainnet ACCEPTED it.
#
# Expected: the real coin age (90.2 d) VALIDATES (rc=1), matching mainnet; a coin ~0.3 d younger is
# REJECTED (rc=-42). The real Core check, real tx, real MTP — BIP68-time on real data.
#
# The HEIGHTS matter as much as the MTPs: check_input_locks only ENFORCES BIP68 from the CSV activation
# height (419328), matching Core. This script used to leave the heights at the host defaults (100/200),
# which is BELOW that gate — so the time-based branch never ran and BOTH cases returned VALID, including
# the one the script announced as "expect REJECT". It printed the contradiction and exited 0 because it
# asserted nothing. Pass the real heights, and assert.
set -uo pipefail
H=${HAZYNC_HOST:-./target/release/host}
fail=0
# rc=1 VALID, rc=-42 time-lock unmet. Assert the code, don't just print it.
check() {  # check <label> <expected-rc> <coin_mtp> <coin_h> <spend_h>
    local label="$1" want="$2" cmtp="$3" ch="$4" sh="$5" out rc
    out=$(HAZYNC_LOCK_RAWTX=$RAWTX HAZYNC_LOCK_IDX=0 HAZYNC_LOCK_COINMTP=$cmtp \
          HAZYNC_LOCK_SPENDMTP=$SMTP HAZYNC_LOCK_COINH=$ch HAZYNC_LOCK_SPENDH=$sh \
          RUST_LOG=error $H test-locks 2>/dev/null)
    rc=$(sed -E 's/.*rc=(-?[0-9]+).*/\1/' <<<"$out")
    if [ "$rc" = "$want" ]; then echo "ok:   $label (rc=$rc)"
    else echo "FAIL: $label — expected rc=$want, got rc=${rc:-?}"; echo "      $out"; fail=1; fi
}
RAWTX=02000000000101cced4edb6445045c3f0126c8369701ddece1589c867450c671cf9d776c7dba030100000000533b400001d59b0a0000000000225120d808b084ec5e6c79369964f45d2fccf9857780d3bee23d2135f2f6dae408054a0441cced5e91c47df7f4b743b55c06c8f14249936df46ba7c13f40c4f439fda398e6321918b3359a80f763c8dd5fbfc5a0313025e727ca8b77ea91251fc6b53e196a1b4087ce9349104a0e6b23fa1a4677dcc87c2307764a327ae851c19e55ffe85c24ac957c0859461135cac1d1491e2034e82d38caed3971d1fcac7340cfb999a797154b03533b40b2752023ae13dcab0c93bbf20b19826c9185bd6b311fd52ca5ecb7bfeaf9369b3562a9ada82040e97d2c997165ee580c5bcc605bc906549111108bdb550899f4649fef2123328741c050929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac04e11f795623944214463e2ea47f24e67496671a4c8e8a8a176ac18f538a9274c00000000
CMTP=1776385451
SMTP=1784181022

COINH=945409      # real creating height
SPENDH=958250     # real spending height (both above CSV activation 419328, so BIP68 is enforced)

check "REAL DATA (mainnet-valid, coin 90.2d old) accepted"      1   "$CMTP"             $COINH $SPENDH
check "COUNTERFACTUAL (coin ~0.3d younger) rejected"          -42   "$((CMTP+25000))"   $COINH $SPENDH
# Below the CSV activation height Core imposes no relative-lock constraint, and neither may we —
# enforcing there would reject blocks Core accepts.
check "pre-CSV height: same unmet lock NOT enforced"            1   "$((CMTP+25000))"   100    200

[ "$fail" -eq 0 ] && echo ">>> BIP68 REAL-TX TEST PASS" || { echo ">>> BIP68 REAL-TX TEST FAIL"; exit 1; }

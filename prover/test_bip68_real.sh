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
set -e
H=${HAZYNC_HOST:-./target/release/host}
RAWTX=02000000000101cced4edb6445045c3f0126c8369701ddece1589c867450c671cf9d776c7dba030100000000533b400001d59b0a0000000000225120d808b084ec5e6c79369964f45d2fccf9857780d3bee23d2135f2f6dae408054a0441cced5e91c47df7f4b743b55c06c8f14249936df46ba7c13f40c4f439fda398e6321918b3359a80f763c8dd5fbfc5a0313025e727ca8b77ea91251fc6b53e196a1b4087ce9349104a0e6b23fa1a4677dcc87c2307764a327ae851c19e55ffe85c24ac957c0859461135cac1d1491e2034e82d38caed3971d1fcac7340cfb999a797154b03533b40b2752023ae13dcab0c93bbf20b19826c9185bd6b311fd52ca5ecb7bfeaf9369b3562a9ada82040e97d2c997165ee580c5bcc605bc906549111108bdb550899f4649fef2123328741c050929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac04e11f795623944214463e2ea47f24e67496671a4c8e8a8a176ac18f538a9274c00000000
CMTP=1776385451
SMTP=1784181022

echo -n "REAL DATA (mainnet-valid, coin 90.2d old)  -> expect VALID  : "
HAZYNC_LOCK_RAWTX=$RAWTX HAZYNC_LOCK_IDX=0 HAZYNC_LOCK_COINMTP=$CMTP HAZYNC_LOCK_SPENDMTP=$SMTP RUST_LOG=error $H test-locks 2>/dev/null
echo -n "COUNTERFACTUAL (coin ~0.3d younger)        -> expect REJECT : "
HAZYNC_LOCK_RAWTX=$RAWTX HAZYNC_LOCK_IDX=0 HAZYNC_LOCK_COINMTP=$((CMTP+25000)) HAZYNC_LOCK_SPENDMTP=$SMTP RUST_LOG=error $H test-locks 2>/dev/null

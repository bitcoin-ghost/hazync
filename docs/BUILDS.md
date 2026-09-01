# The two builds

⏰ **Pinned 2026-09-01** against `feat/liftx-accel`. Everything here was measured in **execute mode
on one laptop** — CPU only, no GPU — on block 962,000, single chunk, against a stock control built
from the same tree. ⛔ **Wall-clock and therefore card counts are DERIVED, not measured.** Section 4
says exactly what a GPU run would settle.

## 1 · Results

| build | cycles | vs stock | chunk | straggler | cards |
|---|---|---|---|---|---|
| stock control | 13,748,003,793 | 1.000x | 14,417 s | 1.054 | 28 |
| **CORE** | **3,357,576,338** | **4.095x** | 3,521 s | **1.311** | **9** |
| GHOST (no field backend) | 1,287,797,844 | 10.676x | 1,350 s | 1.407 | 5 |
| **GHOST** | **1,198,904,653** | **11.467x** | 1,257 s | ⚠ 1.407 *(assumed)* | **5** |

**Every build commits `4fb3e3c5e80417c87584a617d23b53d8c49940348c0e8d455f66299b4bd4656d`** with
`all_valid=1`, `binds=8006` — byte-identical to the stock control and to the recorded value. No
consensus output moves in either mode.

## 2 · CORE — *Core's own code decides*

```bash
# patches (secp256k1 tree)
0012-select-field-bigint2-backend.patch
0013-lift-x-via-witness-hint.patch
# build
HAZYNC_FIELD_BIGINT2=1 HAZYNC_LIFTX_HINT=1 HAZYNC_ECMULT_WINDOW=21 cargo build --release
# packing constants -- REFITTED, and they matter: 1.557 -> 1.311 straggler
HAZYNC_COST_EC_OP=450020 HAZYNC_COST_SCHNORR_OP=450020
HAZYNC_COST_INPUT_BYTE=1 HAZYNC_COST_INPUT_BASE=37946
```

libsecp keeps its wNAF, its GLV, its ECDSA logic and every check. Two changes beneath it: the field
*backend* (an interface libsecp already parameterises for its own use) and a pubkey-Y hint that
**libsecp's own `fe_sqr`/`fe_equal` verify before accepting** — a wrong or missing hint falls back to
its real sqrt.

## 3 · GHOST — *fastest wins*

```bash
# patches (secp256k1)          # patches (bitcoin-core)
0005-ecdsa-verify-group-arith-via-bigint2.patch   0009-sha-transform-fastpath.patch
0006-schnorr-verify-group-arith-via-bigint2.patch 0010-transformd64-via-accelerator.patch
0008-scalar-inverse-via-bigint2.patch
0012-select-field-bigint2-backend.patch
0013-lift-x-via-witness-hint.patch
# build
HAZYNC_BIGINT2_ECDSA=1 HAZYNC_LIFTX_HINT=1 HAZYNC_FIELD_BIGINT2=1 HAZYNC_BIGINT2_SCHNORR=1 \
HAZYNC_SCALAR_INV_ACCEL=1 HAZYNC_SHA_FASTPATH=1 HAZYNC_AGG_READSLICE=1 HAZYNC_SHA_D64_ACCEL=1 \
HAZYNC_ECMULT_WINDOW=21 cargo build --release
# packing constants -- DEFAULTS. Do NOT apply Core's refit here.
```

⛔ **The same refit helps Core and hurts Ghost.** Core 1.557 → 1.311; Ghost 1.407 → **1.884**, and
1.996 on GHOST. It costs a card. The likely cause is rescaling Ghost's Schnorr constant by the old
13.77x ECDSA ratio, when the hint removes decompression from *both* curves — taproot key-path spends
call `lift_x` too — so that ratio should have narrowed. **Ghost needs its own calibration; it does
not have one.**

✅ **#139 and the field backend are orthogonal and stack**: 10.676x → 11.467x, +7.4%. They accelerate
different things — #139 replaces `secp256k1_ecmult`; the field backend replaces the representation
*underneath everything else*.

## 4 · ⛔ What is NOT measured

1. **Wall-clock, on a GPU.** Every card count here converts cycles at a fixed ratio. Proving time is
   quantised by segment (`ceil(cycles / 2^po2)`), so the real straggler can differ from the cycle
   straggler. **This is the one thing that needs hardware.**
2. **GHOST's straggler at default constants** — assumed 1.407 from the no-backend arm, never
   measured for the backend arm. ⏰ It decides 5 cards vs 4: at 1.35 GHOST is **four cards**.
3. **Aggregate** — both modes assume 627 s and a 1.881x two-worker split, measured previously.

## 5 · Reproducing

```bash
scripts/field-backend-tests.sh                     # correctness gates, no GPU, ~3 min
HAZYNC_BLOCK=<block_962000.json> HAZYNC_CHUNKS=1 HAZYNC_PROFILE_EXEC=1 \
  ./target/release/host chunk-profile               # cycles + journal digest
HAZYNC_CHUNKS=16 ...                                # per-chunk cycles + measured straggler
```

⚠ `chunk-profile` executes the block **twice** (count-packed and cost-packed); budget accordingly.
⚠ The straggler is now reported on **measured cycles** as well as predicted. Only read the measured
one: the cost packer balances its own predictor by construction and will report a perfect 1.00x
however wrong that predictor is.

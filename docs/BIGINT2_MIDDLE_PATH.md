# bigint2 middle path — route ECDSA group arithmetic through the accelerator

**MEASURED on an L40S, 2026-08-28. hazync#139.**

Replaces **exactly one line** of `secp256k1_ecdsa_sig_verify` — the `secp256k1_ecmult` computing
`u1*G + u2*Q` — with risc0-crypto's `double_scalar_mul` over the bigint2 coprocessor.

## What stays Core's / libsecp's literal code

```text
  zero checks on r and s            <- libsecp, unchanged
  scalar_inverse_var(sn, s)         <- libsecp, unchanged
  u1 = sn*m ; u2 = sn*r             <- libsecp, unchanged
  secp256k1_ecmult(pr, Q, u2, u1)   <- THE ONLY LINE REPLACED
  infinity check                    <- libsecp, unchanged
  r == x(pr) mod n                  <- libsecp, unchanged
```

DER parsing, low-S handling, pubkey parsing and Core's sighash are untouched.

⚠ It does **not** preserve libsecp's wNAF/GLV — `double_scalar_mul` is Shamir's trick, so the
scalar-multiplication *strategy* changes with it. Far smaller than substituting the whole `ecdsa`
module, **but not nil**.

## Measured

| arm | po2 21 | po2 22 | vs stock |
|---|---|---|---|
| stock libsecp | 446, 446, 446 s (189 seg) | 396, 396 s (93 seg) | — |
| **middle path** | **55, 56, 56, 56 s** (21 seg) | ~50 s (inferred) | **8.00x** |

- execute-mode cycles **9.19x**; GPU proving wall **8.00x** — the coprocessor takes ~13%, not the win
- segment count 189 → 21 (**9.0x**) corroborates from an independent measurement path
- per-verify ECDSA **1,723,407 → 140,044 cycles (12.31x)**
- journal digest **identical** to stock on all 212 signatures of block 140,000

## Soundness posture

A precompile is **constrained, not trusted** — the circuit proves it computed correctly. Same
argument already accepted for the SHA-256 accelerator (`patches/0002`), so this adds no trust
assumption beyond RISC0. What it adds is a **plumbing-correctness obligation**: that the big-endian
limb↔byte conversion in `bigint2_ecmult.rs` is right, and that Shamir's trick agrees with wNAF/GLV.

⛔ **The load-bearing assurance work — differential-testing against libsecp over chain history — has
not started.**

⚠ **No BIP340.** risc0-crypto has no Schnorr, so this accelerates the ECDSA fraction only. Block
962,000 is **1.8% taproot by input**, so nearly all of a tip block benefits — but hazync#190's
type-aware packer is a **prerequisite**, or the post-#139 straggler goes to 2.45x.

## How it is gated

Off unless asked for, three times over: `risc0-crypto` is an **optional** dependency, the guest module
sits behind the **`bigint2-ecdsa`** feature, and patch 0005's body is `#ifdef`'d. A default build is
byte-identical and `METHOD_ID` does not move.

```sh
HAZYNC_BIGINT2_ECDSA=1 ./provision-vps.sh          # applies patch 0005
cd prover && HAZYNC_BIGINT2_ECDSA=1 cargo build --release --features cuda
```

⛔ **Turning it on MOVES `METHOD_ID`** and resets the board. It must ride one re-baseline together
with the aggregate witness read and the Tier 0 codegen batch — see `TOPOLOGY_AND_SETTINGS.md`.

⚠ `cc::Build` emits no `rerun-if-changed` for **headers**, so a patch-only change reuses the cached
guest. Force with `rm -rf target/riscv-guest`, and **always check `METHOD_ID` actually moved.**

# Hazync v0.21.0 — Core becomes the guest that ships

**The release binary now proves with Core acceleration on.** v0.20.0 shipped the stock guest with
every lever off; the README meanwhile said *"CORE — what ships"* and *"Core is the project"*. That
gap is what this release closes. Nothing about the claim changed — the build finally matches it.

⛔ **This is a re-baseline. Every proof published under `3867611d…` is invalid**, and the board
resets to zero from whatever it has reached — 7,852 blocks measured 2026-09-07 09:56Z, and still
climbing, so treat that as a reading and not a final cost. That is not a side effect to apologise
for, it is what a new guest
*means*: a verifier pinned to a new image id cannot accept a proof produced by a different one.

> ⏰ **Canonical `METHOD_ID`: `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`**
> (supersedes `3867611d…`). Produced by **three independent builds that agree** — a GitHub runner, a
> `docker build -f reproduce/Dockerfile .` on the coordinator, and a direct `provision-vps.sh` build
> on an L40S box. A laptop build yields `05a5a279…`; that is not drift, it is the id absorbing
> `$HOME/.cargo` and `$HAZYNC_BASE` paths — which is exactly why the canonical id comes from the
> container and never from a developer machine.

---

## The three channels

Hazync builds one guest three ways. They validate the same Bitcoin blocks and commit the same 32
bytes; they differ in how much of Core's own arithmetic runs.

| channel | what is substituted | fidelity | ships? | board? |
|---|---|---|---|---|
| **stock** | SHA-256 only (`0002`) | `identical` | no | no |
| **CORE** | + field backend, + `lift_x` hint | `substitution (narrow)` + `advice-and-verify` | ✅ **yes** | ✅ **yes** |
| **ghost** | + ECDSA/Schnorr group arith, scalar inverse, SHA fastpath | `substitution (broad)` | no | no |

**stock is not retired.** It becomes the *oracle*: the fidelity floor every acceleration claim is
checked against. Any statement that a lever left consensus untouched is made by diffing a journal
against stock's, and stock is the only build that can make that statement.

**ghost is not a lesser Core.** It is where levers land first, and its constants are its own —
docs/BUILDS.md §3.

---

## Why Core is defensible as the shipped guest

Two levers, and the precedent for both already shipped:

- **`0013` — `lift_x` witness hint** — `advice-and-verify`. The hint is *checked* against
  `y² = x³ + 7` with libsecp's own `fe_sqr` before it is accepted; a wrong or missing hint falls
  back to the real square root. Core's arithmetic still decides.
- **`0012` — `field_bigint2` backend** — `substitution (narrow)`. One primitive replaced at a
  backend interface libsecp already parameterises for its own use. libsecp keeps its wNAF, its GLV,
  its ECDSA logic and every check above it.

⚖ The narrow substitution is a judgement, and it is **not a new one**: `patches/0002` routes SHA-256
through the risc0 accelerator, is also a substitution, and has shipped in every release to date.
Core applies the same decision one layer down.

### ✅ The digest gate

Block 962,000, 8,006 inputs, execute mode. Both arms built from one tree, differing only in whether
`0012`/`0013` are applied and their defines set, with `HAZYNC_ECMULT_WINDOW=21` held constant so the
experiment isolates exactly the two levers:

| | `fq_mul` | `lift_x_hint` | journal sha256 | cycles |
|---|---|---|---|---|
| **CORE** | 1 | 1 | `4fb3e3c5…4656d` | **3,357,576,338** |
| **STOCK** | 0 | 0 | `4fb3e3c5…4656d` | **13,748,003,793** |

`all_valid=1`, `binds=8006`, `kind=0xc4a10004` on both. **Same 32 bytes, 4.095x fewer cycles.**

⚠ Read that precisely. It is one block, in execute mode, and 962,000 is only 1.8% taproot. It is
**not** a card count — cards need the straggler across a real chunked proving run.

---

## Running a non-default channel

All three recipes live in [`docs/BUILDS.md`](BUILDS.md). The levers are build-time, not runtime:
there is no config file that switches channel, because the channel is compiled into the guest and
its identity *is* the `METHOD_ID`.

```bash
# CORE — the default. provision-vps.sh applies 0012/0013 and sets these itself.
HAZYNC_FIELD_BIGINT2=1 HAZYNC_LIFTX_HINT=1 HAZYNC_ECMULT_WINDOW=21
```

⛔ **A non-default build cannot contribute to the board, and fails in the least obvious way.** It
carries a different `METHOD_ID`, and the coordinator re-verifies every submission against its own:

```
receipt rejected: your prover's guest image id (METHOD_ID) does not match this
coordinator's — you built a different guest.
```

`run-workers.sh` checks the id **only at startup** (hazync#99), so a mismatched worker proves into
rejections indefinitely without complaining. To contribute, use the release binary or the
reproducible build — not a hand-rolled one.

---

## Packing constants

Core's per-curve fit becomes the built-in default. These are **host-side only** and move no image id:

| constant | was (#139/Ghost) | now (Core) |
|---|---|---|
| `COST_PER_EC_OP` | 141,612 | **417,798** |
| `COST_PER_SCHNORR_OP` | 1,950,000 | **462,435** |
| `COST_INPUT_BASE` | 34,000 | **41,387** |
| `COST_PER_INPUT_BYTE` | 6 | **2** |

⚠ They are **per-channel and do not transfer.** Core's fit moves Ghost's straggler 1.407 → 1.884,
about a card. A build that takes the wrong set is worse off than one that takes none.

⚠ `COST_PER_EC_OP_REPEAT` is knowingly left at 104,222 and **documented as unrefit**. It was derived
from a #139 profile as 0.736 of a fresh key, and Core's `lift_x` hint removes most of the
decompression that discount prices — so it is probably wrong. It is unchanged because the straggler
1.210 justifying this channel was measured with it at that value; refitting a fifth constant would
invalidate the number being cited. Tracked in hazync#226.

---

## Also in this release

- **Native fuzz harnesses** (hazync#222): 27 differential checks, 661 memory-safety cases, and
  **892 real mainnet inputs** verified through the guest's `VerifyScript`.
- **Negative corpus** (hazync#223): **7/7** consensus rules refused when violated one at a time.
- **Ghost calibration recorded** (hazync#227): straggler 1.462 → 1.189, measured on two L40S. This
  also corrected `docs/BUILDS.md` §3, which had claimed no such calibration existed.

## Upgrading

```bash
hazync stop && hazync update && hazync id "<your handle>" && hazync work
```

⚠ Set your identity **before** starting workers. Starting first mints a throwaway key and your
proofs land under a name you did not choose.

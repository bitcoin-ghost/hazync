# v0.20.0 — the coprocessor field backend

> **DRAFT.** Not yet cut. The cutover (verifier release, wasm deploy, coordinator swap, board reset,
> re-proving the frontier) has to happen with the tag, because this release **moves `METHOD_ID`**.

**`METHOD_ID` 1d6c3792… → 3867611d…**, so every proof published under the old id must be re-proved.
That is not incidental to this release, it is the release: the guest's field arithmetic changed.

> **Provenance.** Every figure below is one the repo or its git history can show. Cycle figures cite
> `docs/FIELD_BIGINT2_BACKEND.md` or the commit that measured them; card counts and stragglers cite
> `docs/BUILDS.md` §1, which is hardware-measured on two L40S. Nothing here is derived from memory.

---

## The headline: libsecp's field arithmetic on the bigint2 coprocessor

`prover/methods/guest/field_bigint2_impl.h` + `src/field_bigint2.rs` add a third libsecp field
backend alongside `field_5x52` and `field_10x26`, routing 256-bit modular arithmetic through RISC0's
`sys_bigint2` accelerator instead of software limbs. libsecp documents the backend as an extension
point; this uses it as one rather than patching call sites.

Whole-block cycles on block 962,000, against a stock control built from the same tree:

| | cycles | vs control | source |
|---|---|---|---|
| control (stock guest) | 13,748,003,793 | 1.000x | `FIELD_BIGINT2_BACKEND.md` |
| field backend alone | 3,583,757,161 | 3.836x | `FIELD_BIGINT2_BACKEND.md` |
| **Core** (+ liftx hint) | — | **4.095x** | commit `0f90ed0` |
| **Ghost** (all levers) | **1,198,904,653** | **11.467x** | `BUILDS.md` §1 |

**Effect,** measured on two L40S with real proving (`docs/BUILDS.md` §1):

| build | chunk work | straggler | aggregate | **cards** |
|---|---|---|---|---|
| **Core** | 4,029 s | 1.295 | 473 s | **10** |
| **Ghost** | 1,713 s | 1.438 | 473 s | **5** |

Both card counts are MEASURED, not derived — which matters, because every derived figure in this
release moved the wrong way when it was finally measured. See "Corrections" below.

The field backend and #139 are **orthogonal and stack**: 10.676x → 11.467x, +7.4%.

**Correctness.** The gate is the journal digest, not a cycle count: block 962,000 produces
`4fb3e3c5e80417c87584a617d23b53d8c49940348c0e8d455f66299b4bd4656d` byte-identical to the stock
control, with `all_valid=1` over 8,006 binds. `scripts/field-backend-tests.sh` adds three GPU-free
gates (a mod-p harness against arbitrary precision, libsecp's own suite against the new backend, and
nine mutation controls that must each be caught) — two mutations are caught ONLY by the mod-p
harness, so libsecp's suite alone is not sufficient.

Two design decisions worth naming:

- **Lazy and branching.** Elements live in `[0, 2^256)`; adds fold the carry via `2^256 = 2^32 + 977`
  and only `normalize` canonicalises. A libsecp `fe` is magnitude-carrying by design and legitimately
  holds values ≥ p, which is a state no stock backend ever reaches — so libsecp's own suite passes two
  BROKEN lazy backends, and that is why the mod-p harness exists.
- **Constant time was dropped deliberately.** There is no timing side channel in a proven trace, so
  the branch-free discipline buys nothing and costs cycles: `fe_add` went 155 → 74 instructions.

## Zero-copy FFI — worth more than the backend it serves

`2.381x → 3.836x` from removing the limb copy at the Rust/C boundary: `load`/`store` now
`core::ptr::read`/`write` a `[u32; 8]` directly. **Saved 2,191 M cycles**, which is larger than the
entire remaining field-arithmetic win.

## liftx hint (+6.31%) and the packer

- **liftx via witness hint** — the x-only pubkey lift is verified against a host-supplied hint rather
  than recomputed. **+6.31%** (commit `0f90ed0`; 6,897 hits / 134 misses, 98.1%), digest gate PASS.
  Fidelity class: advice-and-verify. Its FLAT self-time is 0.30%, so a flat profile hides this lever
  20x — size delegating functions cumulatively.
- **The packer gained three dimensions it was blind to**: curve (ECDSA and Schnorr price separately,
  because #139 accelerates only ECDSA and they diverge ~13.8x), key reuse (the per-chunk decompression
  memo makes a repeat cost ~0.736 of a fresh key; 68.8% of block 962,000's verifying inputs take it),
  and marshalling bytes.
- **Straggler 1.557 → 1.311 → 1.210** across the refits. A wrong predictor does not show up as a bad
  straggler — the packer balances its own model perfectly — it shows up as a bad BLOCK.
- Packing constants are now runtime-overridable (`HAZYNC_COST_*`) and **calibrated per build mode**.

## Bugs fixed, several of which silently disabled measurements

| where | what it did |
|---|---|
| `methods/guest/build.rs` | `cc::Build::define(X, None)` emits a bare `-DX`, making `#if defined(X)` true — **all 8 guest levers were welded on**, so every "lever off" arm secretly had them on |
| patches 0009 / 0010 | the defines were on the wrong `cc::Build` — **the patches were never compiled at all** |
| `field_bigint2_impl.h` | `is_square_var` used a 499-op exponentiation where stock libsecp uses a Jacobi symbol |
| `host/src/main.rs` | `packed_bytes` had no `visit_seq`, so the host could not parse **any** JSON bundle carrying `txids` — including one it had written itself |
| chunk receipts | were not invalidated when `METHOD_ID` moved |
| `gpu-benchmark.sh` | `prove-chunks` does not exist; an unknown arg fell through to a demo path and **exited 0** |

## Corrections — measurements that overturned this release's own projections

Recorded because the pattern is the point, and every one moved the wrong way:

- **bigint2 on block 962,000 measures 4.48x, not the projected 7.53x.** The projection under-weighted
  the ~1.96 G non-ECDSA residual. Not a taproot effect — 962,000 is 1.8% taproot.
- **Core is 10 cards, not 9.** Ghost's chunk work came in 36% above projection.
- Stragglers were predicted within a few percent; cycles → wall-clock was optimistic by 14–36%.
- **The aggregate is not the wall it was reported to be**: 772.4 s with one remote worker, 473.1 s
  once the coordinator also works (1.63x, free), 405.6 s with two (1.90x).
- **`COST_PER_INPUT_BYTE` (6) is roughly 2x too high** under #139 — measured 3.13 cycles/byte from a
  controlled pair. NOT refitted here: block 741000 carries 2 Schnorr verifies out of 723, so it cannot
  inform the curve split, which is the dimension the packer most needs right.

## Repo, docs and tooling

- **README** leads with the two operating modes — Core (fidelity) vs experimental Ghost (speed) — plus
  a repo map, a mermaid pipeline, live badges and a terminal cast of a real verification.
- **`docs/` is current truth; `docs/history/` is the development record**, which names its own stale
  figures rather than quietly carrying them.
- **`docs/BUILDS.md`** pins both builds exactly: patches, env levers, constants, and explicitly what is
  NOT measured.
- **`scripts/gpu-benchmark.sh`** is turnkey, with two-sided exact symbol assertions, a CUDA link check
  and a #119 retry scoped to that one transient fault.
- **`scripts/check-versions.sh`** now passes: a re-baseline can no longer leave a doc quoting a retired
  guest id as current.

## Tests added

`field-backend-tests.sh` (3 gates, 9 mutation controls) · bundle JSON round-trip now covers
`PackedHash`/`PackedHashes` values, not just parsing · packer tests for the curve split, key reuse and
marshalling bytes, the last re-anchored to a measured #139 profile · `msm-selftest` and `msm-bench` ·
two-sided bigint2 symbol assertions.

## Landed after the field backend, and part of this release

**The coordinator's 220,001 stored bundles survive the re-baseline** (#210). Every one of them —
**74 GB** — is in the pre-2026-08-28 nested wire shape, and the post-packing host could not parse a
single one: `invalid type: sequence, expected u8`. `RELEASE_PLAN.md` said they "regenerate", which
would have meant re-running the bridge over 220k blocks against a full node **to arrive at identical
leaf values**, because only the encoding changed. `PackedHashes` now reads both shapes; nothing
writes the legacy one, and the binary path is untouched. Verified against a real production bundle
pulled off the box, not a fixture.

**A stranger test** (#211). The README's own commands, run verbatim in a pristine container with no
checkout, no cache and no keyring. Nothing else in CI looks at the *published* surface — the release
assets, the live coordinator, the served wasm and the signing chain are all outside the repo, so any
of them can break with every job green. Baseline before this cutover: **18 passed, 0 failed**,
including the full trust chain (fingerprint from `SECURITY.md` → key from the publishing account →
good signature → matching checksum).

**The CUDA segment default was documented wrong** (#198). `seg_po2()` returns 21 on CUDA, not 22 —
verified against the code. po2 21 peaks at ~22 GB; po2 22 at ~40.6 GB, and you only get it by setting
`HAZYNC_SEG_PO2` explicitly.

**Two operating modes, named** (#197), and **the aggregate taken apart** (#200) — including the
correction that it measures **405.6 s** on two workers, not the 1,575 s that read as "impossible at
any N".

**`release.sh` phase 4 aborted on an unbound `$2`** (#212), right before the CUDA artifact check —
the one artifact that cannot be verified by running it. Found by `--dry-run` preflighting this
release.

## What this release does NOT touch

- **No consensus logic.** The guest still runs Bitcoin Core's unmodified `VerifyScript`, sighash and
  `pow.cpp`. The field backend swaps how 256-bit modular arithmetic is *computed*, not what is checked
  — and the journal-digest gate is what holds that claim up.
- **No change to the proof system, the aggregate's semantics, or the wire format's meaning.**
- **The reproducible-build mechanism is unchanged** — pinned toolchain, Core v28.0, secp256k1 v0.5.1,
  committed `Cargo.lock`, `reproduce/Dockerfile`. The id is a new value, not a new mechanism.
- **Ghost is still experimental and is not what ships.** `provision-vps.sh` refuses to let a box
  provisioned with the #139 patch produce a shipped proof, and the canonical guest is the stock build.

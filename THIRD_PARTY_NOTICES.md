# Third-party notices

Hazync is MIT-licensed (see [`LICENSE`](LICENSE)). It builds on, and in some cases compiles in,
third-party components under their own licences. This file records them.

## Compiled into the zkVM guest

- **Bitcoin Core** — MIT. `bitcoin/bitcoin` tag `v28.0`. The guest compiles Core's real consensus
  sources (`interpreter.cpp`, `pubkey.cpp`, sighash, serialization) unmodified except for two
  patches that change no consensus logic: `patches/0001` (an ILP32 `Serialize` overload) and
  `patches/0002` (SHA-256 compression routed through the RISC Zero accelerator, byte-identical
  output). Copyright (c) 2009-present The Bitcoin Core developers.
- **libsecp256k1** — MIT. `bitcoin-core/secp256k1` tag `v0.5.1`, compiled for real ECDSA and Schnorr
  verification. Copyright (c) 2013 Pieter Wuille and contributors. **Modified:** since v0.21.0 the
  canonical build applies `patches/0012` (selects a coprocessor-backed field backend at libsecp's
  field-backend interface; the backend sources are `prover/methods/guest/field_bigint2*.h`) and
  `patches/0013` (recovers a pubkey's Y from a witness hint that libsecp's own field arithmetic
  checks before accepting). Every algorithm above the field is unchanged.

## Build / proving stack (linked, not part of the consensus path)

- **RISC Zero (risc0)** zkVM, `risc0-zkvm` / `risc0-build` / `risc0-zkp` `=3.0.5`, and the rzup
  cross-toolchain — Apache-2.0. The `prover/` crate was scaffolded from the risc0 project template and
  carries an additional Apache-2.0 notice at [`prover/LICENSE`](prover/LICENSE) covering that
  risc0-derived build scaffolding. Copyright (c) RISC Zero, Inc.
- **RustCrypto `sha2`** — MIT/Apache-2.0, pinned to an immutable commit, routed through the risc0
  SHA-256 accelerator (byte-identical output).

## Vendored and modified (Apache-2.0)

Both are RISC Zero crates, copied into `vendor/` and wired in through `[patch.crates-io]` in
`prover/Cargo.toml`. Copyright (c) RISC Zero, Inc. Each is upstream's published crate except for the
changes stated here, which are marked in the source (`hazync patch`, `hazync#…`).

- **`vendor/risc0-zkvm`** — `risc0-zkvm` 3.0.5. Modified in `src/host/server/prove/mod.rs` and
  `src/host/server/prove/prover_impl.rs` only: an API to assemble a receipt from segment receipts
  proved on other machines (segment distribution, hazync#148, #153, #161), and a balanced join tree
  in place of the linear fold. Host-side proving only.
- **`vendor/risc0-circuit-rv32im-sys`** — `risc0-circuit-rv32im-sys` 4.0.3. Modified in
  `kernels/cxx/ffi.cpp` and `kernels/cuda/ffi.cu` only (marked `HAZYNC_119_ACCUM_FIX`): phase 3 of the
  accum pass no longer adds to LogUp cells an instruction arm never wrote, which produced an invalid
  receipt (hazync#119). Prover-only; the circuit, the verifier and `METHOD_ID` are unchanged.

## Our own code

- The Utreexo UTXO accumulator (`accumulator/`), the coinbase sparse Merkle tree (`coinbase-smt/`),
  the shared range-state types (`rangestate/`), the host/prover driver (`prover/host/`), the guest
  glue (`prover/methods/guest/`), the verifiers (`verifier/`, `verifier-ffi/`, `verifier-wasm/`), and
  the coordinator (`coordinator/`) are original work under the root MIT licence (the `verifier`
  crate additionally declares Apache-2.0 in its `Cargo.toml`).

Nothing here overrides the terms of the components' own licences; consult each upstream project for
the authoritative text.

## Prior art

Not a dependency, and nothing of theirs is compiled in — recorded because the idea is theirs before
it was ours.

- **ZeroSync** — https://zerosync.org. Robin Linus and collaborators. The project that established
  proving Bitcoin chain state as a real engineering problem rather than a thought experiment.
  Hazync takes a different trade (Core's own consensus sources in a general-purpose zkVM, rather
  than a purpose-built circuit) and is downstream of theirs in the sense that matters: the question
  was already asked.

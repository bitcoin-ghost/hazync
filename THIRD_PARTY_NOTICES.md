# Third-party notices

Hazync is MIT-licensed (see [`LICENSE`](LICENSE)). It builds on, and in some cases compiles in,
third-party components under their own licences. This file records them.

## Compiled into the zkVM guest

- **Bitcoin Core** — MIT. `bitcoin/bitcoin` tag `v28.0`. The guest compiles Core's real consensus
  sources (`interpreter.cpp`, `pubkey.cpp`, sighash, serialization) unmodified except for two
  portability patches (`patches/0001`, `patches/0002`) that change no consensus logic. Copyright
  (c) 2009-present The Bitcoin Core developers.
- **libsecp256k1** — MIT. `bitcoin-core/secp256k1` tag `v0.5.1`, compiled for real ECDSA and Schnorr
  verification. Copyright (c) 2013 Pieter Wuille and contributors.

## Build / proving stack (linked, not part of the consensus path)

- **RISC Zero (risc0)** zkVM, `risc0-zkvm` / `risc0-build` / `risc0-zkp` `=3.0.5`, and the rzup
  cross-toolchain — Apache-2.0. The `prover/` crate was scaffolded from the risc0 project template and
  carries an additional Apache-2.0 notice at [`prover/LICENSE`](prover/LICENSE) covering that
  risc0-derived build scaffolding. Copyright (c) RISC Zero, Inc.
- **RustCrypto `sha2`** — MIT/Apache-2.0, pinned to an immutable commit, routed through the risc0
  SHA-256 accelerator (byte-identical output).

## Our own code

- The Utreexo UTXO accumulator (`accumulator/`), the host/prover driver (`prover/host/`), the guest
  glue (`prover/methods/guest/`), and the coordinator (`coordinator/`) are original work under the
  root MIT licence.

Nothing here overrides the terms of the components' own licences; consult each upstream project for
the authoritative text.

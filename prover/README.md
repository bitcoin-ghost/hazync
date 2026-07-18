# Hazync prover

The zkVM prover: it runs Bitcoin Core's **real, unmodified** consensus code inside RISC0 and emits a
STARK proof that a block — or a whole range of blocks folded together — is valid. This directory holds
the guest program (the code that runs in the zkVM), the host driver (builds witnesses, drives proving,
verifies receipts), and the test scaffolding.

> **Just want to prove a block?** Follow **[`../CONTRIBUTING.md`](../CONTRIBUTING.md)** — it takes you
> from nothing to your first proof with one build command. This README is for driving the prover
> directly. The full operator's guide is **[`PROVING.md`](../docs/PROVING.md)**.

## Layout

```
prover/
├── host/            driver: builds witnesses, proves, verifies receipts (target/release/host)
├── methods/
│   ├── guest/       the zkVM program — verify_input.cpp wraps real Core; main.rs drives
│   │                block validation, the accumulator, recursion, and range-folding
│   └── build.rs     compiles the guest for riscv32im
├── fetch_block.py   explorer scaffold: fetch a block's witness without running a node
├── rangecluster.sh  parallel range-fold across N GPUs → one genesis-anchored receipt
├── cluster.sh       multi-box proving helper
├── evidence/        logs from the demonstrated runs (block 741000, genesis→550, BIP68, …)
└── block_*.json     sample witnesses used by the regression suite
```

## Build

From the repo root, `provision-vps.sh` installs the RISC0 toolchain, CUDA 12.6, and Bitcoin Core v28,
then builds the prover. To build here directly once the toolchain is present:

```bash
cargo build --release --features cuda      # GPU build (CUDA 12.6)
cargo build --release                       # CPU build (execute-mode validation; proving is slow)
```

## Common commands

All commands read witnesses from `HAZYNC_WITNESS_DIR`.

```bash
# validate fast (execute mode, no proving) — full consensus + accumulator, seconds/block
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=550 ./target/release/host check-ibd

# PROVE the recursive chain from genesis (real STARK receipts) + a tip extension
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=170 HAZYNC_TIP=3 ./target/release/host prove-ibd

# PARALLEL range-fold across N GPUs → one genesis-anchored receipt → verify
NGPU=2 LO=1 HI=550 HAZYNC_WITNESS_DIR=/w bash rangecluster.sh

# single block: check-full (validate) / prove-full (STARK), with HAZYNC_BLOCK
HAZYNC_BLOCK=741000 HAZYNC_WITNESS_DIR=/w ./target/release/host prove-full

# verify any receipt someone else made (no GPU needed)
./target/release/host verify-any proof.bin

# regression + the replayable adversarial suite
./target/release/host regress
./target/release/host adversarial
```

See [`PROVING.md`](../docs/PROVING.md) for the complete command reference (SNARK-wrap, tip protocol,
the archive-node witness bridge) and [`SECURITY.md`](../SECURITY.md) for what the adversarial suite
checks.

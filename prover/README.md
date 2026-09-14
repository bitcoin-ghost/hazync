# Hazync prover

The zkVM prover: it runs Bitcoin Core's **real** consensus code inside RISC0 — Core's consensus logic
unmodified, with the patches in [`../patches/`](../patches/) listed in the [root README](../README.md#what-is-actually-compiled-from-core) — and emits a
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
├── fetch_block_rpc.py  the same from an archive node; how the block_*.json fixtures are made
├── rangecluster.sh  parallel range-fold across N GPUs → one genesis-anchored receipt
├── cluster.sh       ONE box, N GPUs: a block's chunks in waves (CUDA_VISIBLE_DEVICES), then agg-chunks
├── ci_*.sh          CI gates: negative tests, boundary tests, verify-any, SNARK verify / prove
├── tools/           Python analysis scripts over the block fixtures
├── testdata/        C++ test sources (chainparams, FFI adversarial, retarget differential), retarget
│                    vectors, boundary fixtures, and the Groth16 fixtures in snark/
├── evidence/        logs from the demonstrated runs (block 741000, genesis→550, BIP68, …)
└── block_*.json     real mainnet blocks: the HAZYNC_BLOCK input to check-full / prove-full, also read
                     by ci_negative_tests.sh, ../fuzz-native/negative-corpus.sh and tools/*.py
                     (`host regress` builds block 170 in code and reads none of them)
```

## Build

From the repo root, `provision-vps.sh` installs the RISC0 toolchain, clones the Bitcoin Core v28.0 and
libsecp256k1 v0.5.1 sources (it does not install a node), applies `patches/`, then builds the prover.
With `GPU=1` it also installs CUDA 12.8 (`HAZYNC_CUDA_VER` pins another 12.x). To build here directly
once the toolchain is present:

```bash
cargo build --release --features cuda      # GPU build (CUDA 12.x; the kernels do not build on 13)
cargo build --release                       # CPU build (execute-mode validation; proving is slow)
```

## Common commands

`check-ibd`, `prove-ibd` and `rangecluster.sh` read per-block witnesses from `HAZYNC_WITNESS_DIR`.
`check-full` / `prove-full` read one block from the JSON file named by `HAZYNC_BLOCK`;
`prove-range-bridge` reads an archive bundle from `HAZYNC_BRIDGE_OUT`; `verify-any`, `regress` and
`adversarial` need neither.

```bash
# validate fast (execute mode, no proving) — full consensus + accumulator, seconds/block
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=550 ./target/release/host check-ibd

# PROVE the recursive chain from genesis (real STARK receipts) + a tip extension
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=170 HAZYNC_TIP=3 ./target/release/host prove-ibd

# PARALLEL range-fold across N GPUs → one genesis-anchored receipt → verify
NGPU=2 LO=1 HI=550 HAZYNC_WITNESS_DIR=/w bash rangecluster.sh

# single block: check-full (validate) / prove-full (STARK); HAZYNC_BLOCK is a FILE PATH
HAZYNC_BLOCK=block_741000.json ./target/release/host check-full

# one block from an archive bundle (what the proof party does), bundle at bundles/bundle_500.json
HAZYNC_BRIDGE_OUT=bundles HAZYNC_OUT=range_500.bin ./target/release/host prove-range-bridge 500

# verify any receipt someone else made (no GPU needed)
./target/release/host verify-any proof.bin

# regression + the replayable adversarial suite
./target/release/host regress
./target/release/host adversarial
```

See [`PROVING.md`](../docs/PROVING.md) for the complete command reference (SNARK-wrap, tip protocol,
the archive-node witness bridge) and [`SECURITY.md`](../SECURITY.md) for what the adversarial suite
checks.

# Hazync — proving, end to end

This is the operator's guide to the real proving commands. Everything below is implemented,
hardened, and demonstrated on real mainnet data — single blocks, recursive chains, the parallel
range-fold, and tip operation. (For *joining the live party* rather than driving the prover
directly, see [`CONTRIBUTING.md`](../CONTRIBUTING.md). For the soundness posture and the nine rounds of
adversarial hardening, see [`SECURITY.md`](../SECURITY.md).)

Proving is RAM- and GPU-heavy. Build on a provisioned box (`provision-vps.sh`); a small WSL2 machine
can run *execute-mode* validation but not proving. GPU proving needs CUDA **12.6** (the script
installs it) and the `cuda` feature.

```
GPU=1 REPO_DIR=$PWD ./provision-vps.sh
cd prover && cargo build --release --features cuda
```

## Fast validation (no proving, no GPU)

Execute mode runs the full consensus path (real `VerifyScript` + `CheckTransaction` + no-inflation +
PoW + retarget + merkle + subsidy + weight + sigops + maturity + locktime + BIP68 + the accumulator
transition) and panics on any violation — so a clean run == every rule passed. Use it as a cheap
pre-flight and as the regression/soundness gate.

```
./target/release/host regress        # block-170 consensus regression (self-contained)
./target/release/host adversarial    # adversarial soundness suite — every known hole must REJECT
HAZYNC_BLOCK=block_741000.json ./target/release/host check-full   # one block, full consensus
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=550 ./target/release/host check-ibd
```

`regress` and `adversarial` are also wired into CI (`.github/workflows/adversarial.yml`).

## Single-block proof (real STARK receipt)

```
HAZYNC_BLOCK=block_741000.json ./target/release/host prove-full   # monolithic
HAZYNC_BLOCK=block_741000.json HAZYNC_CHUNKS=16 ./target/release/host prove-seg   # segmented
```

`prove-full` validates the whole block in one guest run and commits a `ChainState`. `prove-seg`
splits the block's inputs into chunks, proves each chunk's scripts in parallel (`chunk_prove`, mode
4), then aggregates (`aggregate`, mode 5) — each chunk commits a per-input binding digest
(`input_bind`) that the aggregation re-checks against the block's own input, so a chunk cannot
substitute a different spend or weaker flags. Both verify the receipt against `METHOD_ID` and assert
`self_id == METHOD_ID`.

## Recursive chain + tip (implemented and hardened)

The recursion is real: each step commits `self_id` into its journal, asserts the previous step
recursed against the same id and that `w.height == prev.height + 1`, and tags the journal with a
domain constant; the host verifier asserts the final `self_id == METHOD_ID`. The adversarial
`prove-chain-bad` command folds a block against a corrupted `self_id` and confirms it is rejected.

```
./target/release/host prove-chain        # fold real blocks 170 -> 171 -> 172 (IVC), verify the tip
./target/release/host prove-chain-bad     # adversarial: wrong self_id must be REJECTED
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=170 HAZYNC_TIP=3 ./target/release/host prove-ibd
./target/release/host verify-chain <chain.bin>   # verify a ChainState receipt + PIN its anchor to genesis
```

A verified chain-tip receipt attests every block from the genesis anchor to the tip without the
blocks. `verify-chain` (round 8 / S5) is the chain-track analogue of `verify-range`'s genesis pin: it
verifies the STARK, asserts `self_id == METHOD_ID` + the `KIND_CHAIN` tag, and pins the committed
`anchor_id` to `dsha256(genesis_anchor)` — so a receipt built on a fabricated (non-genesis) anchor is
rejected, and a checkpoint-anchored demo proof correctly fails it. See [`SOUNDNESS.md`](SOUNDNESS.md)
§3/§3c for the recursion + anchor argument (and the verifier's obligation to pin `self_id == METHOD_ID`
and the anchor).

## Parallel range-fold (the backfill path)

Genesis→tip is embarrassingly parallel: prove each block as a self-contained range, then merge
adjacent ranges in a log-depth tree. A fold verifies two range receipts and checks the full seam
(tip, UTXO roots+leaves, difficulty, and the MTP window) meets.

```
./target/release/host prove-range <n>                 # one block as range [n..n]
./target/release/host fold-range <left.bin> <right.bin> <out.bin>
./target/release/host verify-range <out.bin>          # verify + PIN the leftmost boundary to genesis
./target/release/host verify-any <bin>                 # verify without the genesis pin (coordinator's per-range check)
NGPU=2 LO=1 HI=550 HAZYNC_WITNESS_DIR=/w bash rangecluster.sh   # multi-GPU fan-out -> one genesis-anchored receipt
```

`verify-range` pins the full genesis in-boundary; `verify-any` (used by the coordinator on each
submitted range) additionally emits a full boundary digest so ranges can be chained on the same seam
invariant the guest fold enforces. See [`HAZYNC_ARCHITECTURE.md`](HAZYNC_ARCHITECTURE.md).

## Prover reliability: the risc0 segment-boundary retry

The pinned prover (`risc0-circuit-rv32im` 4.0.5) has a preflight bug: for ~10% of blocks a proving
segment packs right up to its `2^po2` boundary and the assertion `cycles <= 1 << segment.po2` overflows,
so the prove **panics** — on CPU *and* CUDA (it's the shared witgen, not backend-specific). This is a
liveness bug only: it never produces a wrong proof, it just fails to produce one for those blocks.

The fix is host-side, so it does **not** affect `METHOD_ID`: `host` reads `HAZYNC_SEG_PO2` for the
executor's `segment_limit_po2` on **every** prove path — single-block
`prove-range`/`prove-range-bridge`, `fold-range`, `prove-chunk`, chunk-aggregate, the replay path, the
IVC chain step, and the SNARK wrap — and the CLI retries a failed prove *or fold* with progressively
**smaller** segments (`HAZYNC_SEG_PO2` 21→20→19→18), which repartition the work and clear the boundary.

As of v0.10.0 the **default depends on the backend**: **21 for a `cuda` build, 20 (the risc0 default) for
CPU**. Bigger segments mean fewer of them and so less recursion/fold overhead — measured on an L40S,
block 130000 proves in **23.0s at po2 21 vs 24.4s at 20** (~6% faster), flat to 22. But a po2-21 segment
also needs roughly **twice the working memory**, and that is a pure cost on CPU, where the speed win was
never measured: an 11 GB box proving block 170 (2.3M cycles) peaked at **8.7 GB RSS and went to swap**.
Swapping is not a prove *failure*, so the retry ladder never fires — it just crawls. Hence the split.
`host seg-po2` prints the effective default, and the coordinator CLI starts its ladder there rather than
at a hardcoded rung (which on CPU would waste a duplicate attempt at the size that just failed).
`HAZYNC_SEG_PO2` overrides either way.
Normal workloads prove at the default; only the affected ~10% fall back, and the receipt is identical
either way. **Releases:** the current release is **v0.10.0** at `METHOD_ID 3f52baff` (libsecp's ecmult
window raised to its measured optimum — see `reproduce/METHOD_ID`), on top of the real-Core `pow.cpp`
difficulty retarget carved into the guest, every consensus constant sourced from Core's own
`chainparams.cpp`, and the v0.9.0 witness wire format. Both the
**`hazync-host-x86_64-linux-gnu`** CPU binary and the multi-arch **CUDA** binary embed this guest and
carry the segment-retry across all prove paths plus the earlier fixes (P2SH sigop count, BIP30 bridge
handling, in-block-spend leaf fix, and the round-9 R-1 coinbase-vin hardening). A deeper fix (patching
risc0's segment reservation so no retry is needed) is future work.

## The guest image id (METHOD_ID) & reproducibility

A proof is verified **against a guest image id** — `METHOD_ID`, a hash of the exact zkVM guest ELF.
`verify-any`/`verify-range` call `r.verify(METHOD_ID)`: the receipt only checks out against the *same*
guest that produced it.

That id is a hash of the **whole guest build**, not just the source: Bitcoin Core's version, the
riscv32 C/C++ toolchain, the RISC0 Rust toolchain, and the `risc0` crate versions all feed into it. So
**two people who build the host from source can get different `METHOD_ID`s** — and a host whose id
differs from the one that produced a published proof will report:

```
STARK verification FAILED.
This is almost certainly a guest image-id (METHOD_ID) MISMATCH, not a bad proof:
  this host's METHOD_ID: <hex>
```

**This is a build mismatch, not an invalid proof.** Print your host's id and compare:

```
./target/release/host method-id
```

### Reproducing the canonical `METHOD_ID`

The build is **reproducible**: everything that feeds the id is pinned (`risc0-zkvm`/`risc0-build` `=3.0.5`,
the `rzup` toolchain `rust 1.94.1`/`cpp 2024.1.5`/`cargo-risczero`+`r0vm 3.0.5`, Bitcoin Core `v28.0`,
secp256k1 `v0.5.1`, and a committed `Cargo.lock`) **and** the build runs at fixed paths inside a
container, so the absolute build location can't leak into the ELF. Reproduce it on any machine:

```
docker build -f reproduce/Dockerfile -t hazync-repro .
docker run --rm hazync-repro          # prints METHOD_ID — must equal reproduce/METHOD_ID
```

The canonical id is checked in at [`reproduce/METHOD_ID`](../reproduce/METHOD_ID) and verified
reproducible bit-for-bit across machines (local + GitHub CI); the `reproducible-image-id` CI job asserts
every build still matches it. A from-source host built **outside** the container may differ (absolute
paths bake into the ELF) — that's the mismatch `verify-any` warns about; build via the container to get
the canonical guest. The **coordinator** is also an independent check: it re-verifies every submitted
proof before recording it, so a bad proof never lands on the board.

The guest image id is **independent of the host proving backend** — the CPU and CUDA host binaries embed
the same guest ELF — so the CPU-only `reproduce/Dockerfile` attests the canonical id (`3f52baff`,
the current guest) for **both** the CPU and CUDA release binaries.

## SNARK wrap (optional, for cheap universal verification)

Wrap a tip/range STARK to Groth16 (~200–300 B, verifiable on a phone or on-chain). The capability is
validated (block 170); applying it to the chain/range output is future work.

```
./target/release/host prove-snark
```

## Acceleration note

In the sound build only SHA-256 is routed to the RISC0 accelerator (patch 0002); ECDSA and Schnorr
run through the compiled, unmodified `libsecp256k1`, unaccelerated. Speeding up the EC verify is open
work — the k256 substitution was **removed from the guest** (2026-07-19; it reintroduced the
reimplementation question), and the bigint2 field-mul intercept was prototyped and disproven (~10%
slower). The guest is pure Core; acceleration analysis in [`ACCELERATION.md`](ACCELERATION.md).

## Building the release binaries (maintainer notes)

**Both binaries are built by `prover/build-release.sh`**, which encodes everything below:

```bash
./prover/build-release.sh cpu     # -> dist/hazync-host-x86_64-linux-gnu
./prover/build-release.sh cuda    # -> dist/hazync-host-x86_64-linux-gnu-cuda
```

It runs the build at `HOME=/root` inside `ubuntu:22.04`, passes the multi-arch NVCC flags, and prints
`METHOD_ID` / `seg-po2` / glibc / SASS coverage next to the canonical id so a mismatch is obvious before
you publish. The repo is bind-mounted, so `prover/target` persists and a host-only change rebuilds just
the host crate rather than the guest and CUDA kernels again. The notes below are what it automates.

Both published binaries must print the canonical `METHOD_ID` (`3f52baff…`); the guest id is reproducible,
the host bytes need not be. Build both in a container so the binary links against **glibc 2.34** (Ubuntu
22.04) and runs on older distros:

- **CPU** — `reproduce/Dockerfile` *is* the release build: its base was pinned to `ubuntu:22.04` (glibc 2.34)
  in v0.8.0, so the canonical reproducibility container and the portable CPU binary are now the same
  artifact (no `sed` step any more). `docker build -t hazync-repro -f reproduce/Dockerfile .`, then copy
  `/hazync-zkvm/prover/target/release/host` out of the image.
- **CUDA** — the same container plus `--features cuda`, run with `--gpus all`. **Do not build natively if the
  box has CUDA 13**: risc0-sys 1.5.0's kernels don't compile under CUDA 13 (`nvcc -arch=native` fails). Use
  the **CUDA 12** toolchain inside the `ubuntu:22.04` container, and pass multi-arch flags so the binary
  isn't pinned to one GPU:
  `NVCC_APPEND_FLAGS="-gencode arch=compute_80,code=sm_80 -gencode arch=compute_86,code=sm_86 -gencode arch=compute_89,code=sm_89 -gencode arch=compute_90,code=sm_90 -gencode arch=compute_90,code=compute_90"`.
  Confirm coverage with `cuobjdump --list-elf <host>` (expect `sm_80 sm_86 sm_89 sm_90`).

Before publishing, smoke-test each binary: `host method-id` (== `reproduce/METHOD_ID`), `host
bundle-roundtrip-test`, and `host verify-any` on a served proof. `prover/e2e_bundle_test.sh` runs the full
served-bundle → prove → verify path on a spend-block — the coverage the in-memory suite doesn't reach.

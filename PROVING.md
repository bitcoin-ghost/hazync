# Hazync — proving, end to end

This is the operator's guide to the real proving commands. Everything below is implemented,
hardened, and demonstrated on real mainnet data — single blocks, recursive chains, the parallel
range-fold, and tip operation. (For *joining the live party* rather than driving the prover
directly, see [`CONTRIBUTING.md`](CONTRIBUTING.md). For the soundness posture and the seven rounds of
adversarial hardening, see [`SECURITY.md`](SECURITY.md).)

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
```

A verified chain-tip receipt attests every block from the genesis anchor to the tip without the
blocks. See [`SOUNDNESS.md`](SOUNDNESS.md) §3 for the recursion argument (and the verifier's
obligation to pin `self_id == METHOD_ID` and the genesis anchor).

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
invariant the guest fold enforces. See [`SCALING.md`](SCALING.md).

## SNARK wrap (optional, for cheap universal verification)

Wrap a tip/range STARK to Groth16 (~200–300 B, verifiable on a phone or on-chain). The capability is
validated (block 170); applying it to the chain/range output is future work.

```
./target/release/host prove-snark
```

## Acceleration note

In the sound build only SHA-256 is routed to the RISC0 accelerator (patch 0002); ECDSA and Schnorr
run through the compiled, unmodified `libsecp256k1`, unaccelerated. Speeding up the EC verify is open
work — the k256 substitution (`patches/0003`) is measured but reintroduces the reimplementation
question and is **not** applied in the sound build; the bigint2 field-mul intercept was prototyped and
disproven (~10% slower). See [`ACCELERATION.md`](ACCELERATION.md).

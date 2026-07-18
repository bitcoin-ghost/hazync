# Join the Hazync Proof Party

You prove one block of Bitcoin's history on your own machine, sign it, and submit it. Your name goes on the board at https://bitcoinghost.org/hazync, and the proof is public for anyone to download and check. This guide takes you from nothing to your first proof.

## What you need

- A Linux machine, Ubuntu 22.04 or 24.04. A cloud GPU box works well.
- An NVIDIA GPU if you can get one. It makes proving about twenty times faster. No GPU still works for the early blocks, it is just slower.
- Roughly 16 GB of RAM and 80 GB of disk for the one-time build.
- About 25 minutes for the build the first time. After that, an early block proves in seconds on a GPU; a full range of blocks takes proportionally longer.

## Minimum spec, by what you want to do

| You want to | You need |
|-------------|----------|
| Verify a proof someone else made | Any Linux box, no GPU, a couple of GB of RAM |
| Prove early or small blocks | 16 GB RAM to build, an NVIDIA GPU ideally, 80 GB disk |
| Prove big modern blocks (thousands of inputs) | 64 GB+ RAM and a serious GPU |
| Run your own coordinator | A cheap 2-core, 4 GB box, no GPU |

## Step 1: get the code

```
git clone https://github.com/bitcoin-ghost/hazync
cd hazync
```

## Step 2: build it, one command

This installs everything it needs (the RISC0 toolchain, CUDA, Bitcoin Core) and compiles the prover. It takes about 25 minutes, so leave it running.

```
GPU=1 REPO_DIR=$PWD ./provision-vps.sh
```

No GPU? Drop the `GPU=1`:

```
REPO_DIR=$PWD ./provision-vps.sh
```

## Step 3: install the signing library

```
sudo apt install -y python3-cryptography
```

## Step 4: set your name and point at the party

```
export COORD_URL=https://bitcoinghost.org/hazync
export HAZYNC_HOST=$PWD/prover/target/release/host
export WITNESS_DIR=$PWD/w
./coordinator/hazync id yourname
```

Your name can be anything. It is tied to a signing key the tool makes for you and keeps in `~/.hazync`, so nobody else can claim your blocks. Back that folder up if you care about keeping the same identity.

## Step 5: prove a range

```
./coordinator/hazync run
```

That asks the coordinator for the next open range near the frontier, fetches the witnesses it needs, proves it on your machine, signs the receipt, and submits it. The coordinator re-verifies your proof, and when the tool prints a line starting with `✓`, your name is on the board at https://bitcoinghost.org/hazync.

The party proves the chain forward from genesis, so you take a range near the current frontier. You can see the open ranges on the board. Want a specific one? Pass it:

```
./coordinator/hazync run 0-999
```

Proving a whole range takes a while (each block is proved, then the receipts are folded together). Prove as many ranges as you like, just run it again. An arbitrary far-future block is not something a fresh contributor can prove: the coordinator serves witnesses for its window near the frontier, and proving needs the chain up to that point.

## Just want to check a proof, not make one?

You never have to trust the party. Every verified proof is public at `https://bitcoinghost.org/hazync/api/proof/<block>`, and you can also click any green block on the board to download it. Then check it yourself, no GPU needed:

```
# download block 170's proof receipt
curl https://bitcoinghost.org/hazync/api/proof/170 -o proof_170.bin

# verify it against the same real Bitcoin Core consensus code
./prover/target/release/host verify-any proof_170.bin
```

If it prints a line starting with `RANGE-OK`, the proof is genuine. That is the whole point of this project: every proof is public and anyone can check it, no trust required.

The `.bin` is a **binary STARK receipt** (a RISC0 proof, a few hundred KB), not text — opening it in a text editor just shows gibberish, which is expected. You *use* it with `verify-any`, you don't read it.

If `verify-any` prints `STARK verification FAILED ... METHOD_ID MISMATCH` instead of `RANGE-OK`, that is **not** a bad proof — your host was built from a different guest than made the proof, so their image ids differ. Run `./prover/target/release/host method-id` to see yours. To get the canonical guest that matches the published proofs, build via the reproducible container (`docker build -f reproduce/Dockerfile .`) — its id is pinned in [`reproduce/METHOD_ID`](reproduce/METHOD_ID). See [`docs/PROVING.md`](docs/PROVING.md).

## If something breaks

- The build runs out of memory on a small box. It needs about 16 GB of RAM. Build on a bigger box and copy `prover/target/release/host` across to a smaller one, the built binary runs anywhere.
- CUDA proving needs CUDA 12.6 specifically. The provision script installs it. If you already have a newer CUDA, the script points the build at 12.6.
- Anything else, open an issue on the repo.

## Running your own party

The coordinator (`coordinator/`) is optional and reusable. If you want to run your own proving effort, for a testnet, another chain, or a private run, `coordinator/deploy/RUNBOOK.md` walks through standing one up. Contributors point their `COORD_URL` at your coordinator, and their proofs land in your ledger, not ours. Each coordinator is its own island: separate ledger, separate frontier, separate stored proofs. The proofs themselves are universal, so anyone can verify a proof from any party, but coordinators do not share state with each other.

## Reviewing the code

If you would rather try to break it than prove blocks, that is the most valuable thing you can do. `SECURITY.md` is the map of where the soft spots are.

# Join the Hazync Proof Party

You prove one block of Bitcoin's history on your own machine, sign it, and submit it. Your name goes on the board at https://bitcoinghost.org/hazync, and the proof is public for anyone to download and check. This guide takes you from nothing to your first proof.

## What you need

- A Linux machine (x86-64), Ubuntu 24.04+ (glibc 2.39+) for the prebuilt binaries. A cloud GPU box works well.
- An NVIDIA GPU + the CUDA 12.6 runtime for fast proving. No GPU still works (the CPU binary proves the early blocks, just slower).
- **No build.** Grab the prebuilt binary below — proving an early block takes seconds on a GPU.

## Minimum spec, by what you want to do

| You want to | You need |
|-------------|----------|
| Verify a proof someone else made | Any Linux x86-64 box, no GPU, a couple of GB of RAM — download the CPU binary, done |
| Prove early or small blocks | An NVIDIA GPU + CUDA 12.6 (or the CPU binary, slower) |
| Prove big modern blocks (thousands of inputs) | 64 GB+ RAM and a serious GPU |
| Run your own party (coordinator + archive bridge) | An always-on box with a full `bitcoind` — ~8-core, 32 GB, 1 TB+ NVMe |

## Step 1: get the prover (no build needed)

Download the prebuilt prover — it's the **canonical guest**, so the coordinator accepts your proofs. Needs an NVIDIA GPU + the CUDA 12.6 runtime.

```
# the prover binary (canonical guest, GPU)
curl -L -o host https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu-cuda
chmod +x host
# the contributor CLI + signing library
curl -L -o hazync https://raw.githubusercontent.com/bitcoin-ghost/hazync/main/coordinator/hazync
chmod +x hazync
sudo apt install -y python3-cryptography
```

**No GPU?** Use the CPU binary instead (`hazync-host-x86_64-linux-gnu`) — it proves too, just slower.

> **Building from source instead?** You *must* build the **canonical guest** (via `reproduce/Dockerfile`, or the pinned inputs at fixed paths — see the repo README) so your `METHOD_ID` matches `reproduce/METHOD_ID`. If it doesn't, the coordinator rejects every proof you submit (`METHOD_ID` mismatch). The prebuilt binary above sidesteps this entirely.

## Step 2: set your name and point at the party

```
export COORD_URL=https://bitcoinghost.org/hazync
export HAZYNC_HOST=$PWD/host
export WITNESS_DIR=$PWD/w
./hazync id yourname
```

Your name can be anything. It is tied to a signing key the tool makes for you and keeps in `~/.hazync`, so nobody else can claim your blocks. Back that folder up if you care about keeping the same identity.

## Step 3: prove

```
./hazync run              # picks the next open range near the frontier
./hazync run 0-999        # or a specific range
./hazync run 5            # or a single block
```

`run` claims the work, fetches the witnesses it needs, proves it on your machine, signs the receipt, and submits it. The coordinator re-verifies your proof, and when the tool prints a `✓`, your name is on the board at https://bitcoinghost.org/hazync. Prove as many as you like — just run it again.

Proving a whole range takes a while (each block is proved, then the receipts are folded together). Prove as many ranges as you like, just run it again. An arbitrary far-future block (past the coordinator's served window) is not something a fresh contributor can prove yet: the coordinator's archive bridge serves a ready-made witness for each block in its window near the frontier, and you prove a block directly from that witness — no node of your own, no chain replay.

## Just want to check a proof, not make one?

You never have to trust the party. Every verified proof is public — fetch any proven block from `https://bitcoinghost.org/hazync/api/proof/<block>` (e.g. `/api/proof/1`). Then check it yourself, no GPU needed and **no build required** — grab the prebuilt verifier from the release (Linux x86-64, glibc 2.39+ / Ubuntu 24.04+):

```
# 1. get the prebuilt host (it IS the canonical guest — the same one that made the proofs)
curl -L -o host https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu
chmod +x host

# 2. download a proof (by block number) and verify it against real Bitcoin Core consensus code
curl https://bitcoinghost.org/hazync/api/proof/1 -o proof.bin
./host verify-any proof.bin
```

If it prints a line starting with `RANGE-OK`, the proof is genuine. That is the whole point of this project: every proof is public and anyone can check it, no trust required. (Building the `host` from source works too — see the repo README — but the prebuilt binary is the one-step path.)

The `.bin` is a **binary STARK receipt** (a RISC0 proof, a few hundred KB), not text — opening it in a text editor just shows gibberish, which is expected. You *use* it with `verify-any`, you don't read it.

If `verify-any` prints `STARK verification FAILED ... METHOD_ID MISMATCH` instead of `RANGE-OK`, that is **not** a bad proof — your host was built from a different guest than made the proof, so their image ids differ. The prebuilt binary above avoids this (it's the canonical guest). If you built from source, run `host method-id` to see yours and reproduce the canonical id with the container (`docker build -f reproduce/Dockerfile .`) — it's pinned in [`reproduce/METHOD_ID`](reproduce/METHOD_ID). See [`docs/PROVING.md`](docs/PROVING.md).

## If something breaks

- `./host: cannot execute` or a `GLIBC` error — the prebuilt binaries need glibc 2.39+ (Ubuntu 24.04+). On an older distro, build from source (canonical guest — see the repo README) or run in the reproducible container.
- The CUDA prover needs the **CUDA 12.6 runtime**. If proving fails to find CUDA, install it (`cuda-toolkit-12-6`) or use the CPU binary (slower, no CUDA).
- The coordinator rejects your proof with a `METHOD_ID` mismatch — you're proving with a non-canonical guest. Use the prebuilt binary, or reproduce the canonical id with `reproduce/Dockerfile`.
- Anything else, open an issue on the repo.

## Running your own party

The coordinator (`coordinator/`) is optional and reusable. If you want to run your own proving effort, for a testnet, another chain, or a private run, `coordinator/deploy/RUNBOOK.md` walks through standing one up. Contributors point their `COORD_URL` at your coordinator, and their proofs land in your ledger, not ours. Each coordinator is its own island: separate ledger, separate frontier, separate stored proofs. The proofs themselves are universal, so anyone can verify a proof from any party, but coordinators do not share state with each other.

## Reviewing the code

If you would rather try to break it than prove blocks, that is the most valuable thing you can do. `SECURITY.md` is the map of where the soft spots are.

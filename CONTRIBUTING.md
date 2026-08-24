# Join the Hazync Proof Party

You prove one block of Bitcoin's history on your own machine, sign it, and submit it. Your name goes on the board at https://bitcoinghost.org/hazync, and the proof is public for anyone to download and check. This guide takes you from nothing to your first proof.

## What you need

- A Linux machine (x86-64), Ubuntu 22.04+ (glibc 2.34+) for the prebuilt binaries. A cloud GPU box works well.
- An NVIDIA GPU and a driver recent enough for it. **No CUDA toolkit or runtime install is needed** — the prebuilt prover links only `libcuda.so.1`, which ships with the driver, so a stock cloud GPU image works as it comes (CUDA 13 included). No GPU still works (the CPU binary proves the early blocks, just slower).
- **No build.** Grab the prebuilt binary below — proving an early block takes seconds on a GPU.

## Minimum spec, by what you want to do

| You want to | You need |
|-------------|----------|
| Verify a proof someone else made | Any Linux x86-64 box, no GPU, a couple of GB of RAM — download the CPU binary, done |
| Prove early or small blocks | An NVIDIA GPU + its driver (or the CPU binary, slower) |
| Prove big modern blocks (thousands of inputs) | 64 GB+ RAM and a serious GPU |
| Run your own party (coordinator + archive bridge) | An always-on box with a full `bitcoind` — ~8-core, 32 GB, 1 TB+ NVMe |

**Proving memory.** A proving *segment* is the unit that has to fit in memory, and its size is
`HAZYNC_SEG_PO2` — each step up doubles the working set. The binary picks a sensible default (21 on the
CUDA build, where bigger segments prove ~6% faster; 20 on CPU, where the extra memory buys nothing), and
the CLI automatically retries smaller if a prove fails. But **swapping is not a failure**, so if your box
is RAM-tight it will crawl rather than fall back — set `HAZYNC_SEG_PO2=20` (or 19) explicitly. For scale:
even block 170, a 2.3M-cycle toy block, peaks around 8.7 GB at po2 21.

## Step 1: get the prover (no build needed)

Download the prebuilt prover — it's the **canonical guest**, so the coordinator accepts your proofs. Needs an NVIDIA GPU and its driver; no CUDA toolkit.

```
# the prover binary (canonical guest, GPU)
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu-cuda
chmod +x hazync-host-x86_64-linux-gnu-cuda
# the contributor CLI and the fleet launcher — both SIGNED release artifacts, covered by
# SHA256SUMS.txt.asc. run-workers.sh used to come unsigned from raw.githubusercontent, on the line
# right after this comment claimed a signature; it is the script that launches your fleet, so it is
# now attested like everything else you run.
curl -fLO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-worker
curl -fLO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-run-workers.sh
ln -sf hazync-run-workers.sh run-workers.sh   # shorter to type; the real file keeps the asset name,
                                              # which is what SHA256SUMS.txt lists
chmod +x hazync-worker hazync-run-workers.sh
ln -sf hazync-worker hazync      # a shorter name to type; the real file keeps the asset name
sudo apt install -y python3-cryptography
```

(`run-workers.sh` is optional — it keeps several workers going for you. Keep it **next to** the
CLI; it finds `hazync-worker` or `hazync` beside itself. `MODE=fold` runs folders instead of
provers, `MODE=mixed` runs both.)

**No GPU?** Use the CPU binary instead (`hazync-host-x86_64-linux-gnu`) — it proves too, just slower.

> **Both binaries prove in-process — there is nothing else to install.** Up to and including
> v0.12.1 the CPU binary did not: it was built without risc0's `prove` feature, so it shelled out to
> `r0vm`, a ~109 MB VM the release does not ship, and the first `hazync run` died on a bare
> `No such file or directory (os error 2)`. The CUDA build linked its prover in, so the GPU path
> worked and the "no GPU still works" path did not. Fixed — the CPU binary now links the prover in
> too. You only need `r0vm` if you deliberately set `RISC0_PROVER=ipc`.

**Keep the asset filenames.** `SHA256SUMS.txt` lists them, so downloading with `-LO` means
`sha256sum -c --ignore-missing SHA256SUMS.txt` verifies what you got. Renaming on download
(`-o host`) makes it report *"no file was verified"* — which looks like a signature problem and is
not. Symlink or rename afterwards if you want shorter names.

Take the CLI from the **release**, not from a raw source URL: it holds your ed25519 signing key and
decides what gets submitted under your name, so it is the artifact most worth having a signature on.
Both it and the prover are covered by the signed `SHA256SUMS.txt`.

Want to check what you downloaded? Verify its SHA256 + PGP signature — see [`SECURITY.md`](SECURITY.md#verifying-releases) — or, stronger, run `./host method-id` and confirm it matches `reproduce/METHOD_ID`.

> **Building from source instead?** You *must* build the **canonical guest** (via `reproduce/Dockerfile`, or the pinned inputs at fixed paths — see the repo README) so your `METHOD_ID` matches `reproduce/METHOD_ID`. If it doesn't, the coordinator rejects every proof you submit (`METHOD_ID` mismatch). The prebuilt binary above sidesteps this entirely.

## Step 2: set your name and point at the party

```
# Both of these are OPTIONAL now — the CLI defaults to the public party and finds the prover
# beside itself. Set them only if you are pointing somewhere else.
export COORD_URL=https://bitcoinghost.org/hazync
export HAZYNC_HOST=$PWD/hazync-host-x86_64-linux-gnu-cuda
export WITNESS_DIR=$PWD/w
./hazync id yourname
```

Your name can be anything. It is tied to a signing key the tool makes for you and keeps in `~/.hazync`, so nobody else can claim your blocks. Back that folder up if you care about keeping the same identity.

**Back it up now, not later.** Losing the box loses the key, and the blocks it proved stay under a name
you can no longer sign for. This has already happened once on the public board: 29,669 blocks sit under
an identity whose key died with a deleted machine, and nothing can recover them.

### Moved to a new box, or rebuilt one?

If you still have the old `key.hex`, move its blocks onto the new machine's identity:

```
./hazync rotate /path/to/old/key.hex
```

Both keys sign one message, so this can neither take someone else's blocks nor push yours onto them.
The old secret is read locally to produce that signature and is never transmitted; the coordinator only
ever sees two public keys and two signatures. Your totals merge and the leaderboard shows one row.

The old key is not retired. If you forgot a box somewhere and it is still proving, its work resolves
onto your current identity rather than being lost.

**Before you prove, run a pre-flight:**

```
./hazync selftest
```

It confirms your prover is present, its guest `METHOD_ID` matches the coordinator (the common trap — a different guest build has every proof rejected), and it can verify a real board proof end to end. Better to catch a setup problem now than after a long prove.

## Step 3: prove

```
./hazync run              # takes the coordinator's suggestion
./hazync run 5            # or name any block you like
```

**Nothing is reserved and nothing is allocated.** The coordinator will *suggest* a block that would be
most useful next, but you may prove any height you want and submit it — the suggestion is advisory.
That means a worker that dies mid-block leaves nothing behind to expire or hand back, and a block
nobody can prove no longer blocks anyone else.

`run` fetches the witness it needs, proves it on your machine, signs the receipt, and submits it. The coordinator re-verifies your proof, and when the tool prints a `✓`, your name is on the board at https://bitcoinghost.org/hazync. Prove as many as you like — just run it again.

**Leaving it running, or running several at once?** One `run` proves one block and exits, so use the
supplied loop — it keeps N workers going and, importantly, refuses to start if your guest id doesn't
match the coordinator's:

```
HAZYNC_HOST=./hazync-host-x86_64-linux-gnu-cuda ./run-workers.sh 4          # 4 parallel workers, logs in ~/hazync-workers
./run-workers.sh 4 --stop
```

That pre-flight matters: a worker on the wrong guest id proves happily and has **every** submission
rejected, burning GPU hours for nothing. That is exactly what happens if you keep an old binary after a
re-baseline, so the script blocks it rather than letting it run.

Prove as many blocks as you like — just run it again.

**Any height the bridge has reached is provable, not only ones near the frontier.** The coordinator
serves a ready-made witness for every block it holds, so you prove a block directly from that witness
with no node of your own and no chain replay. Blocks the bridge has not reached yet have no witness
to serve, and `run` tells you so before it starts rather than after a long prove.

## Step 4: two other ways to help, both cheaper than proving

Proving is the expensive job. These two turn what has already been proved into something a stranger
can check in one download, and both cost seconds rather than minutes.

```
./hazync fold          # combine two adjacent proven ranges into one wider proof
./hazync fold 20       # do twenty of them
```

The board holds a receipt per block. Folding merges adjacent ones — `[100..199] + [200..299]` becomes
`[100..299]` — and the result can be folded again with *its* neighbour. It is a tree, so any adjacent
pair may be folded in any order and any number of people may do it at once. Nothing is allocated: the
coordinator suggests pairs whose fold does not exist yet, and if someone beats you to one your
submission is simply discarded. A fold verifies two receipts and checks the seam; it does not re-prove
anything.

```
./hazync spine 5       # absorb the next 5 chunks into the genesis-anchored head
```

The **spine** is the one artifact that says *"every block from genesis to N is valid"*. It advances by
absorbing the next adjacent chunk rather than being rebuilt, so it is complete after every step. This
is the only job in the whole system that has to happen in order — anchoring means starting at block 1
— so one machine at a time makes progress on it, and duplicate effort is harmless but wasted.

Absorbing wide chunks is far better than absorbing single blocks: it is **one fold either way**. So
folding first (above) and then absorbing is how the spine catches up quickly.

Whoever advances the spine cannot corrupt it. Every absorption is re-verified against the canonical
guest id and pinned to genesis, and because every per-block receipt is retained, anyone can rebuild
the spine from scratch without re-proving anything.

## Got more than one card?

Proving one block can be split across machines, so a second card halves the wall-clock rather than
just doubling how many blocks you can work on at once. Both halves of the job divide: the segments a
block's proof is made of, and the recursion that folds them back into one receipt.

One machine is the coordinator; every other card connects to it and takes work.

```
# coordinator
HAZYNC_PORT=9110 ./host seg-serve

# each worker, anywhere that can reach it
HAZYNC_WORKER_ID=w1 ./host seg-connect <coordinator-host>:9110
```

Workers need nothing but the segment in front of them. A worker **cannot forge a receipt** — every
one is verified on arrival, and joins assert that the two receipts being merged actually meet — so it
can only fail to return work, never corrupt it. That is why it is safe to point strangers' cards at
your coordinator.

Measured on two matched L40S: **2.03x**, with a verifying receipt each run.

⚠ **One prove per card.** At the default segment size a prove peaks at about 40.6 GB of VRAM, so a
48 GB card holds exactly one. Running two on the same card will exhaust it, and it would buy roughly
3% even if it fit.

Details, the aggregate case, and every knob: [`docs/SEGMENT_DISTRIBUTION.md`](docs/SEGMENT_DISTRIBUTION.md).

## Just want to check a proof, not make one?

You never have to trust the party. Every verified proof is public — fetch any proven block from `https://bitcoinghost.org/hazync/api/proof/<block>` (e.g. `/api/proof/1`). Then check it yourself, no GPU needed and **no build required** — grab the prebuilt verifier from the release (Linux x86-64, glibc 2.34+ / Ubuntu 22.04+):

```
# 1. get the prebuilt host (it IS the canonical guest — the same one that made the proofs)
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu
chmod +x hazync-host-x86_64-linux-gnu
ln -sf hazync-host-x86_64-linux-gnu host   # shorter to type; the real file keeps its asset name,
                                           # which is what SHA256SUMS.txt lists

# 2. download a proof (by block number) and verify it against real Bitcoin Core consensus code
curl -f https://bitcoinghost.org/hazync/api/proof/1 -o proof.bin
./host verify-any proof.bin
```

(Want to check the verifier binary itself before trusting it? SHA256 + PGP signature steps are in [`SECURITY.md`](SECURITY.md#verifying-releases); or confirm `./host method-id` equals `reproduce/METHOD_ID`.)

If it prints a line starting with `RANGE-OK`, the proof is genuine — with one nuance: `verify-any` attests *that single step* (this block is a correct consensus transition between its stated boundaries); `verify-chain` / `verify-range` (or the board's connected frontier) are what pin it back to the genesis anchor. That is the whole point of this project: every proof is public and anyone can check it, no trust required. (Building the `host` from source works too — see the repo README — but the prebuilt binary is the one-step path.)

The `.bin` is a **binary STARK receipt** (a RISC0 proof, a few hundred KB), not text — opening it in a text editor just shows gibberish, which is expected. You *use* it with `verify-any`, you don't read it.

If `verify-any` prints `STARK verification FAILED ... METHOD_ID MISMATCH` instead of `RANGE-OK`, that is **not** a bad proof — your host was built from a different guest than made the proof, so their image ids differ. The prebuilt binary above avoids this (it's the canonical guest). If you built from source, run `host method-id` to see yours and reproduce the canonical id with the container (`docker build -f reproduce/Dockerfile .`) — it's pinned in [`reproduce/METHOD_ID`](reproduce/METHOD_ID). See [`docs/PROVING.md`](docs/PROVING.md).

## If something breaks

- `./host: cannot execute` or a `GLIBC` error — the prebuilt binaries need glibc 2.34+ (Ubuntu 22.04+). On an older distro (pre-22.04, e.g. Ubuntu 20.04 / Debian 11), build from source (canonical guest — see the repo README) or run in the reproducible container.
- The CUDA prover needs **only the NVIDIA driver** (`libcuda.so.1`), not a CUDA toolkit or runtime.
  Verified on a stock Ubuntu 24.04 cloud GPU image shipping **CUDA 13.2** and no `nvcc` at all: the
  binary reported the canonical `METHOD_ID`, passed `selftest` including a real GPU prove, and went
  on to prove several hundred blocks. `ldd` shows the whole story:

  ```
  $ ldd hazync-host-x86_64-linux-gnu-cuda | grep -i cuda
  	libcuda.so.1 => /lib/x86_64-linux-gnu/libcuda.so.1
  ```

  CUDA **12** is a BUILD requirement, not a runtime one: risc0-sys 1.5.0's kernels do not compile
  under CUDA 13 (see `prover/build-release.sh`). `provision-vps.sh` installs `cuda-toolkit-12-6` for
  that reason. If you are downloading the prebuilt binary you are not building, and it does not
  apply to you. If proving genuinely cannot find the GPU, check the driver with `nvidia-smi`.
- The coordinator rejects your proof with a `METHOD_ID` mismatch — you're proving with a non-canonical guest. Use the prebuilt binary, or reproduce the canonical id with `reproduce/Dockerfile`.
- Anything else, open an issue on the repo.

## Running your own party

The coordinator (`coordinator/`) is optional and reusable. If you want to run your own proving effort, for a testnet, another chain, or a private run, `coordinator/deploy/RUNBOOK.md` walks through standing one up. Contributors point their `COORD_URL` at your coordinator, and their proofs land in your ledger, not ours. Each coordinator is its own island: separate ledger, separate frontier, separate stored proofs. The proofs themselves are universal, so anyone can verify a proof from any party, but coordinators do not share state with each other.

## Reviewing the code

If you would rather try to break it than prove blocks, that is the most valuable thing you can do. `SECURITY.md` is the map of where the soft spots are.

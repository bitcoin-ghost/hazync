# Groth16 SNARK fixtures

Two wrapped range proofs, used by `prover/ci_snark_verify.sh` to gate Groth16 **verification** on every
push (#23).

Regenerated 2026-08-04 under canonical guest `b62d2a60…` (audit #5 guest guards — see
`reproduce/METHOD_ID`), which superseded `b161735a…`. A proof carries its guest id inside it, so a
re-baseline cannot be absorbed by editing anything: the pair has to be re-proved and re-wrapped, and
until it is, `ci_snark_verify.sh`, `ci_verify_any.sh` and `verifier-wasm/test-parity.sh` fail — as
they should, since a verifier pinned to the new id genuinely cannot accept a proof made by the old one.

The 2026-08-02 regeneration before it was forced the same way, by the `b161735a…` lineage changing
every leaf hash.

**They were [1..1000] and are now [1..8].** The originals were folded from 1000 GPU block-proofs; on
CPU that is ~9.6 days. Range length is irrelevant to what these fixtures test — `[1..8]` is exactly as
genesis-anchored as `[1..1000]`, and the negative is a single mid-chain block either way. The
filenames changed with the content rather than being kept, because `fold_1000.snark` holding a proof
of eight blocks would be a fixture lying about itself.

| File | Range | Size | Must |
|---|---|---|---|
| `fold_8.snark` | `[1..8]`, genesis-anchored | 2,353 B | **VERIFY** |
| `neg500.snark` | `[500..500]`, valid but **not** genesis-anchored | 6,145 B | **be REJECTED, on the genesis pin** |

The negative fixture is the important one. A verifier that accepts everything passes a positive-only
test, so the gate asserts not merely that the non-genesis range is rejected but that it is rejected
*because of the anchor* — not a missing file, not a parse error. That assertion is what stops a smaller,
more shareable artifact from checking less than the receipt it replaces.

Note `neg500.snark` (5,633 B) is **larger** than `fold_8.snark` (1,841 B) despite covering one block
instead of eight: a genesis-anchored range has an empty in-boundary, while a mid-chain range commits two
populated Utreexo root vectors. Size tracks boundary content, not range length.

## They can only be verified by a CANONICAL host

The gate runs in the **`reproducible-image-id`** job, inside the fixed-path container — not in
`soundness-suite`. That is not incidental: a RISC0 guest image id absorbs the build's `$HOME/.cargo`
paths, so a host built on a CI runner (`$HOME=/home/runner`) has a **non-canonical** METHOD_ID and
rejects these fixtures. The first attempt put the check in `soundness-suite` and failed with

```
proof's guest id:      <the canonical id>
this host's METHOD_ID: <a different id, from the runner's own build paths>
```

which reads like a broken proof and is actually a build-path mismatch. Verify these with a container
build, never with an ad-hoc one.

(Deliberately not quoting the runner's actual id here: `scripts/check-versions.sh` fails the build on
any guest id in the docs that is neither canonical nor listed in `reproduce/METHOD_ID`, and it should —
a stale or foreign id in documentation is exactly the drift it exists to catch. It caught this.)

## These are tied to a METHOD_ID

Both were wrapped under guest image id `4722cec826239c1b3a3598bbac284376cc7b920c9bcd9863fa34f40c9ea7bbae`
(audit #5). **A guest re-baseline invalidates them** — the verifier will reject proofs made against a
different image id, and the gate will fail loudly, which is intended. Regenerate them as part of the
re-baseline, alongside the other artifacts listed in `coordinator/deploy/RUNBOOK.md`.

## Regenerating

Needs a host binary with `snark-wrap` **and** a working Groth16 backend. Today that means a **CPU**
build — Groth16 crashes in sppark on every CUDA build we ship (#20).

**Use a CANONICAL host binary, but do NOT run the wrap inside the container.** Both halves of that
sentence cost a run on 2026-08-04:

* The binary must be the container-built one, or the fixtures are wrapped against a non-canonical id
  (the id absorbs `$HOME/.cargo` paths) and CI rejects them. The id is baked in at BUILD time, so
  copying the binary out of the image and running it on the host keeps it canonical — verify with
  `host method-id` before trusting it.
* `snark-wrap` **shells out to Docker** for the Groth16 compression. Run it inside the reproduce
  container and it dies with `groth16 compress: Please install docker first` — there is no Docker
  inside that container. Extract the binary and wrap on the host, where the daemon is reachable.

```sh
# get a canonical host binary OUT of the image, then confirm it is canonical
cid=$(docker create hazync-repro) && docker cp "$cid:/hazync-zkvm/prover/target/release/host" ./host
docker rm "$cid" && chmod +x ./host && ./host method-id      # must equal reproduce/METHOD_ID

# block proofs. Use the BRIDGE path: bundles make each block O(1) instead of replaying the
# accumulator from genesis, which is what makes a mid-chain block like 500 tractable at all.
for h in 1 2 3 4 5 6 7 8 500; do curl -s "$COORD/api/witness/$h" > bundles/bundle_$h.json; done
HAZYNC_BRIDGE_OUT=bundles HAZYNC_OUT=range_$h.bin ./host prove-range-bridge $h   # ~8 min each, CPU

# positive: fold [1..8] as an ALIGNED tree (1+2, 3+4, 5+6, 7+8 -> ... -> [1..8])
./host fold-range range_1.bin range_2.bin f12.bin             # ... 7 folds, ~3 min each
./host snark-wrap fold8.bin fold_8.snark                      # ~74 s
./host verify-snark fold_8.snark                              # must PASS

# negative: wrap any single mid-chain range
./host snark-wrap range_500.bin neg500.snark                  # ~75 s
./host verify-snark neg500.snark                              # must FAIL, naming the genesis pin
```

⚠ **One prove at a time.** A CPU prove holds ~4.7 GB. Three concurrent on a 12 GB box drove available
memory to 276 MB and the kernel killed two of them — and a Docker OOM kill takes the container's
stdout with it, so they failed with ZERO-BYTE logs that look like a mystery rather than exhaustion.

⚠ Sanity-check the mid-chain receipt with `verify-any`, **not** `verify-range`. `verify-range` asserts
the range starts at block 1, so it panics `range must start at block 1` on the negative fixture —
which says the checker was pointed at the wrong thing, not that the receipt is bad.

## What this does NOT cover

Only **verification**. Whether `snark-wrap` still *produces* a valid proof is not exercised here,
because a CPU Groth16 prove is far too slow for per-push CI. That half is `prover/ci_snark_prove.sh`,
which is opt-in. Groth16 shipped broken from v0.8.0 precisely because neither half was tested, so the
gap is narrowed here, not closed.

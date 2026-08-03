# Groth16 SNARK fixtures

Two wrapped range proofs, used by `prover/ci_snark_verify.sh` to gate Groth16 **verification** on every
push (#23). Regenerated 2026-08-02 under canonical guest `dfc9eeda…` (cshims.c hardening + multi_check
docs — see `reproduce/METHOD_ID`),
which changes every leaf hash and therefore invalidated the previous pair.

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

Both were wrapped under guest image id `dfc9eeda7a5cc19f5091a642c1d88cde6fb153259d94be7e317ee20efb41206f`
(v0.10.0). **A guest re-baseline invalidates them** — the verifier will reject proofs made against a
different image id, and the gate will fail loudly, which is intended. Regenerate them as part of the
re-baseline, alongside the other artifacts listed in `coordinator/deploy/RUNBOOK.md`.

## Regenerating

Needs a host binary with `snark-wrap` **and** a working Groth16 backend. Today that means a **CPU**
build — Groth16 crashes in sppark on every CUDA build we ship (#20):

```sh
# positive: fold a genesis-anchored range, then wrap it
host fold-range r1.bin r2.bin f.bin        # ... log-depth tree up to [1..1000]
host snark-wrap fold_1000.bin fold_8.snark
host verify-snark fold_8.snark          # must PASS

# negative: wrap any single mid-chain range
host snark-wrap ~/.hazync/receipts/500.bin neg500.snark
host verify-snark neg500.snark             # must FAIL, naming the genesis pin
```

## What this does NOT cover

Only **verification**. Whether `snark-wrap` still *produces* a valid proof is not exercised here,
because a CPU Groth16 prove is far too slow for per-push CI. That half is `prover/ci_snark_prove.sh`,
which is opt-in. Groth16 shipped broken from v0.8.0 precisely because neither half was tested, so the
gap is narrowed here, not closed.

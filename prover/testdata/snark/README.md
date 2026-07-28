# Groth16 SNARK fixtures

Two wrapped range proofs, used by `prover/ci_snark_verify.sh` to gate Groth16 **verification** on every
push (#23). Both were produced by the CPU Groth16 path on 2026-07-28 — see
`prover/evidence/fold_and_snark_wrap_1_1000.txt` for the run that generated them.

| File | Range | Size | Must |
|---|---|---|---|
| `fold_1000.snark` | `[1..1000]`, genesis-anchored | 3,441 B | **VERIFY** |
| `neg500.snark` | `[500..500]`, valid but **not** genesis-anchored | 5,633 B | **be REJECTED, on the genesis pin** |

The negative fixture is the important one. A verifier that accepts everything passes a positive-only
test, so the gate asserts not merely that the non-genesis range is rejected but that it is rejected
*because of the anchor* — not a missing file, not a parse error. That assertion is what stops a smaller,
more shareable artifact from checking less than the receipt it replaces.

Note `neg500.snark` is **larger** than `fold_1000.snark` despite covering one block instead of a
thousand: a genesis-anchored range has an empty in-boundary, while a mid-chain range commits two
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

Both were wrapped under guest image id `3f52baff7e7d4adaa328b832d6f15fffb1b35968b6636760f9d50e045bbae67e`
(v0.10.0). **A guest re-baseline invalidates them** — the verifier will reject proofs made against a
different image id, and the gate will fail loudly, which is intended. Regenerate them as part of the
re-baseline, alongside the other artifacts listed in `coordinator/deploy/RUNBOOK.md`.

## Regenerating

Needs a host binary with `snark-wrap` **and** a working Groth16 backend. Today that means a **CPU**
build — Groth16 crashes in sppark on every CUDA build we ship (#20):

```sh
# positive: fold a genesis-anchored range, then wrap it
host fold-range r1.bin r2.bin f.bin        # ... log-depth tree up to [1..1000]
host snark-wrap fold_1000.bin fold_1000.snark
host verify-snark fold_1000.snark          # must PASS

# negative: wrap any single mid-chain range
host snark-wrap ~/.hazync/receipts/500.bin neg500.snark
host verify-snark neg500.snark             # must FAIL, naming the genesis pin
```

## What this does NOT cover

Only **verification**. Whether `snark-wrap` still *produces* a valid proof is not exercised here,
because a CPU Groth16 prove is far too slow for per-push CI. That half is `prover/ci_snark_prove.sh`,
which is opt-in. Groth16 shipped broken from v0.8.0 precisely because neither half was tested, so the
gap is narrowed here, not closed.

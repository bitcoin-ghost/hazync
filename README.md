# Hazync

Verify the whole Bitcoin chain from one small proof, instead of re-executing every block from genesis. The groundwork for a full node that syncs in minutes.

Hazync runs Bitcoin Core's **own, unmodified** consensus code — `interpreter.cpp`, `SignatureHash`, `libsecp256k1` — inside a zero-knowledge VM, and proves every block is valid. The proofs fold: genesis to tip collapses into one succinct receipt you can check in milliseconds. No re-execution, no trusting peers.

It is **not a reimplementation.** Every other validity-proof effort rewrites consensus and inherits the question "does your rewrite match Core in every edge case, forever?" Hazync runs Core, so it doesn't.

## Verify a proof

No GPU, no build, no clone. Linux x86-64:

```bash
curl -L -o host https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu && chmod +x host
curl https://bitcoinghost.org/hazync/api/proof/1 -o proof.bin
./host verify-any proof.bin        # → RANGE-OK
```

Every proof on the [board](https://bitcoinghost.org/hazync) is public. The binary is the canonical guest — rebuild it yourself (`reproduce/Dockerfile`) and you get the same image id, byte for byte (`reproduce/METHOD_ID`).

## What it proves

A verified chain proof attests: **every block from genesis to the tip is valid under Core consensus, the UTXO set equals the committed root, and the work is as committed** — with no re-execution. That covers scripts of every type, real ECDSA and Schnorr through `libsecp256k1`, no inflation, proof-of-work and difficulty, merkle and witness commitments, weight, sigops, and the locktime/BIP rules, under Core's exact flags. The one non-Core piece is the Utreexo UTXO accumulator (`accumulator/`) — our code, exhaustively tested.

## How it works

```
per-input script proof ── block proof ── chain fold ── tip / range proof
 (real VerifyScript)     (all rules)    (recursion)   (one receipt)
```

Prove each block with real Core in the zkVM, fold blocks recursively into one receipt, verify the receipt. Witnesses come from a full node during its own sync. Details in [`docs/`](docs/).

## Status

Built and demonstrated on real mainnet data — single blocks, recursive chains, tip operation, parallel backfill; every tip hash and UTXO count matches mainnet. Hardened across **seven rounds** of adversarial self-audit.

Not done yet: the full genesis→tip backfill (a GPU-compute campaign, not new capability) and an external audit. This is early-stage research, shared for review. Trying to break it is the most useful thing you can do — [`SECURITY.md`](SECURITY.md) maps the soft spots.

## More

- New to zero-knowledge proofs? [`EXPLAINER.md`](EXPLAINER.md) — plain English.
- Prove blocks, join the party: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Soundness and the audit record: [`SECURITY.md`](SECURITY.md)
- How it's built: [`docs/`](docs/)

---

*Bitcoin Core is BSD/MIT; the patches are portability-only and change no consensus logic.*

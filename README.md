# Hazync

**Bitcoin Core's own consensus code, proven in a zero-knowledge VM.** Hazync runs the *actual, unmodified* `interpreter.cpp`, `SignatureHash`, and `libsecp256k1` — not a reimplementation — inside a zkVM, and proves each block valid under real consensus. Every other validity-proof effort rewrites consensus and inherits the question "does your rewrite match Core in every edge case, forever?" Hazync runs Core, so it doesn't.

The proofs fold: verified block by block, a stretch of the chain collapses into one succinct receipt you check in a moment — no re-execution, no trusting peers. The end it builds toward: **verify the whole chain from a single proof — a full node that syncs in minutes.**

**Status — proving the chain, live.** The board shows the frontier climbing from genesis, in the open — [watch it](https://bitcoinghost.org/hazync). We're not at the tip yet; this is early-stage research, shared for review. Real Bitcoin Core code in a zkVM is the hard part, and it's done and audited ([`SECURITY.md`](SECURITY.md), [`AUDIT_2026-07.md`](AUDIT_2026-07.md)) — the rest is the compute campaign to prove the chain forward.

## Verify a proof

No GPU, no build, no clone. Linux x86-64, glibc 2.39+ (Ubuntu 24.04+; on older distros build from source — see [`docs/PROVING.md`](docs/PROVING.md) — or run the binary inside `reproduce/Dockerfile`):

```bash
curl -L -o host https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu && chmod +x host
curl https://bitcoinghost.org/hazync/api/proof/1 -o proof.bin
./host verify-any proof.bin        # → prints a line starting with RANGE-OK
```

Every proof on the [board](https://bitcoinghost.org/hazync) is public. The binary is the canonical guest — rebuild it yourself (`reproduce/Dockerfile`) and you get the same image id, byte for byte (`reproduce/METHOD_ID`).

## What it proves

A verified chain proof attests: **every block from genesis to the tip is valid under Core consensus, the UTXO set equals the committed root, and the work is as committed** — with no re-execution. That covers scripts of every type, real ECDSA and Schnorr through `libsecp256k1`, no inflation, proof-of-work and difficulty, merkle and witness commitments, weight, sigops, and the locktime/BIP rules, under Core's exact flags. The one non-Core piece is the Utreexo UTXO accumulator (`accumulator/`) — our code, exhaustively tested.

## How it works

```
per-input script proof ── block proof ── chain fold ── tip / range proof
 (real VerifyScript)     (all rules)    (recursion)   (one receipt)
```

Prove each block with real Core in the zkVM, fold blocks recursively into one receipt, verify the receipt. Witnesses are served ready-made by an archive-node bridge (a full node that drives the UTXO accumulator forward once and emits each block's witness), so a prover needs no node of its own and no chain replay. Details in [`docs/`](docs/).

## Status

Built and demonstrated on real mainnet data — single blocks, recursive chains, tip operation, parallel backfill; every tip hash and UTXO count matches mainnet. Hardened across **eight rounds** of adversarial self-audit — the latest a five-reviewer completeness+verifier pass ([`AUDIT_2026-07.md`](AUDIT_2026-07.md)).

Still to come: the full genesis→tip proving campaign and an external audit. Trying to break it is the most useful thing you can do — [`SECURITY.md`](SECURITY.md) maps the soft spots.

## More

- New to zero-knowledge proofs? [`EXPLAINER.md`](EXPLAINER.md) — plain English.
- Prove blocks, join the party: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Soundness and the audit record: [`SECURITY.md`](SECURITY.md) · latest round: [`AUDIT_2026-07.md`](AUDIT_2026-07.md)
- How it's built: [`docs/`](docs/)

## Licence

MIT (see [`LICENSE`](LICENSE)). The guest compiles in Bitcoin Core and libsecp256k1 (both MIT); the
patches are portability-only and change no consensus logic. `prover/` carries an additional Apache-2.0
notice for the risc0-derived build scaffolding. Third-party components are attributed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

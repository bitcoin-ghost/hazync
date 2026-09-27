# Hazync

[![blocks proven](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.hazync.org%2Fapi%2Fstate&query=%24.progress.proven&label=blocks%20proven&color=1f6feb&style=flat-square&cacheSeconds=300)](https://hazync.org/)
[![chain tip](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.hazync.org%2Fapi%2Fstate&query=%24.progress.tip&label=chain%20tip&color=30363d&style=flat-square&cacheSeconds=300)](https://hazync.org/)
[![share of chain](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.hazync.org%2Fapi%2Fstate&query=%24.progress.pct&label=%2525%20of%20chain&color=8957e5&style=flat-square&cacheSeconds=300)](https://hazync.org/)
[![provers](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fapi.hazync.org%2Fapi%2Fstate&query=%24.progress.contributors&label=provers&color=238636&style=flat-square&cacheSeconds=300)](CONTRIBUTING.md)

**Sync Bitcoin from a proof, not from trust.**

---

## The problem

To use Bitcoin without trusting anyone, your node downloads the entire chain and re-checks every
signature ever made — hundreds of gigabytes and hours of work, and it grows every day. Most people
don't. They use someone else's node and take its word for it.

That cost is the reason Bitcoin gets less decentralised over time.

## The idea

Check the work **once**, produce a proof that it was done correctly, and let everyone else verify
that proof in milliseconds.

This isn't new. What's hard is the honest version of it.

## Why this one is different

Every other attempt rewrites Bitcoin's consensus rules in a language a prover can handle. That
rewrite then has to match Bitcoin Core **exactly, in every edge case, forever** — including the bugs
Core can never fix because they're now consensus. Nobody can prove that about a rewrite.

Hazync doesn't rewrite them. It compiles **Bitcoin Core's actual C++** — the real
`interpreter.cpp`, the real `SignatureHash`, the real `libsecp256k1` — to RISC-V and runs it inside a
zero-knowledge VM.

If Core accepts a block, so does this. Not because the behaviour was carefully matched, but because
it is the same code.

## Check one yourself

A proof verifies in **tens of milliseconds**, in a browser, with nothing sent anywhere:

**[hazync.org/explorer/#verify](https://hazync.org/explorer/#verify)**

Or from the command line — download `hazync-verify-x86_64-linux-gnu` from the
[latest release](https://github.com/hazync/hazync/releases/latest) and point it at any proof. The
[full walkthrough](docs/DESIGN_OVERVIEW.md#check-one-yourself-it-takes-about-thirty-seconds) takes
about thirty seconds.

## Where it actually stands

Honest, because overclaiming here would be the whole problem again:

**Works today**

- Bitcoin Core's consensus code proven inside a zkVM, on real blocks from every era
- An open **proof party**: anyone proves any block on their own GPU and signs it with their own key
- Proofs combine — a chain of them reaches back to genesis
- A node holding only stripped blocks validated them against a proof
- A near-tip block (966,256, 9,079 inputs) proven in **544 seconds on 27 rented GPUs**
- Reproducible builds: the same source gives the same program ID, bit for bit

**Not yet**

- **No independent audit.** Nobody outside the project has reproduced the build or reviewed the
  accumulator, which is the one component that isn't Core's code
- The chain is **~13% proven** (see the badge — it's live)
- Syncing a node from a proof is shown at low height, not at scale
- Following the chain's tip continuously is the current target, not a result

## Help

The most valuable contribution is **trying to break it** — specifically, finding a case where
Hazync says a block is valid and Bitcoin Core would not. [`docs/EXTERNAL_REVIEW.md`](docs/EXTERNAL_REVIEW.md)
says where an hour is worth most.

| | |
|---|---|
| **Prove blocks** on your GPU | [`CONTRIBUTING.md`](CONTRIBUTING.md) |
| **Plain-English explanation** | [`docs/EXPLAINER.md`](docs/EXPLAINER.md) |
| **How it works, in detail** | [`docs/DESIGN_OVERVIEW.md`](docs/DESIGN_OVERVIEW.md) |
| **Specification & soundness** | [`docs/SPEC.md`](docs/SPEC.md), [`docs/SOUNDNESS.md`](docs/SOUNDNESS.md) |
| **Every document** | [`docs/README.md`](docs/README.md) |
| **Discussion** | [Delving Bitcoin](https://delvingbitcoin.org/t/running-cores-real-consensus-code-inside-a-zkvm/2811) |

Running it costs real money in GPU time. [What it costs and how to help pay for
it](https://hazync.org/donate/).

## Licence

MIT (see [`LICENSE`](LICENSE)). The guest compiles in Bitcoin Core and libsecp256k1 (both MIT) with
the patches in `patches/`, none of which changes Core's consensus logic. `prover/` carries an
additional Apache-2.0 notice for the risc0-derived build scaffolding. Third-party components are
attributed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

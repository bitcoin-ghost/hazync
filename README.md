# Hazync

**Bitcoin's consensus rules, proven with Bitcoin Core's own code, inside a zero-knowledge VM.**

Not a reimplementation of the rules. The actual `interpreter.cpp`, the actual `SignatureHash`, the
actual `libsecp256k1`, compiled to RISC-V and executed inside a prover. Every prior validity-proof
effort inherits the question *"does your rewrite match Core in every edge case, forever?"* This one
does not have to answer it.

**Discussion:** [Proving Bitcoin — running Core's real consensus code inside a zkVM](https://delvingbitcoin.org/t/running-cores-real-consensus-code-inside-a-zkvm/2811)
(Delving Bitcoin). That post is the long-form argument, the measurements, and the list of things
that are *not* covered. Adversarial review is what this needs most; see
[`docs/EXTERNAL_REVIEW.md`](docs/EXTERNAL_REVIEW.md) for where it is worth spending an hour.

---

### Check one yourself. It takes about thirty seconds.

```bash
curl -fLO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-verify-x86_64-linux-gnu
chmod +x hazync-verify-x86_64-linux-gnu
curl -f https://bitcoinghost.org/hazync/api/spine/proof -o proof.bin
./hazync-verify-x86_64-linux-gnu proof.bin
```

```
>>> SNARK RANGE PROOF [1..N] VERIFIED — genesis-anchored
```

A **1.7 MB** binary, and a proof that every block from genesis to N is valid under Core's real
consensus rules, checked in **milliseconds** on a laptop, with no node, no peers, no chain data and
nothing to trust. [Or do it in your browser](https://bitcoinghost.org/hazync/verify/), where the
verifier is a WebAssembly module served in **~285 KB** gzipped (1,066,001 bytes raw) that peaks at
**1.9 MiB of memory**, small enough for a phone.

N is however far the anchored proof currently reaches, and it grows as the board does. Swap the URL
for `/api/proof/<height>` to be handed one block instead and check that alone.

That is the whole idea. Proving is expensive and done by a few; **verifying is cheap and done by
everyone.**

---

### The proofs combine

Two adjacent proofs fold into one, and the result folds again. A stretch of chain collapses into a
single succinct receipt, the same size whether it covers two blocks or two hundred thousand. One
receipt, one check, no re-execution.

The end this builds toward: **a node that verifies the whole chain from a single proof, instead of
re-executing seventeen years of it.**

### Where it actually is

The hard part is done: real Core consensus code, proving real mainnet blocks, validated across the
segwit, taproot, big-block and pre-BIP34 eras. The guest image id is **reproducible**; CI rebuilds
it from scratch and checks it matches.

What remains is scale, and there is now an answer to it. **Proving a block divides across
machines**: segments prove independently, the recursion that folds them is a tree rather than a
chain, and both halves scale. Measured end to end on three GPUs: a near-tip block goes from **4.9
hours on one card to 100 minutes on three — 2.92x**, against a ceiling of 3.0x, with a byte-identical
receipt at every configuration. Roughly thirty cards would bring it under ten minutes. A worker needs
only the segment in front of it, so it cannot forge a receipt, only fail to return one.
[`docs/SEGMENT_DISTRIBUTION.md`](docs/SEGMENT_DISTRIBUTION.md).

The board **resets with this release**, as it does at every re-baseline: guest `1d6c3792` (2026-08-23)
supersedes `b62d2a60` (2026-08-04, audit #5), because `validate_block` was restructured to do
per-transaction work once per transaction rather than once per input: 3,455 M cycles down to 955 M,
with Core's consensus code untouched. Changing the guest at all is what costs a reset: the id is what
makes a proof checkable, so a proof made under the old guest cannot verify under the new one.

The board is open and anyone can join. Whatever figure it shows is not seventeen years of
accumulated work; it is what has been re-proved since that re-baseline.
[The live board](https://bitcoinghost.org/hazync.html) is the only place a current figure belongs,
and a genesis-anchored proof is downloadable there whether or not anyone is proving today. Proving
Bitcoin's real cryptography is deliberately expensive, and that cost *is* the security argument.

**Two independent external reviews ran in August 2026 ([`SECURITY.md`](SECURITY.md), rounds
10 and 11). Neither found a way to make the guest ACCEPT an invalid chain.** Both found real defects
anyway, and both landed on the same two places as the residual risk: the C++ shim layer compiled
into the guest, and the accumulator. Everything they raised is fixed or tracked. Those were
AI-assisted code reviews, not a commissioned professional audit; that has still not happened.

The most serious finding of that period was **ours, not theirs. Internal audit #3 found a
canonical-chain break that would have made the guest REJECT a valid chain**, stalling any
from-genesis prover at block 91841, roughly 10% in. Blocks 91842 and 91880 duplicate coinbases that
were still unspent, which is the reason BIP30 exists, and the new non-membership check had no
exception for them. Fixed in v0.15.0, with the real blocks now in the fixture set.

[**Watch the board**](https://bitcoinghost.org/hazync) · [**Join in**](CONTRIBUTING.md) ·
[**Read the spec**](docs/SPEC.md)

---

## What is actually compiled from Core

The script interpreter (`interpreter.cpp`), `SignatureHash`, `CheckTransaction`,
`ComputeMerkleRoot`, the transaction/weight/sigop machinery, the difficulty retarget (`pow.cpp`'s
`CalculateNextWorkRequired`, driven through the real `CBlockIndex`), and `libsecp256k1`, all unmodified,
with two narrow portability shims and zero consensus-logic changes.

What is *not* compiled from Core is a thin, self-contained slice: the subsidy halving schedule and the
script-flag activation heights, each differentially tested against Core (the flag schedule is proven a
sound superset of `GetBlockScriptFlags`). Even the compiled retarget is belt-and-suspenders:
cross-checked against the actual on-chain `nBits` at every one of the 476 mainnet retargets.

## Verifying, in detail

The command above is the whole story for most people. The rest is in
[`docs/PROVING.md`](docs/PROVING.md); these are the parts that trip people up.

The file it downloads is the **spine**: the current genesis-anchored head, one receipt attesting
that every block from 1 to N is valid under Core's own consensus code. `/api/proof/<n>` serves a
single block instead, and that one exits **`2`**, not `0`, because one mid-chain block is not
genesis-anchored. That is the correct answer, not a failure.

Exit codes: `0` genesis-anchored, `2` valid but a mid-chain segment (most proofs on the board are
segments), `1` the proof is actually bad.

`-LO` keeps the asset's own filename, which is what `SHA256SUMS.txt` lists. Renaming on download
makes `sha256sum -c` say *"no file was verified"*, which looks like a broken signature and is not.

Prebuilt binaries need Linux x86-64, glibc 2.34+. An `aarch64` build is published too, so "a phone
can check this" is a file you can download rather than a claim. On an older distro, run the same
binary in a container rather than rebuilding.

**The ~187 MB host** does everything else: proving, and `verify-any`, which accepts any single proof
rather than only genesis-anchored ones.

```bash
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu
chmod +x hazync-host-x86_64-linux-gnu
./hazync-host-x86_64-linux-gnu verify-any proof.bin   # prints a line starting with RANGE-OK
```

`RANGE-OK` means the STARK checks out and the receipt proves block *n* is a correct consensus
transition between its stated boundaries. That those boundaries chain back to the real genesis is
what the connected chain establishes; a single isolated proof attests its own step, not the whole
history.

The binary is the canonical guest. Rebuild it yourself (`reproduce/Dockerfile`) and you get the same
image id, byte for byte (`reproduce/METHOD_ID`).

## What it proves

A verified chain proof attests: **every block from genesis to the tip is valid under Core consensus,
the UTXO set equals the committed root, and the work is as committed**, with no re-execution. That
covers scripts of every type, real ECDSA and Schnorr through `libsecp256k1`, no inflation,
proof-of-work and difficulty, merkle and witness commitments, weight, sigops, and the locktime/BIP
rules, under Core's exact flags. The one non-Core piece is the Utreexo UTXO accumulator, our own
code (the proven version is the guest's `prover/methods/guest/src/utreexo.rs`), differentially
fuzzed ~900k executions against a reference model (`audit-fuzz/`). Both August 2026 reviewers
independently named it one of the two most likely places for a hidden bug, and one found real panic
paths in the reference crate, now fixed. It still has not had a commissioned audit, and it remains
the thing we most want outside eyes on.

## How it works

```
per-input script proof ── block proof ── chain fold ── tip / range proof
 (real VerifyScript)     (all rules)    (recursion)   (one receipt)
```

Prove each block with real Core in the zkVM, fold blocks recursively into one receipt, verify the
receipt. Witnesses are served ready-made by an archive-node bridge (a full node that drives the UTXO
accumulator forward once and emits each block's witness), compactly encoded and de-duplicated per
transaction, so a big block's witness is tens of MB smaller, so a prover needs no node of its own
and no chain replay. Details in [`docs/`](docs/).

## Status

Built and demonstrated on real mainnet data: single blocks, recursive chains, tip operation,
parallel backfill; every tip hash and UTXO count matches mainnet. Empirically validated across the
segwit, taproot, big-block and pre-BIP34 eras.

Two external reviews ran in August 2026, findings, fixes and what each could *not* verify are
recorded in [`SECURITY.md`](SECURITY.md). Still to come: the full genesis→tip proving campaign and a
commissioned audit. Trying to break it is the most useful thing you can do,
[`SECURITY.md`](SECURITY.md) maps the soft spots.

## More

- New to zero-knowledge proofs? [`EXPLAINER.md`](docs/EXPLAINER.md), plain English.
- Prove blocks, join the party: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Run your own coordinator (archive node + bridge + board): [`docs/RUN_YOUR_OWN_COORDINATOR.md`](docs/RUN_YOUR_OWN_COORDINATOR.md)
- **Specification** (formats, invariants, how to verify independently): [`docs/SPEC.md`](docs/SPEC.md)
- Soundness statement (a reviewer's best first read): [`docs/SOUNDNESS.md`](docs/SOUNDNESS.md)
- Audit record: [`SECURITY.md`](SECURITY.md) · latest round: [`AUDIT_2026-07.md`](docs/AUDIT_2026-07.md)
- Adversarial fuzzing (what was fuzzed, what wasn't): [`docs/FUZZING.md`](docs/FUZZING.md)
- What we're for, and how far along: [`docs/GOALS.md`](docs/GOALS.md), six goals, measured status
- What's left to build: [`docs/RELEASE_PLAN.md`](docs/RELEASE_PLAN.md)
- **What to actually run** — fleet shape, card, po2, build flags, and what is still unsettled:
  [`docs/TOPOLOGY_AND_SETTINGS.md`](docs/TOPOLOGY_AND_SETTINGS.md)
- Why those numbers, and what we got wrong reaching them:
  [`docs/CORE_VS_GHOST.md`](docs/CORE_VS_GHOST.md)
- How it's built: [`docs/`](docs/)

## Prior art and credit

**[ZeroSync](https://zerosync.org)** (Robin Linus and collaborators) has been at this longer: a
proof system for instant chain-state sync, a developer toolkit, and the case for a ZKP verifier in
Bitcoin itself. Read that first.

**[RISC Zero](https://risczero.com)** is the zkVM this runs in. `prover/` was scaffolded from their
template; `vendor/risc0-zkvm` carries their crate with two local changes. Apache-2.0.

**Bitcoin Core** and **libsecp256k1** are compiled in, unmodified but for two portability patches.
Full attribution in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Licence

MIT (see [`LICENSE`](LICENSE)). The guest compiles in Bitcoin Core and libsecp256k1 (both MIT); the
patches are portability-only and change no consensus logic. `prover/` carries an additional Apache-2.0
notice for the risc0-derived build scaffolding. Third-party components are attributed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

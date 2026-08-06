# Hazync

**Bitcoin's consensus rules, proven — using Bitcoin Core's own code, inside a zero-knowledge VM.**

Not a reimplementation of the rules. The actual `interpreter.cpp`, the actual `SignatureHash`, the
actual `libsecp256k1`, compiled to RISC-V and executed inside a prover. Every prior validity-proof
effort inherits the question *"does your rewrite match Core in every edge case, forever?"* This one
does not have to answer it.

---

### Check one yourself. It takes about thirty seconds.

```bash
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-verify-x86_64-linux-gnu
chmod +x hazync-verify-x86_64-linux-gnu
curl https://bitcoinghost.org/hazync/api/spine/proof -o proof.bin
./hazync-verify-x86_64-linux-gnu proof.bin
```

```
>>> SNARK RANGE PROOF [1..N] VERIFIED — genesis-anchored
```

A **1.7 MB** binary, and a proof that every block from genesis to N is valid under Core's real
consensus rules — checked in **milliseconds**, on a laptop, with no node, no peers, no chain data and
nothing to trust. [Or do it in your browser](https://bitcoinghost.org/hazync/verify/), where the
verifier is a 290 KB WebAssembly module that peaks at **1.9 MiB of memory** — small enough for a
phone.

N is however far the anchored proof currently reaches, and it grows as the board does. Swap the URL
for `/api/proof/<height>` to be handed one block instead and check that alone.

That is the whole idea. Proving is expensive and done by a few; **verifying is cheap and done by
everyone.**

---

### The proofs combine

Two adjacent proofs fold into one, and the result folds again. A stretch of chain collapses into a
single succinct receipt — the same size whether it covers two blocks or two hundred thousand. One
receipt, one check, no re-execution.

The end this builds toward: **a node that verifies the whole chain from a single proof, instead of
re-executing sixteen years of it.**

### Where it actually is

The hard part is done: real Core consensus code, proving real mainnet blocks, hardened across nine
rounds of adversarial **self**-audit ([`SECURITY.md`](SECURITY.md)) and validated across the segwit,
taproot, big-block and pre-BIP34 eras. The guest image id is **reproducible** — CI rebuilds it from
scratch and checks it matches.

What remains is scale, and we are honest about it: the board **restarted from genesis on 2026-08-04**,
when the audit #5 re-baseline (shipped in v0.17.0) pinned the current guest `4722cec8` — the third
reset in as many days, and the price of changing the guest at all. Re-proving is under way, on an open
board anyone can join; [the live board](https://bitcoinghost.org/hazync.html) is the only place a
current figure belongs, and a genesis-anchored proof is downloadable there. Proving Bitcoin's real cryptography is deliberately expensive — that cost
*is* the security argument — which is why this is a public proof party rather than something finished
quietly. **Three independent external reviews ran in August 2026. None found a way to make the guest ACCEPT an
invalid chain — but the third found a canonical-chain break that would have made it REJECT a valid
one**, stalling any from-genesis prover at block 91841, roughly 10% in. Blocks 91842 and 91880
duplicate coinbases that were still unspent, which is the reason BIP30 exists, and the new
non-membership check had no exception for them. Fixed in v0.15.0, with the real blocks now in the
fixture set. The first two reviews flagged the same two places as the residual risk, and
everything they raised is fixed or tracked ([`SECURITY.md`](SECURITY.md)). Those were code reviews,
not a commissioned professional audit — that has still not happened.**

[**Watch the board**](https://bitcoinghost.org/hazync) · [**Join in**](CONTRIBUTING.md) ·
[**Read the spec**](docs/SPEC.md)

---

## What is actually compiled from Core

The script interpreter (`interpreter.cpp`), `SignatureHash`, `CheckTransaction`,
`ComputeMerkleRoot`, the transaction/weight/sigop machinery, the difficulty retarget (`pow.cpp`'s
`CalculateNextWorkRequired`, driven through the real `CBlockIndex`), and `libsecp256k1` — unmodified,
with two narrow portability shims and zero consensus-logic changes.

What is *not* compiled from Core is a thin, self-contained slice: the subsidy halving schedule and the
script-flag activation heights, each differentially tested against Core (the flag schedule is proven a
sound superset of `GetBlockScriptFlags`). Even the compiled retarget is belt-and-suspenders —
cross-checked against the actual on-chain `nBits` at every one of the 476 mainnet retargets.

## Verifying, in detail

The command above is the whole story for most people. This section is the rest of it.

The file it downloads is the **spine**: the current genesis-anchored head, one receipt attesting that
*every* block from 1 to N is valid under Core's own consensus code. It advances by absorbing new
blocks rather than being rebuilt, so it is always complete as it stands — check
[`/hazync/api/spine`](https://bitcoinghost.org/hazync/api/spine) for how far it reaches.

`/api/proof/<n>` serves the receipt for a single block instead. That one exits **`2`**, not `0`: the
SNARK is valid, but one mid-chain block is not genesis-anchored. That is the correct answer rather
than a failure, and the verifier says so rather than pretending otherwise.

Prebuilt binaries need Linux x86-64, glibc 2.34+ (Ubuntu 22.04+, Debian 12+). No GPU, no build, no clone.

`-LO` keeps the asset's own filename, which is what `SHA256SUMS.txt` lists. Renaming it on download
(`-o hazync-verify`) makes `sha256sum -c` report *"no file was verified"* — which looks like a broken
signature and is not. Rename it afterwards if you like.

An `aarch64` build is published too, so "a phone can check this" is a file you can download rather than
a claim. It exits `0` when the proof is genesis-anchored, `2` when the SNARK is valid but the range is
a mid-chain **segment** (most proofs on the board are segments — that is not a failure), and `1` when
the proof is actually bad.

**The 184 MB host** does everything else — proving, and `verify-any`, which accepts *any* single proof
rather than only genesis-anchored ones. (It was 71 MB up to v0.12.1, when it did not actually contain
a prover: it shelled out to `r0vm`, which the release does not ship, so the CPU binary could not prove
at all. The prover is linked in from v0.12.2 — that is the extra 113 MB, and it is why this one works.)

```bash
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu
chmod +x hazync-host-x86_64-linux-gnu
./hazync-host-x86_64-linux-gnu verify-any proof.bin   # → prints a line starting with RANGE-OK
```

**On an older distro** (Ubuntu 20.04, Debian 11 — glibc < 2.34), run the *same* binary in a container, no rebuild:

```bash
docker run --rm -v "$PWD":/w -w /w ubuntu:22.04 ./hazync-host-x86_64-linux-gnu verify-any proof.bin
```

(Or build from source — see [`docs/PROVING.md`](docs/PROVING.md).) Want to trust the binary itself? Verify its SHA256 + PGP signature first — [`SECURITY.md`](SECURITY.md#verifying-releases). The stronger guarantee, though, is reproducibility: `method-id` prints the guest image id, and it matches `reproduce/METHOD_ID` byte for byte.

`RANGE-OK` means the STARK checks out and the receipt is a valid proof that block *n* is a correct consensus transition between its stated boundaries. **Genesis-anchoring** — that those boundaries chain all the way back to the real genesis — is what the connected chain establishes (the board's frontier, or `host verify-chain` on a folded chain proof, which pins the genesis anchor); a single isolated proof attests its own step, not the whole history. Every proof on the [board](https://bitcoinghost.org/hazync) is public. The binary is the canonical guest — rebuild it yourself (`reproduce/Dockerfile`) and you get the same image id, byte for byte (`reproduce/METHOD_ID`).

## What it proves

A verified chain proof attests: **every block from genesis to the tip is valid under Core consensus, the UTXO set equals the committed root, and the work is as committed** — with no re-execution. That covers scripts of every type, real ECDSA and Schnorr through `libsecp256k1`, no inflation, proof-of-work and difficulty, merkle and witness commitments, weight, sigops, and the locktime/BIP rules, under Core's exact flags. The one non-Core piece is the Utreexo UTXO accumulator — our own code (the proven version is the guest's `prover/methods/guest/src/utreexo.rs`), differentially fuzzed ~900k executions against a reference model (`audit-fuzz/`). Both August 2026 reviewers independently named it one of the two most likely places for a hidden bug — and one found real panic paths in the reference crate, now fixed. It still has not had a commissioned audit, and it remains the thing we most want outside eyes on.

## How it works

```
per-input script proof ── block proof ── chain fold ── tip / range proof
 (real VerifyScript)     (all rules)    (recursion)   (one receipt)
```

Prove each block with real Core in the zkVM, fold blocks recursively into one receipt, verify the receipt. Witnesses are served ready-made by an archive-node bridge (a full node that drives the UTXO accumulator forward once and emits each block's witness) — compactly encoded and de-duplicated per transaction, so a big block's witness is tens of MB smaller — so a prover needs no node of its own and no chain replay. Details in [`docs/`](docs/).

## Status

Built and demonstrated on real mainnet data — single blocks, recursive chains, tip operation, parallel backfill; every tip hash and UTXO count matches mainnet. Hardened across **nine rounds** of adversarial self-audit ([`AUDIT_2026-07.md`](docs/AUDIT_2026-07.md)) and empirically validated across the segwit, taproot, big-block, and pre-BIP34 eras on real mainnet data.

Two external reviews ran in August 2026 — findings, fixes and what each could *not* verify are recorded in [`SECURITY.md`](SECURITY.md). Still to come: the full genesis→tip proving campaign and a commissioned audit. Trying to break it is the most useful thing you can do — [`SECURITY.md`](SECURITY.md) maps the soft spots.

## More

- New to zero-knowledge proofs? [`EXPLAINER.md`](docs/EXPLAINER.md) — plain English.
- Prove blocks, join the party: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Run your own coordinator (archive node + bridge + board): [`docs/RUN_YOUR_OWN_COORDINATOR.md`](docs/RUN_YOUR_OWN_COORDINATOR.md)
- **Specification** (formats, invariants, how to verify independently): [`docs/SPEC.md`](docs/SPEC.md)
- Soundness statement (a reviewer's best first read): [`docs/SOUNDNESS.md`](docs/SOUNDNESS.md)
- Audit record: [`SECURITY.md`](SECURITY.md) · latest round: [`AUDIT_2026-07.md`](docs/AUDIT_2026-07.md)
- Adversarial fuzzing (what was fuzzed, what wasn't): [`docs/FUZZING.md`](docs/FUZZING.md)
- What we're for, and how far along: [`docs/GOALS.md`](docs/GOALS.md) — six goals, measured status
- What's left to build: [`docs/RELEASE_PLAN.md`](docs/RELEASE_PLAN.md)
- How it's built: [`docs/`](docs/)

## Licence

MIT (see [`LICENSE`](LICENSE)). The guest compiles in Bitcoin Core and libsecp256k1 (both MIT); the
patches are portability-only and change no consensus logic. `prover/` carries an additional Apache-2.0
notice for the risc0-derived build scaffolding. Third-party components are attributed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

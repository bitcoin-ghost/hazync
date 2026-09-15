# Changelog

**The GitHub releases are canonical:** <https://github.com/bitcoin-ghost/hazync/releases>. This file is an
index of them — one entry per tag, newest first, each summarised from that release's title and body and
nothing else. Dates are the GitHub publication date (UTC).

Maintenance: add the new entry here in the same change that publishes the release notes. An entry marked
**re-baseline** moved the guest image id, so every proof made under the previous id stops verifying; the full
id history, with the commit that produced each id, is [`reproduce/LINEAGE.tsv`](reproduce/LINEAGE.tsv).
Copies of recent release bodies are kept in [`docs/history/releases/`](docs/history/releases/).

## v0.21.5 — 2026-09-15
Claims that belong to their key, and workers that tell you when they stop. The worker signs its claims
(#323) and pushes to the prover's phone when it stops or cannot work (#326); the coordinator's claim grace,
per-key cap and re-take wait (#297, #319, #321) shipped live beforehand. Not a re-baseline.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.5>

## v0.21.4 — 2026-09-13
A board that explains itself. Every change comes from block 39,413, which pinned the frontier for five hours
while the coordinator's only account was "a live worker is proving it". Host and coordinator only; not a
re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.4>

## v0.21.3 — 2026-09-12
A board that frees itself — a frontier frozen for thirteen hours and a block no worker could prove — and
attribution for proving, folding and anchoring as three separate jobs. The CUDA host is rebuilt from this
release's source. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.3>

## v0.21.2 — 2026-09-11
Reliability: workers that fail loudly (#261: a box with no usable GPU stops instead of claiming and
abandoning blocks), a spine that keeps up, an aggregate you can time. Not a re-baseline.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.2>

## v0.21.1 — 2026-09-11
#119 fixed: a witness-generation bug in risc0's rv32im prover (`risc0-circuit-rv32im-sys` 4.0.3) made
receipts fail their own `verify()`. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.1>

## v0.21.0 — 2026-09-07 — re-baseline `3867611d` → `37987b85`
Core becomes the guest that ships: the release binary proves with the CORE levers on. Every proof under the
previous id is invalid and the board reset. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.21.0>

## v0.20.0 — 2026-09-06 — re-baseline `1d6c3792` → `3867611d`
The coprocessor field backend, merged but not enabled: this release ships the stock guest, and the id moved
because guest source changed. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.20.0>

## v0.19.0 — 2026-08-25 — re-baseline `4722cec8` → `b62d2a60` → `1d6c3792`
Proving distributes end to end: the aggregate, not only segment proving, can be served to workers. Crosses
two baselines. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.19.0>

## v0.18.5 — 2026-08-08
Everything downloaded and run is signed (`run-workers.sh` and `server.py` join `SHA256SUMS.txt.asc`); the
coordinator half of federation. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.18.5>

## v0.18.3 — 2026-08-07
Fixes from two audits, one following the published instructions as a stranger would; the contributor
quick-start now runs. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.18.3>

## v0.18.2 — 2026-08-06
The OOM remedy message named the wrong cause (#97): two proves do not fit on one 46 GB card. Release tooling
fixed. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.18.2>

## v0.18.1 — 2026-08-06
Bug fixes for failures that reported the wrong thing or nothing, including `hazync fold` never reporting why
it failed. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.18.1>

## v0.18.0 — 2026-08-06
`host dump-snapshot`: the bridge's UTXO set at a proven height as a checkable artifact; verifier FFI and
release tooling changes. Not a re-baseline. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.18.0>

## v0.17.0 — 2026-08-04 — re-baseline `b161735a` → `4722cec8`
Audit #5 guest guards: two bounds checks and an integer accumulator. No consensus rule changed.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.17.0>

## v0.16.0 — 2026-08-04 — re-baseline `dfc9eeda` → `b161735a`
The guest id no longer depends on the checkout path (#88). No consensus logic moved.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.16.0>

## v0.15.0 — 2026-08-03 — re-baseline `71790584` → `dfc9eeda`
BIP30 closed by a coinbase-only SMT (#54): proven, not argued.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.15.0>

## v0.14.0 — 2026-08-02 — re-baseline `be5e0528` → `71790584`
Two findings from the first external reviews, batched into one re-baseline.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.14.0>

## v0.13.5 — 2026-08-02
External-review response: the release tag was interpolated into `run:` blocks of the job holding the signing
key, and is no longer. Host only. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.13.5>

## v0.13.4 — 2026-08-01
The documented from-source reproduce build works. Binaries byte-identical to v0.13.3.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.13.4>

## v0.13.3 — 2026-08-01
Claims survive long proves (#52); bounded, visible provers. Only `hazync-worker` changes.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.13.3>

## v0.13.2 — 2026-08-01
Folding builds a tree: it had been offering any adjacent pair (581 folds for 96 blocks). Mainly a coordinator
fix. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.13.2>

## v0.13.1 — 2026-08-01
A missing coordinator endpoint no longer reads as a rejected proof. Only `hazync-worker` changes.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.13.1>

## v0.13.0 — 2026-07-31
Compute that accumulates: an incremental genesis-anchored spine (`host extend-spine`) and folding as a task.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.13.0>

## v0.12.2 — 2026-07-31
The CPU prover proves: v0.12.1's CPU host was built without risc0's `prove` feature.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.12.2>

## v0.12.1 — 2026-07-31
Work is claimed again, one block at a time. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.12.1>

## v0.12.0 — 2026-07-31 — re-baseline `3f52baff` → `85dc0b56` → `be5e0528`
Accumulator leaf/interior domain separation, then `ruint` 1.19.0 → 1.20.0 (RUSTSEC-2026-0220). The board
restarted from genesis. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.12.0>

## v0.11.0 — 2026-07-29
A proof you can check and a node that can act on it: the 1.6 MB standalone verifier (`verifier/`). Guest
unchanged. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.11.0>

## v0.10.0 — 2026-07-27 — re-baseline `68819a54` → `3f52baff`
`ECMULT_WINDOW_SIZE` 15 → 19 and backend-aware segment po2. Speed only.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.10.0>

## v0.9.1 — 2026-07-26
Host-only fix for v0.9.0's bundle JSON round-trip bug, plus proof-party hardening.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.9.1>

## v0.9.0 — 2026-07-26 — re-baseline `7a8b29e0` → `68819a54`
Witness byte-packing and per-transaction dedup: ~40% fewer guest cycles on big blocks, 37× smaller witnesses.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.9.0>

## v0.8.0 — 2026-07-26 — re-baseline to `7a8b29e0`
The difficulty retarget runs Core's own `pow.cpp`, and every consensus constant comes from `chainparams.cpp`.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.8.0>

## v0.7.2 — 2026-07-25
Host fix: in-block spend detection keyed on the coin leaf, not its txid.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.7.2>

## v0.7.1 — 2026-07-24
BIP30 bridge handling: the overwrite witness for blocks 91842 and 91880. Host only.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.7.1>

## v0.7.0 — 2026-07-24 — re-baseline `601d7ca2` → `36a0415d`
P2SH sigop over-count fix (reject-valid only) and a corrected CUDA binary.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.7.0>

## v0.6.2 — 2026-07-24
Prover reliability: a host-side retry for risc0 4.0.5's segment-boundary preflight panic.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.6.2>

## v0.6.1 — 2026-07-23
The archive-node bridge: one resident Utreexo forest and a ready-made witness per block, removing the
quadratic replay. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.6.1>

## v0.6.0 — 2026-07-22 — re-baseline `c029cee4` → `601d7ca2`
Round-8 audit: anchor-bound chain proofs and a gated witness commitment.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.6.0>

## v0.5.2 — 2026-07-19
Docs currency and diagnostic completeness. Guest unchanged. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.5.2>

## v0.5.1 — 2026-07-19
Pre-release hardening: `verify-any` tells a forged proof from a guest-id mismatch. Guest unchanged.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.5.1>

## v0.5.0 — 2026-07-19 — re-baseline `d1fc4065` → `c029cee4`
Minimal pure-Core guest: the dormant k256 experiment and an unreachable legacy mode removed.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.5.0>

## v0.4.0 — 2026-07-18
Reproducible guest image id (`d1fc4065`): the guest is built at fixed paths in a hermetic container.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.4.0>

## v0.3.0 — 2026-07-17
Round-6/7 hardening, including the coordinator height-splice fix (H9).
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.3.0>

## v0.2.0 — 2026-07-17
The Proof Party: the public coordinator and the `coordinator/hazync` contributor CLI.
<https://github.com/bitcoin-ghost/hazync/releases/tag/v0.2.0>

## v0.1.0 — 2026-07-16 (pre-release)
Research preview. <https://github.com/bitcoin-ghost/hazync/releases/tag/v0.1.0>

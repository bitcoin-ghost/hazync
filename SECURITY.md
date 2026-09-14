# Hazync — security review & status

**No commissioned professional audit has happened.** Most findings in the audit record were surfaced
by our own adversarial passes over the guest/host code; two rounds (10 and 11) were AI-assisted
full-source reviews by people outside the project, which is a real distinction but not the same thing
as a paid audit. We fix what is found, re-run the regression to identical results, and record it in
the open: the status table and open items here, the round-by-round detail in
[`docs/history/SECURITY_AUDIT_LOG.md`](docs/history/SECURITY_AUDIT_LOG.md). **Independent review is explicitly invited** — the open items at the bottom are the starting
bounty list. If you find a way to make an invalid input prove valid, that is the finding that matters
most.

> ⚠️ **Two numbering schemes run through this repo, and they do not line up.** The **rounds**
> (1–9 self, 10–11 external) are the review passes recorded in the audit log. The **internal audits**
> (#1–#5, counted in [`docs/EXTERNAL_REVIEW.md`](docs/EXTERNAL_REVIEW.md)) are a separate series, and
> they are what `reproduce/METHOD_ID`, `docs/history/ROADMAP.md` and `docs/PROVING.md` mean by "audit #3" and
> "audit #5". Audit #3 is *not* round 3: it is the 2026-08-03 pass that found the BIP30 F-1
> canonical-chain break, and audit #5 is the 2026-08-04 pass whose guest guards were pinned as
> `4722cec8` (superseded since; the guest that ships now is `37987b85`, pinned 2026-09-06).

The property that makes this worth reviewing: the prover runs **real Bitcoin Core v28 consensus code**
(`interpreter.cpp`, `SignatureHash`, `libsecp256k1`, with Core's consensus logic unmodified) inside a
RISC0 zkVM, plus a Utreexo accumulator. There is no consensus reimplementation to diverge from Core.
It is *maximal-Core*, not pure Core ([`docs/SPEC.md`](docs/SPEC.md) §12): the
canonical guest applies `patches/0001` (an ILP32 `Serialize` overload) and `patches/0002` (SHA-256 via
the zkVM accelerator) to Core and, since v0.21.0, `patches/0012` (a coprocessor field backend) and
`patches/0013` (a `lift_x` witness hint that libsecp's own arithmetic checks) to libsecp256k1. Those
four patches are part of the trust base. The prover-side changes in `vendor/` are listed in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). [`docs/SOUNDNESS.md`](docs/SOUNDNESS.md) states
the rest of the trust base; its patch list predates v0.21.0.

## Verifying releases

Each release carries a PGP-signed `SHA256SUMS.txt` (signed in CI by the release-signing workflow). The
maintainer key is:

```
defenwycke <defenwycke@icloud.com>
777FE81F 8CC077FD 3D08055E 852C2B31 90F5B928
```

From a release's assets, download the binary **keeping its asset filename** (`hazync-host-x86_64-linux-gnu`) plus `SHA256SUMS.txt` and `SHA256SUMS.txt.asc`, then:

```bash
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-host-x86_64-linux-gnu
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/SHA256SUMS.txt
curl -LO https://github.com/bitcoin-ghost/hazync/releases/latest/download/SHA256SUMS.txt.asc
curl -s https://github.com/defenwycke.gpg | gpg --import   # the key, from the account that publishes
                                                           # the releases (see "second source" below)
gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt   # must report a GOOD signature from the key above
sha256sum -c --ignore-missing SHA256SUMS.txt      # → hazync-host-x86_64-linux-gnu: OK
```

**The second source is `https://github.com/defenwycke.gpg`.** A fingerprint published in the same
repository you are auditing is not independent evidence — anyone who could alter the releases could
alter this file. GitHub serves the maintainer's key from the *account* the releases come from, which is
a different system with a different compromise path, so agreement between the two is worth something.
Fetching it is also the whole import step above; `gpg --recv-keys 852C2B3190F5B928` from a keyserver
works too and is a third, independent source.

Whichever you use, the fingerprint must be:

```
777FE81F 8CC077FD 3D08055E 852C2B31 90F5B928
```

**The key expires 2028-04-25.** After that, signatures on releases made before then still verify but
`gpg` reports the key as expired — that is the key's lifetime ending, not evidence of tampering.

**`gpg` will also warn that the key "is not certified with a trusted signature".** That is expected and
is not a problem with the signature: it means *you* have not personally signed this key, which nobody
has on a fresh keyring. What matters is the `Good signature` line and that the fingerprint matches the
one above. If you want the warning to go away, sign the key locally (`gpg --lsign-key
852C2B3190F5B928`) — but check the fingerprint against a second source first, because that is the step
the warning is asking you to think about.

`--ignore-missing` matters: the manifest covers **every** published asset — eight in v0.21.4: both
hosts, both native verifiers, the WASM verifier, the worker CLI, the fleet launcher and the coordinator
script — so a plain `sha256sum -c` reports `FAILED open or read` and exits non-zero for each one you
didn't download. That is
the command complaining about a missing file, not about a bad checksum — but it looks alarming, so use
the flag and check only what you fetched.

(If you saved the binary as `host` per the quick-start, `sha256sum -c` won't find it by name — either keep the asset filename as above, or run `sha256sum host` and compare the digest to the matching line in `SHA256SUMS.txt` by eye.)

A good signature plus a matching checksum means the binary is the maintainer's published build. Note
that the *stronger* guarantee is reproducibility, not the signature: anyone can rebuild the guest and
confirm its `METHOD_ID` matches the canonical id in `reproduce/METHOD_ID`, and any proof verifies against
that id regardless of who built the host. The signature adds provenance on top of that. (The canonical id
is re-pinned whenever the guest changes — most recently to `37987b85` (2026-09-06: Core becomes the
guest that ships), after `3867611d` (the coprocessor field backend), `1d6c3792` (parallel block validation) and `3f52baff` (v0.10.0: libsecp's
`ECMULT_WINDOW_SIZE` 15→19, a compile-time speed trade with no consensus change), after the witness
wire-format change (`68819a54`, v0.9.0), the round-9 post-audit hardening (`cb114426`), the real-Core
`pow.cpp` retarget carve (`ffdc6095`), and chainparams-sourced consensus constants (`7a8b29e0`); see
`reproduce/METHOD_ID` for the authoritative lineage. The reproducible-build mechanism itself — pinned toolchain, Core v28.0, secp256k1 v0.5.1, `Cargo.lock`,
`reproduce/Dockerfile` — is unchanged, so the id stays reproducible, it is just a new value.)

## Status at a glance

| ID | Area | Severity | Status |
|----|------|----------|--------|
| SEC-1 | Witness-commitment bypass (`has_witness` host-controlled) | med-high | **fixed** (6c63565) |
| SEC-2 | Accumulator `delete` trusted an unverified position | high-crit location | **fixed** (6c63565) |
| SEC-3 | Prevouts vector length unchecked (OOB read) | low | **fixed** (6c63565) |
| S1 | Recursion `self_id` self-reference argument | soundness | **fixed** (committed + verifier-asserted; adversarial wrong-id chain rejected) |
| S2 | Coinbase maturity / BIP68-height fed placeholder metadata | soundness | **fixed** (real coin metadata; validated on 741000) |
| S4a/b | BIP34 (height in coinbase), BIP30 (duplicate txid) | completeness | **fixed** (validated on 741000) |
| C1 | No automated regression harness | quality | **fixed** (`check-full` / `regress` execute-mode) |
| SEC-neg | Negative regression tests (reject-path coverage) | quality | **done + continuously CI-enforced** (`prover/ci_negative_tests.sh`): SEC-1 witness (`witness_ok`), SEC-2 position (`all_ok`/`root_matches`), COV-1 time-too-old, COV-2 merkle mutation, and the **retarget / block-weight / sigop-cost** reject-paths — each asserts the malicious input REJECTS and an honest baseline ACCEPTS, so a future guest change can't silently regress one |
| DIFF-retarget | The retarget carve (real `pow.cpp`) matches Core on real data | quality | **done + CI-enforced** (`prover/retarget_diff_test.sh`): the guest's `calc_next_bits` — now Core's compiled `CalculateNextWorkRequired` — reproduces the **actual on-chain nBits at all 476 mainnet retargets**, agrees with an independent transcription over the vectors + a span sweep, and satisfies the clamp/powLimit/monotonicity invariants |
| DIFF-flags | Script-flag schedule is a sound superset of Core | quality | **done + CI-enforced** (`host script-flags-test`, sharing the guest's `script_flags` module): proves guest flags ⊇ Core's `GetBlockScriptFlags` at every activation boundary (retroactive base flags can only *reject* more — soundness-preserving), the buried soft-forks (DERSIG/CLTV/CSV/NULLDUMMY) flip at Core's exact heights, and the two `script_flag_exception` blocks behave (BIP16 → no flags, taproot → TAPROOT cleared) |
| S3 | Standalone block proofs don't bind to the real UTXO set | inherent | **by design** — real binding comes from the chain recursion; closed operationally by the archive-node bridge |
| BIP68-time | Block-proving path now commits real `MTP(coinHeight−1)` | **soundness** | **fixed + now CI-enforced** (`prover/test_bip68_locks.sh` + `test_bip68_real.sh`: height- and time-based locks on real mainnet MTP, a real CSV-locked mainnet tx, and a pre-CSV control asserting we do *not* enforce where Core doesn't. Both harnesses previously only *printed* results and were not run — `test_bip68_real.sh` was in fact announcing "expect REJECT" while getting VALID, because it left the heights below the CSV activation gate so the branch never ran. Now they assert and run on every push.) |
| COV-1 | `time-too-old`: block timestamp must exceed MTP(prev 11) — was unchecked | **soundness** | **fixed + negative-tested** (asserted in chain_step/aggregate/prove_range) |
| COV-2 | Merkle CVE-2012-2459 mutation flag was discarded (`nullptr`) | **soundness** | **fixed + negative-tested** (capture Core's `mutated` and reject) |
| H1 | Block height host-controlled (flag/subsidy downgrade) | **critical** | **fixed + negative-tested** (`w.height == prev.height+1`; `host adversarial` #1) |
| H2 | Segmented chunks bound neither flags nor spending witness | **critical** | **fixed + negative-tested** (per-input binding digest; `HAZYNC_H2_BADHEIGHT` prove-seg) |
| H3 | In-block coin double-spend / ordering (inflation) | **high** | **fixed + negative-tested** (ordered multiplicity guard; `host adversarial` #3) |
| H4 | Coinbase never run through `CheckTransaction` | med | **fixed + negative-tested** (`host adversarial` #4) |
| H5 | Multi-input tx: non-`input_idx` fee-prevouts unbound to the accumulator (inflation/theft) | **critical** | **fixed + negative-tested** (per-tx input-list pre-pass; `host adversarial` #5) |
| H6 | Range verifier under-pinned the genesis in-boundary (`in_epoch_start`/`in_roots`/`in_recent`) → forgeable first retarget / phantom UTXO seed | high | **fixed** (`assert_genesis_in_boundary` in `verify-range`; `verify-any` applies it when the range claims genesis) |
| H7 | Coordinator chained ranges by tip-hash only — no cross-range difficulty/MTP continuity | medium | **fixed** (`verify-any` now pins the genesis in-boundary + exposes nbits/epoch; coordinator `_frontier_chain` requires `out_nbits/out_epoch(k) == in_nbits/in_epoch(k+1)` across every seam) |
| H8 | Cross-mode journal laundering: `block_proof` (mode 1) commits a self_id-free journal that never aborts | speculative | **fixed** (domain tag `KIND_*` is the first committed field of every recursion-consumed journal — `ChainState`/`RangeState`/`ChunkOut` — and asserted on every decode) |
| A1 | Bare `ChainState` (mode-2/5) receipt did not commit its anchor → a fabricated-anchor receipt is journal-indistinguishable from a genesis-anchored one | verifier-hole | **fixed** (S5: `anchor_id = dsha256(base anchor)` committed + carried; new `verify-chain` pins it to genesis. Not exploitable via shipped verifiers before — they take only `KIND_RANGE`) |
| G3 | BIP141 witness-commitment check ran at all heights (Core gates on segwit ≥481824) → reject-valid stall in 433k–481823 | med (reject-valid) | **fixed** (gate at 481824; below, `witness_ok=!has_witness` = Core `unexpected-witness`) |
| G2 | `unexpected-witness` excluded the coinbase from `has_witness` | low | **fixed** (coinbase now counted) |
| G5 | Block-level `MoneyRange(nFees)` implicit; `subsidy+fee` unguarded i64 add | low | **fixed** (explicit MoneyRange + i128-safe bound, folded into `subsidy_ok`) |
| G1 | General BIP30 `HaveCoin` replaced by a structural argument (utreexo has no non-membership proof) | low (bounded) | **CLOSED (#54)** — replaced by a coinbase-only sparse Merkle tree that proves non-membership directly; root journalled + seam-enforced, pinned empty at the genesis anchor. The ~1,983,702 ceiling no longer applies |
| G4 | 2-hour future-time limit | (not a bug) | **intentionally not enforced** (verifier-local wall-clock, unprovable in a zkVM) |
| N1 | Dead `BlockInput.flags` wire field (flags are guest-derived) | hygiene | **removed** (guest+host+all sites, incl. host `Spend.flags`) |
| N2 | `scriptPubKey` not length-prefixed in the UTXO leaf | fragility | **fixed** (length-prefixed in all 3 byte-identical leaf sites — changes every leaf hash/root) |
| N3 | `tx_full_sigops` returned legacy-only cost on a short prevouts blob | fragility | **fixed** (fails closed: poison cost → `sigops_ok=false`) |
| #4 | P2SH sigop over-count: `tx_full_sigops` counted redeemScript sigops for every input (reject-valid near the sigop cap) | med (reject-valid) | **fixed** (round 9: guard the redeemScript count with `IsPayToScriptHash()`, matching Core's `GetP2SHSigOpCount`; guest change → id re-baselined) |
| #6 | BIP30 grandfathered duplicate coinbases (91842 / 91880) require Core's overwrite | completeness | **fixed** (round 9: guest *mandates* the overwrite witness at exactly those two block hashes; the bridge emits it; 91842 proved end-to-end, `RANGE-OK`) |
| #8 | In-block-spend detection keyed on txid, not the coin leaf (pre-BIP34 coinbase-txid collision) | liveness (host-side) | **fixed** (round 9: both host witness-builders detect in-block spends by `created.contains(&coin_leaf)` — the guest's exact rule; guest unchanged, `v0.7.2`. A valid block was made unprovable — no invalid block was ever accepted) |
| R-1 | Coinbase `vin[0]` empty-guard | robustness | **fixed** (soundness audit: empty-guard hardening against UB — a robustness fix, **not** a soundness hole) |
| — | External audit | — | **open / wanted** |

## The audit record

The round-by-round narrative — each finding's detail, fix and validation — is in
[`docs/history/SECURITY_AUDIT_LOG.md`](docs/history/SECURITY_AUDIT_LOG.md), moved there from this file
on 2026-09-14. Every finding ID used in this file or cited elsewhere in the repo is defined in one of
its sections:

| IDs | section of the log |
|---|---|
| SEC-1, SEC-2, SEC-3 (and the 741000 leaf-count note) | Fixed 2026-07-16 |
| H1–H4 | Fixed 2026-07-17 |
| H5–H8 | Fixed 2026-07-17 (round 2) |
| H-S1–H-S4; S1/F1, F2, F3, S2, S3 (the coordinator trust boundary) | Fixed 2026-07-17 (round 3) |
| the bench-backdoor and `MiniReader` hardening | Round 4 |
| F1–F3 (round 5's, distinct from round 3's) | Round 5 |
| H9 | Round 6 |
| A1, G1–G5, N1–N3 | Round 8 (full write-up in `docs/history/AUDIT_2026-07.md`) |
| #4, #6, #8 | Round 9 |
| `cshims.c` (round 10); H-1, M-1, L-1–L-3 (round 11) | Rounds 10 & 11 |
| S1–S4, C1–C3, and housekeeping H1–H3 (distinct from the guest H1–H4) | Earlier findings (2026-07-15) |
| COV-1, COV-2 | Coverage audit (2026-07-16) |

## Open items (the review bounty list)

1. **SEC-neg — DONE and continuously CI-enforced.** Negative regression tests proving the fixes
   *reject* the malicious cases (not just that valid blocks still pass). Both halves below demonstrate
   rejection, and — beyond SEC-1/SEC-2 — the **retarget, block-weight, and sigop-cost reject-paths are
   now covered too**: `prover/ci_negative_tests.sh` drives each of `retarget_ok` / `weight_ok` /
   `sigops_ok` false on a crafted malicious block (with an honest baseline that must still pass) and runs
   in CI, so a future guest change cannot silently regress one. Anything earlier that framed those three
   reject-paths as untested or a follow-up is stale.
   - **SEC-1 (witness) — done.** `prover/make_negative_tests.py` produces `block_741000_badwit.json`
     (one byte flipped inside a transaction's witness → wtxid changes, txid does not). `check-full`
     reports `merkle_ok=true, witness_ok=false, all_ok=false` — the block is rejected specifically on
     the BIP141 witness commitment, confirming the check is enforced and unskippable.
   - **SEC-2 (position) — done.** A test-only host knob (`HAZYNC_SEC2_BADPOS=1`) corrupts the first
     spend's `global_pos` to a different in-range index while leaving its inclusion proof honest — the
     exact inconsistency the normal path can't express (both fields derive from the same accumulator
     lookup). `check-full` on block 170 then reports `all_ok=false, root_matches=false` with every
     other flag true, isolating the rejection to the hardened `delete`'s position check. Without the
     knob the same block is VALID. The knob is inert unless the env var is set. See
     `prover/make_negative_tests.py`.
2. **BIP68 time-based (soundness) — FIXED.** The correct value is Core's `GetMedianTimePast(coinHeight−1)`.
   The block-proving path previously committed the creating block's raw timestamp (or `0`), skewing the
   required-elapsed test so a premature time-based relative-locked spend could prove valid.
   - **The check is proven correct on REAL mainnet data.** `prover/test_bip68_real.sh`
     (evidence `prover/evidence/bip68_real_mainnet.txt`) runs the real `check_input_locks` on a real
     mainnet tx — `3fa669af…` in block 958250, a 90-day Taproot CSV lock — with the real
     `coin_mtp = MTP(945408)` and `spend_mtp = MTP(958250)`. The coin is 90.2 days old, mainnet accepted
     it, and the check returns VALID; a coin ~0.3 days younger is REJECTED (`-42`).
   - **The proving path now commits the real value.** A coordinated host+guest change: the guest's
     `validate_block` commits `mtp` (= `median(prev.recent_times)` = `MTP(h−1)`) on created-output
     leaves, and every host builder derives the same value from the chain it has processed (the
     `block_mtp` window in the IBD path, mirroring what an archive node holds for free) — so the
     creation-side and spend-side leaves match. Validated: `check-ibd` genesis→550, `check-full` 741000,
     and the 170→172 chain demo all remain VALID with **identical** tip hashes (those are header-derived;
     only the internal leaf MTP is now correct). No fetcher dependency for the IBD path — the host derives
     the MTP itself.
3. **External audit** — especially of the accumulator (the one non-Core component) and the recursion
   binding. Wanted.

## TL;DR
The hard part — proving the *real* Core consensus code, not a reimplementation — is done and is the
thing that removes the soundness gap every prior effort carried. The findings so far are around the
edges (a host-controllable witness flag, an under-constrained accumulator index, placeholder metadata,
a couple of missing rules) and are all fixed and regression-checked, including the BIP68 time-lock
input from the bridge (open item 2). What remains is the real ask: independent adversarial review.
Plainly: the **Utreexo accumulator
is the single most likely location of any remaining soundness bug** — it is the one non-Core component,
differentially fuzzed but **not** externally audited, and it is where we most want outside eyes.

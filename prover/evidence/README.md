# `prover/evidence/` index

Committed raw output from runs that docs cite: logs, measurements, negative controls. Each file records
what one run produced under the guest and code of its day, so a superseded guest id or a stale line
number inside one is history, not a reference to update (`scripts/rebaseline-id.sh` excludes this
directory for that reason). Treat the files as append-only: a correction is a new dated entry, never a
rewrite. This index is the file to change.

To add evidence, put in the file itself: the date, the producing commit or branch, the guest id
(`hazync-host method-id`), the box, and the exact command. Then add a row here.

Columns: **Produced under** is taken from the file where it says; otherwise "not stated" plus the
commit that added it (`git log --follow --diff-filter=A`). **Reproduce** is the command or script the
file names, with whether it still exists on `main`. **Cited by** is a `git grep` for the file name
outside this directory; directory-level mentions (`docs/GOALS.md`, `docs/history/AUDIT_2026-07.md`,
`prover/README.md`) are not listed per file.

## Genesis-era proving and hardening (added in `e6496d8`, 2026-07-16)

| File | Records | Produced under | Reproduce | Cited by |
|---|---|---|---|---|
| `block_741000_proof.log` | Block 741,000: 16 chunks on 2 GPUs, aggregated, verified | Not stated; added in `e6496d8` 2026-07-16 | Not stated (the `CLUSTER:` output format is `prover/cluster.sh`, which exists) | none (`.gitignore` names it) |
| `bridge_ibd.log` | Archive-node bridge emitting witnesses to height 199 | 2026-07-15 (log timestamps); guest not applicable | Not stated; the `-hazyncwitness` hook is described in `docs/history/HAZYNC_ARCHITECTURE.md` | none |
| `hardening_validation.txt` | H1/H2/H3 hardening results: regressions, in-block spend, BIP30 | 2026-07-15, 2x L40S; guest not stated | Not stated | none |
| `hardening_rangefold_1_550.log` | Range-fold `[1..550]` verified genesis-anchored, 1077 s | Not stated in file (`hardening_validation.txt` dates the run 2026-07-15) | Not stated (`LEVEL 0: prove blocks` format is `prover/rangecluster.sh`, exists) | none |
| `rangefold_1_176.log` | Range-fold `[1..176]` verified genesis-anchored, 320 s | Not stated; added in `e6496d8` 2026-07-16 | Not stated (`prover/rangecluster.sh` format, exists) | none |
| `test1_2_prove_ibd.log` | Recursive chain genesis to 170, then three tip extensions | Not stated; added in `e6496d8` 2026-07-16 | Not stated (`host prove-ibd`, exists: `prover/host/src/main.rs`) | none |

## Consensus checks and negative tests

| File | Records | Produced under | Reproduce | Cited by |
|---|---|---|---|---|
| `bip68_locks.txt` | BIP68 time/height locks on real MTP, blocks 700000-700100 | Guest not stated; added in `0d78e17` 2026-07-16 | `prover/test_bip68_locks.sh` (exists) | none |
| `bip68_real_mainnet.txt` | 90-day CSV lock on real tx, plus counterfactual reject | Guest not stated; added in `d8387f8` 2026-07-16 | Not stated (current harness `prover/test_bip68_real.sh`; see known issues) | `SECURITY.md` |
| `cov_negatives.txt` | COV-2 merkle mutation and COV-1 time-too-old rejected | Guest not stated; added in `c50b184` 2026-07-16 | `prover/test_cov_negatives.sh` (**deleted**, see known issues) | `docs/history/SECURITY_AUDIT_LOG.md` |
| `cshims_shim_audit.txt` | `cshims.c` call sites, negative `_sbrk`, glibc differential | 2026-08-01, guest `be5e0528` (pre-fix) | Not stated (section 3 output matches `prover/methods/guest/test-cshims.sh`, exists) | none |
| `coinbase_smt_witness_size.txt` | Coinbase-SMT proof size versus tree size (#54 gate) | Measured 2026-08-02; guest not applicable | Not stated; no producing harness found in the tree | none |

## Folding, spine, SNARK wrap, verifiers, adoption

| File | Records | Produced under | Reproduce | Cited by |
|---|---|---|---|---|
| `extend_spine_1_3.txt` | `extend-spine` negative, two absorptions, genesis-anchored verify | Local non-canonical guest `72fb6608` (not in `reproduce/LINEAGE.tsv`); board was on `be5e0528`, since superseded; added in `93b9bff` 2026-07-31 | `host extend-spine`, `host verify-range` (exist) | `coordinator/test_spine_fold.py` (glob `extend_spine_*.txt`) |
| `extend_spine_seam_normalization.txt` | Seam pre-check false rejection; normalised fix; fold cost n=3 | Failure under `be5e0528`; re-run on "local guest" (id not stated); added in `f74206c` 2026-08-01 | `host extend-spine`, `host verify-range` (exist) | `coordinator/test_spine_fold.py` (glob) |
| `fold_and_snark_wrap_1_1000.txt` | Fold `[1..1000]` to one receipt, Groth16 wrap, genesis-pin negative | 2026-07-28, guest `3f52baff` (v0.10.0) | `host fold-range`, `host snark-wrap`, `host verify-snark` (exist) | `docs/PROVING.md`, `docs/SPEC.md`, `docs/TOPOLOGY_AND_SETTINGS.md`, `docs/history/HAZYNC_ARCHITECTURE.md` |
| `fold_concurrency_2xL40S.txt` | Fold throughput and VRAM at K=1..4, both cards | 2026-07-28, guest `3f52baff` (v0.10.0) | `prover/bench-fold-concurrency.sh` (exists) | `docs/TOPOLOGY_AND_SETTINGS.md` |
| `groth16_snark_wrap.txt` | First Groth16 wrap, block 170, CPU path; CUDA path broken | 2026-07-28, guest `3f52baff` (v0.10.0) | `host prove-snark` (exists) | `docs/GLOSSARY.md`, `docs/PROVING.md`, `docs/SPEC.md`, `docs/history/HAZYNC_ARCHITECTURE.md` |
| `groth16_cuda_crash_sm89.txt` | Groth16 CUDA `sppark` illegal-memory-access, environment capture (#20) | 2026-07-28T15:51:27Z, guest `3f52baff`, risc0 3.0.5 | `host snark-wrap` (exists) | `prover/testdata/snark/README.md` |
| `verifier_aarch64.txt` | `hazync-verify` on aarch64 under qemu; later WASM memory | 2026-07-28 `3f52baff`; appended 2026-07-30 `85dc0b56` and 2026-07-31 (WASM) | `cargo build --target aarch64-unknown-linux-gnu`, `qemu-aarch64-static` (commands, stated) | `verifier/README.md` |
| `wasm_verifier_live.txt` | Deployed WASM module equals release; live verdicts correct | 2026-08-01; page reports guest `be5e0528`; module equals v0.13.1 asset | Not stated as a script (node driving deployed `hazync-verify.js`) | `docs/GOALS.md` |
| `node_sync_demo.txt` | Adoptable state from a proof matches a live node | 2026-07-28, guest `3f52baff` (v0.10.0), proof `fold_1000.snark` | Not stated (output matches `prover/node-sync-demo.sh`, exists) | none |
| `hazed_chain_binding.txt` | Identity (merkle + headers) plus validity proof, RPC txids | 2026-07-28, guest `3f52baff` | Not stated (`prover/hazed-chain-verify.py --txid-source rpc`, exists) | none |
| `hazed_gsb_binding.txt` | Same binding from a real hazed `gsb00000.dat` archive | 2026-07-29; archive from ghostd v1.10.16; guest not stated | Not stated (`prover/hazed-chain-verify.py --txid-source gsb`, exists) | none |

## Chunk packing and acceleration

| File | Records | Produced under | Reproduce | Cited by |
|---|---|---|---|---|
| `chunk_packing_741000.txt` | Count- versus cost-packed chunks, block 741,000, execute mode | Host branch `fix/cost-weighted-chunk-packing` (no longer a branch); guest not stated; added in `6b7b033` 2026-08-20 | `host chunk-profile` (exists) | `docs/PROVING.md` |
| `chunk_packing_962000.txt` | Same comparison, block 962,000: straggler 4.07x to 1.10x | Guest not stated; added in `6b7b033` 2026-08-20 | Witness rebuilt with `prover/tools/mkblock.py` (exists); `host chunk-profile` | none |
| `chunk_packing_962000_p2a.txt` | P2A anchor spends priced at zero; straggler 1.10x to 1.02x | Guest not stated; added in `04a8659` 2026-08-21 | `host chunk-profile` (exists) | none |
| `acceleration_board_962000.txt` | Input/signature classification and acceleration model, 962,000 and 741,000 | Generated 2026-08-21T11:47:45Z; guest not applicable (host-side tools) | `prover/tools/classify_inputs.py`, `model_acceleration.py`, `pack_after_139.py` on `prover/block_962000.json` / `block_741000.json` (all exist) | `docs/history/ACCELERATION.md` |

## H100 run, 2026-08-21 (added in `8528bf2`, #141)

Manifest: `h100_962000_2026-08-21.txt`. Its header: H100 80GB HBM3 (sm_90), UpCloud FI-HEL2, driver
595.58.03, CUDA 12.6, 12 cores; branch `bench/135-on-main` at `c6e95ff` (main + #136 + #137); guest
`b62d2a60` (canonical from 2026-08-21 per `reproduce/LINEAGE.tsv`, since superseded); block 962,000, `HAZYNC_CHUNKS=16`,
cost-packed, chunk 9 (451 inputs). All files below share that provenance unless noted. No file here is
cited by name from any doc; the write-up is in `docs/history/ACCELERATION.md` (H100 sections), which
points to an off-repo copy of the full logs (`~/hazync-h100-evidence/`).

| File | Records |
|---|---|
| `h100_962000_2026-08-21.txt` | Manifest and write-up: throughput, concurrency, #139 bake-off, idle gap, signature split |
| `h100_provision.log` | Box provisioning and prover build (step headings of `provision-vps.sh`, exists) |
| `h100_build139.log` | Prover rebuild (release + CUDA) for the #139 bake-off |
| `h100_rebuild-bench.log` | Second prover rebuild before the benchmark runs |
| `h100_ec-prove-patch.diff` | Patch adding `HAZYNC_EC_PROVE` proving arm to `ec_bench()` |
| `h100_run139.log` | EC bake-off: libsecp256k1 versus bigint2, execute and prove |
| `h100_prove-po2-22.log` | Chunk 9 prove at po2 22: 216 segments, 978 s |
| `h100_prove-po2-23.log` | po2 23 attempt: panics, `23 > 22` ProverOpts cap |
| `h100_prove-run2.log` | Repeat chunk 9 prove at po2 22: 966 s |
| `h100_prove-conc2-c1.log` | Concurrent prove 1 of 2: 443 segments, 2064 s |
| `h100_prove-conc2-c2.log` | Concurrent prove 2 of 2: 443 segments, 2062 s |
| `h100_run2-id.txt` | Guest id for run 2: `b62d2a60…` |
| `h100_run2.log` | Completion marker only (see known issues) |
| `h100_vmstat-139.log` | `vmstat` samples during the bake-off |
| `h100_vmstat-run2.log` | `vmstat` samples during run 2 |
| `h100_vram-139.log` | `nvidia-smi` memory/utilisation samples, bake-off |
| `h100_vram-conc2.log` | `nvidia-smi` samples, concurrent run |
| `h100_vram-po2-22.log` | `nvidia-smi` samples, po2 22 prove |
| `h100_vram-run2.log` | `nvidia-smi` samples, run 2 |
| `sigsplit.rs.txt` | Rust source attributing predicted EC verifies to ECDSA/Schnorr |
| `sigsplit_962000.txt` | Its output: 7,917 ECDSA / 191 Schnorr, block 962,000 |

## Known issues

Recorded here rather than fixed in the files.

1. `h100_962000_2026-08-21.txt` line 115 cites `evidence/sigsplit-962000.txt` (hyphen, no `prover/`
   prefix). The file is `sigsplit_962000.txt`.
2. `cov_negatives.txt` names `prover/test_cov_negatives.sh`, deleted in `e1c0837` (2026-07-27) as
   superseded by `prover/ci_negative_tests.sh`, which runs COV-1 and COV-2 in CI.
3. `sigsplit.rs.txt` has no manifest: no `Cargo.toml`, dependency versions or build command.
   `sigsplit_962000.txt` calls it `sigsplit.rs (this directory)`. It says it mirrors
   `predicted_ec_ops` exactly; that held at `8528bf2` only (the function now takes raw byte arguments,
   `prover/host/src/main.rs`). `sigsplit_962000.txt` also records an absolute laptop path.
4. `h100_run2.log` is 10 bytes and contains only `DONE_RUN2`. Run 2's output is `h100_prove-run2.log`.
5. `h100_ec-prove-patch.diff` patches `ec_bench()`, which is not on `main`: the bake-off commits
   `3c185ab` and `7a482e2` are not ancestors of `origin/main`. `h100_run139.log` cannot be reproduced
   from `main`.
6. `acceleration_board_962000.txt` and the H100 manifest cite `docs/ACCELERATION.md`, now
   `docs/history/ACCELERATION.md`.
7. `node_sync_demo.txt` and `verifier_aarch64.txt` (first entry) used
   `prover/testdata/snark/fold_1000.snark`, removed in the re-baseline `3926891` (2026-07-30). The
   current fixtures are `fold_8.snark` and `neg500.snark`.
8. `extend_spine_*.txt` quote `host/src/main.rs` line numbers (1241, 1250, and `normalize_host` at
   193) that have moved; `normalize_host` is now at line 276. `72fb6608` is a local build id, not a
   lineage row.
9. `bip68_real_mainnet.txt` shows `coin_h=100 spend_h=200`. Since `e1c0837`, BIP68 is enforced only
   from CSV activation height 419328, so those heights no longer reach the time-lock branch;
   `prover/test_bip68_real.sh` now passes the real heights 945409 / 958250.
10. `block_741000_proof.log` reports 402 UTXO leaves; `07c38b2` annotated it `[STALE: … current
    guest = 394]`.
11. Amended after being added (the only exceptions to append-only): `block_741000_proof.log`
    (`07c38b2`, annotation), `fold_and_snark_wrap_1_1000.txt` (`6072721`, replaced a root-count claim),
    `verifier_aarch64.txt` (`3926891`, `7b489d3`, appended entries only).
12. `hazed_chain_binding.txt` says the tool refuses `--txid-source gsb`; `prover/hazed-chain-verify.py`
    now implements it, and `hazed_gsb_binding.txt` is the run that used it.
13. 37 of the 47 files are cited by no doc by name.

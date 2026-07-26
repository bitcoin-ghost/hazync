# Hazync fuzzing — resume notes (for the bigger machine)

Status as of 2026-07-21. Four GPU-free adversarial fuzz passes are **done and clean**; the heavy
tier is **deferred** to a machine with real cores/RAM (this was a WSL2 box that OOM/disk-deaths on
big native builds). Full results in `FUZZING.md`; per-target detail in `audit-fuzz/FINDINGS.md` and
`coordinator/SEAM_FINDINGS.md`.

## Done (portable — stable rust + stdlib python3, no GPU, no Core build)
- `audit-fuzz/` — Utreexo accumulator, guest hardened `delete`/`verify`, ~893k execs, clean; a
  positive control (unhardened reference) crashes in <1s.
- `coordinator/seam_fuzz.py` — `_frontier_chain` seam soundness, 200k, clean; `--control` (pre-H9)
  is caught.
- `coordinator/parse_fuzz.py` — untrusted-input handlers, 300k, clean.
- `guest-pure-fuzz/` — pure-Rust helpers extracted from `main.rs` at build time; activation heights
  vs canonical mainnet, `add256`, `median_time_past`, all clean.

Re-run everything: see the Reproduce block in `FUZZING.md`.

## Deferred to the bigger machine (needs the Core build and/or the prover)

**A. Native C++ Core differential — the highest-value next step.** Fuzz the real Core consensus code
the guest actually runs, natively (no zkVM), differentially against independent references + real
mainnet vectors. This directly tests the "we run real Core" claim.
- Stand up `$HAZYNC_BASE` (Bitcoin Core source at the pinned rev + `secp256k1` + `coreshim`) the way
  `provision-vps.sh` does — that's the prerequisite the repo doesn't ship.
- Compile the guest's C++ shim (`prover/methods/guest/verify_input.cpp` + `cshims.c`) and the ~17
  Core TUs listed in `prover/methods/guest/build.rs` **for the host** (x86-64 g++, not riscv). Core
  consensus code is portable, so this should build with the same file list minus the `-march=rv32im`
  flags.
- Wrap the `extern "C"` exports (`block_subsidy`, `calc_next_bits`, `add_work`, `check_pow`,
  `merkle_root`, `check_tx`, `tx_wu_sigops`, `verify_input`, …) with Rust FFI + `cargo-fuzz` and:
  - **Differential** each math export vs an independent reference (subsidy halving formula; the Core
    retarget/timespan-clamp math; an independent Merkle incl. the CVE-2012-2459 mutation flag).
  - **Memory-safety** fuzz the parsers — `verify_input.cpp` reads attacker-controlled `tx_bytes`,
    `prevouts`, `header`, `txids`, `cb` buffers; feed truncated/oversized/garbage buffers and assert
    no OOB/UB (build with ASan). This is the wrapper glue SECURITY.md calls a soft spot.
  - **Real-vector** oracle: a corpus of real mainnet blocks/txs; every FFI result must match a
    trusted full node.

**B. In-zkVM negative corpus (needs the prover/GPU).** Blocks Core rejects — each consensus rule
violated one at a time (over-subsidy, bad merkle, weak flags, bad PoW, premature locktime, BIP30/34
violations, weight/sigop over-limit) — must all FAIL to produce a receipt. Pair with a positive
corpus that must all prove.

**C. Recursion / `METHOD_ID` binding (needs the proving stack).** A doctored guest image or a spliced
recursion level must never yield an accepted tip proof. Exercises `env::verify(self_image_id, …)` and
the fold seam that `seam_fuzz.py` only models at the coordinator layer.

## Gotchas carried over
- CMake/native builds: keep parallelism low (`-j2`) — cc1plus OOMs at higher on a small box.
- `cargo-fuzz` needs the `nightly` toolchain; `-rss_limit_mb` guards against OOM.
- The guest C++ needs the risc0 riscv toolchain only for the *guest* build; the host differential
  uses the system g++ — different flags, same source file list.

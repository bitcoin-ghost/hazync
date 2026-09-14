# Hazync — adversarial fuzzing summary

Independent fuzzing of the components that carry Hazync's soundness and are reachable without a GPU
or the full Bitcoin Core build. Every pass follows the same discipline: an **independent oracle**, a
**positive control** proving the harness detects the exact bug class, and an **honest scope note**.

Harnesses live beside the code they test, and each carries its own detailed report:
[`audit-fuzz/FINDINGS.md`](../audit-fuzz/FINDINGS.md) and
[`coordinator/SEAM_FINDINGS.md`](../coordinator/SEAM_FINDINGS.md).

## Passes

| # | Target | Harness | Scenarios | Result |
|---|--------|---------|-----------|--------|
| 1 | Utreexo accumulator — guest hardened `delete`/`verify` | `audit-fuzz/` (libFuzzer + `arbitrary`) | ~893k execs | **clean** |
| 1c | Accumulator reference `Stump` — positive control | same | — | ⚠ **needs a rerun** — crashed <1 s before the reference was hardened (#63); unverified since |
| 1d | Cached `Forest` vs the pre-cache reference (`forest_cache_equivalence`) | same | 192k runs | **clean**, mutation-checked |
| 2 | Coordinator seam `_frontier_chain` (S1/F1/H9) | `coordinator/seam_fuzz.py` | 200k | **clean** |
| 2c | Seam control, H9 height-guard removed — positive control | same `--control` | — | caught (expected) |
| 3 | Coordinator untrusted-input handlers (`parse_range`, `clean_handle`, `is_hex`) | `coordinator/parse_fuzz.py` | 300k | **clean** |
| 4 | Guest pure-Rust helpers (`block_script_flags`, `add256`, `median_time_past`) | `guest-pure-fuzz/` (build-time extract + refs) | 700k+ | **clean** |
| 5 | Guest C++ shim + Core TUs built natively — math exports vs independent references | `fuzz-native/differential.cpp` (#222) | 27 checks | pass |
| 6 | The same FFI glue under ASan+UBSan — truncated, empty, bit-flipped, random buffers | `fuzz-native/memsafety.cpp` (#222) | 661 cases | 0 sanitizer findings |
| 7 | Real mainnet inputs through `verify_input`, with a byte-flip negative control | `fuzz-native/realvector.cpp` (#222) | 892 inputs | all valid |
| 8 | In-zkVM negative corpus — each consensus rule violated alone | `fuzz-native/negative-corpus.sh` (#223) | 7 rules | **7/7 refused** |
| 9 | Leaf preimage bytes — C++ spend-side, C++ create-side and Rust builders | `leaf-differential/` | — | agree |

The scenario counts are the manual campaigns. CI runs smaller ones on every push — `seam_fuzz.py 20000`,
its `--control` and `parse_fuzz.py 30000` (`.github/workflows/adversarial.yml`) — plus the unit tests of
`audit-fuzz` and `guest-pure-fuzz`, and `leaf-differential`. The libFuzzer campaigns (1, 1c, 1d) and
`fuzz-native/` (5–8) do **not** run in CI.

### 1 — Utreexo accumulator (`../audit-fuzz/FINDINGS.md`)
A non-Core component, and the SEC-2 soft spot. A `Forest` oracle held in lockstep with the guest's
hardened `Stump`, driven with honest ops, tamper-from-honest forged spends, **fully attacker-authored
proofs**, and arbitrary `verify` calls. Asserts soundness / atomicity / completeness / no-panic every
op. ~893k execs across three campaigns, zero failures. Control, as recorded: the then-unhardened
reference crashed on the SEC-2 location-confusion class in <1 s, and the guest rejected that exact
input in 1 ms. ⚠ The reference has since been hardened (`8e789a9`, #63) and the control has not been
rerun; `audit-fuzz/FINDINGS.md` says why it may or may not still fire, and gives the command.

### 2 — Coordinator seam (`../coordinator/SEAM_FINDINGS.md`)
`_frontier_chain` stitches verified ranges into the genesis frontier. A randomized model-checker over
a tiny symbolic alphabet drives the real DB-backed function, checked against an independent DFS oracle
enumerating every legitimate genesis-anchored seam-path. 200k scenarios, zero over-reports / splices /
crashes. Control: removing the H9 height guard is caught in the first scenarios.

### 3 — Coordinator input handlers
`parse_range` (claim-id aliasing / bounds), `clean_handle` (stored-XSS choke-point), `is_hex`. 300k
random/adversarial inputs, no crash, all invariants held.

### 4 — Guest pure-Rust consensus helpers (`guest-pure-fuzz/`)
> **2026-07-27:** this harness had stopped building. `build.rs` extracts items verbatim from the guest,
> and `block_script_flags` + the two exception hashes had moved from `main.rs` into `script_flags.rs`
> during the chainparams carve, so extraction failed on `BIP16_EXCEPTION`. It rotted precisely because
> nothing ran it. Fixed (the extractor now searches every guest source) and **added to CI**, along with
> `audit-fuzz`'s unit tests, so it cannot rot again.
`build.rs` extracts `block_script_flags`, `add256`, `median_time_past` and the flag/height constants
they depend on verbatim from the guest sources at compile time (zero drift), and the tests check them
against independent references:
- **`block_script_flags`** — the soft-fork activation heights (DERSIG 363725, CLTV 388381, CSV
  419328, NULLDUMMY 481824) are asserted to match canonical Bitcoin **mainnet** chainparams with
  correct off-by-one boundaries (OFF at h-1, ON at h), plus exception-block handling (the BIP16 and
  Taproot exception hashes), retroactive base flags, and monotonicity in height. Directly addresses
  the "height-gated rules" soft spot. The C++-side script flags (`verify_input.cpp`) are out of
  scope here.
- **`add256`** — 256-bit little-endian add-with-wrap vs an independent u128-limb reference, 500k
  random + carry-edge vectors.
- **`median_time_past`** — sorted-middle semantics + order-independence, 200k random windows.

## What is NOT covered (and why)

These carry real soundness weight but need the prover or the Core build — out of reach here, and the
right targets for an external audit:

1. **`verify-any` boundary-digest binding** — the whole seam argument assumes the digest from
   `verify-any` faithfully, collision-resistantly commits the full boundary (UTXO roots + difficulty
   + MTP + tip). If it under-binds a field, seam-fuzzing cannot see it. Needs real receipts.
2. **Core-in-zkVM script/consensus parity** — that the guest's `VerifyScript`/sighash/`libsecp256k1`
   and the C++ consensus math (`block_subsidy`, `calc_next_bits`, `ComputeMerkleRoot`) match real
   Core on adversarial blocks. This is the project's central claim, and it is now **partly** covered:
   - **Native C++ harnesses — landed (#222).** `fuzz-native/build.sh` compiles the guest's C++ shim and
     the Core TUs listed in `prover/methods/guest/build.rs` for the host, and three passes run against
     it: **differential** (`differential.cpp` — subsidy, the retarget timespan clamps, the
     CVE-2012-2459 merkle mutation, cumulative work), **memory-safety** under ASan+UBSan
     (`memsafety.cpp`), and **real-vector** (`realvector.cpp` — real mainnet inputs, with a signature
     byte-flip that must stop verifying). The prerequisite this item used to name is `build.sh` itself.
     Core is portable but the provisioned tree is not: `build.sh` overlays pristine copies of the two
     files `patches/0001` and `0002` change.
   - **In-zkVM negative corpus — landed (#223).** `fuzz-native/negative-corpus.sh` violates seven rules
     one at a time — nBits, weight, sigops, subsidy, merkle, PoW, BIP34 — against a positive control,
     in execute mode: 7/7 refused.
   - **Still not covered.** The native harnesses build libsecp on its stock field backend, not the
     shipped `field_bigint2` backend (`patches/0012`) or the `lift_x` hint (`0013`), which need the
     guest. The negative corpus has **no bad-signature case**, so no in-guest run shows the shipped
     CORE guest rejecting a corrupted signature (`docs/FIELD_BIGINT2_BACKEND.md` §5b, gate 4). Premature
     locktime and BIP30 are not in the corpus either, and nothing in `fuzz-native/` runs in CI.
3. **Recursion / `METHOD_ID` fold binding** — that a doctored guest image or a spliced recursion level
   can never yield an accepted tip proof. Exercises `env::verify(self_image_id, …)` and the fold seam
   that `seam_fuzz.py` only models at the coordinator layer. Needs the proving stack.

## Gotchas

- `cargo-fuzz` needs the **nightly** toolchain; use `-rss_limit_mb` to guard against OOM.
- Native/CMake builds on a small box: keep parallelism low (`-j2`) — `cc1plus` OOMs above that.
- The guest C++ needs the risc0 riscv toolchain only for the *guest* build; a host differential uses
  the system g++ — different flags, same source file list.

## Reproduce

All paths are from the repo root.

```bash
# 1 — accumulator (needs nightly + cargo-fuzz)
cd audit-fuzz && cargo test --release
cargo +nightly fuzz run delete_soundness            -- -max_total_time=120   # clean
cargo +nightly fuzz run delete_soundness_reference  -- -max_total_time=60    # control: unverified since #63 — audit-fuzz/FINDINGS.md

# 2, 3 — coordinator (stdlib python3)
cd coordinator
python3 seam_fuzz.py 200000     # clean
python3 seam_fuzz.py --control  # control: caught
python3 parse_fuzz.py 300000    # clean

# 4 — guest pure-Rust helpers (stable rust, extracts from the guest sources at build time)
cd guest-pure-fuzz && cargo test --release
```

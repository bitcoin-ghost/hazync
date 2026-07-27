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
| 1c | Accumulator reference `Stump` (pre-SEC-2) — positive control | same | — | crashes <1s (expected) |
| 2 | Coordinator seam `_frontier_chain` (S1/F1/H9) | `coordinator/seam_fuzz.py` | 200k | **clean** |
| 2c | Seam control, H9 height-guard removed — positive control | same `--control` | — | caught (expected) |
| 3 | Coordinator untrusted-input handlers (`parse_range`, `clean_handle`, `is_hex`) | `coordinator/parse_fuzz.py` | 300k | **clean** |
| 4 | Guest pure-Rust helpers (`block_script_flags`, `add256`, `median_time_past`) | `guest-pure-fuzz/` (build-time extract + refs) | 700k+ | **clean** |

### 1 — Utreexo accumulator (`../audit-fuzz/FINDINGS.md`)
The one non-Core component; the SEC-2 soft spot. A `Forest` oracle held in lockstep with the guest's
hardened `Stump`, driven with honest ops, tamper-from-honest forged spends, **fully attacker-authored
proofs**, and arbitrary `verify` calls. Asserts soundness / atomicity / completeness / no-panic every
op. ~893k execs across three campaigns, zero failures. Control: the unhardened reference crashes on
the SEC-2 location-confusion class in <1s; the guest rejects that exact input in 1 ms.

### 2 — Coordinator seam (`../coordinator/SEAM_FINDINGS.md`)
`_frontier_chain` stitches verified ranges into the genesis frontier. A randomized model-checker over
a tiny symbolic alphabet drives the real DB-backed function, checked against an independent DFS oracle
enumerating every legitimate genesis-anchored seam-path. 200k scenarios, zero over-reports / splices /
crashes. Control: removing the H9 height guard is caught in the first scenarios.

### 3 — Coordinator input handlers
`parse_range` (claim-id aliasing / bounds), `clean_handle` (stored-XSS choke-point), `is_hex`. 300k
random/adversarial inputs, no crash, all invariants held.

### 4 — Guest pure-Rust consensus helpers (`guest-pure-fuzz/`)
`build.rs` extracts `block_script_flags`, `add256`, and `median_time_past` verbatim from
`main.rs` at compile time (zero drift), and the tests check them against independent references:
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
   Core on adversarial blocks. This is the project's central claim. Two feasible-but-heavier angles:
   - **Native C++ differential — the highest-value next step.** Fuzz the real Core consensus code the
     guest actually runs, natively (no zkVM), against independent references and real mainnet vectors.
     Concretely: stand up `$HAZYNC_BASE` (Bitcoin Core at the pinned rev + `secp256k1` + `coreshim`)
     the way `provision-vps.sh` does — that is the prerequisite the repo doesn't ship. Compile the
     guest's C++ shim (`prover/methods/guest/verify_input.cpp` + `cshims.c`) and the ~17 Core TUs
     listed in `prover/methods/guest/build.rs` **for the host** (x86-64 g++, same file list minus the
     `-march=rv32im` flags — Core consensus code is portable). Then wrap the `extern "C"` exports
     (`block_subsidy`, `calc_next_bits`, `add_work`, `check_pow`, `merkle_root`, `check_tx`,
     `tx_wu_sigops`, `verify_input`, …) with Rust FFI + `cargo-fuzz` and run three angles:
     **differential** (each math export vs an independent reference — subsidy halving, the retarget /
     timespan-clamp math, an independent Merkle including the CVE-2012-2459 mutation flag);
     **memory-safety** (build with ASan and feed truncated/oversized/garbage `tx_bytes`, `prevouts`,
     `header`, `txids`, `cb` buffers — this wrapper glue is a soft spot `SECURITY.md` calls out);
     and **real-vector** (a corpus of real mainnet blocks/txs where every FFI result must match a
     trusted full node).
   - **In-zkVM negative corpus** — blocks Core rejects, each consensus rule violated one at a time
     (over-subsidy, bad merkle, weak flags, bad PoW, premature locktime, BIP30/34, weight/sigop
     over-limit), which must all fail to produce a receipt. Pair with a positive corpus that must all
     prove. Requires the prover.
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
cargo +nightly fuzz run delete_soundness_reference  -- -max_total_time=60    # control: fast crash

# 2, 3 — coordinator (stdlib python3)
cd coordinator
python3 seam_fuzz.py 200000     # clean
python3 seam_fuzz.py --control  # control: caught
python3 parse_fuzz.py 300000    # clean

# 4 — guest pure-Rust helpers (stable rust, extracts from main.rs at build time)
cd guest-pure-fuzz && cargo test --release
```

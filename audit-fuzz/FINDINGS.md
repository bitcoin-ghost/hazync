# Hazync accumulator — adversarial fuzz report

**Target:** the Utreexo UTXO accumulator, the one non-Core component and the soft spot most
flagged in `SECURITY.md` (SEC-2 was an accumulator `delete` bug; the top external-audit ask is
"especially of the accumulator"). It is also the only part fuzzable *hard* without a GPU or a
Core build.

**What is under test:** the guest's SEC-2-hardened `Stump::delete`
(`prover/methods/guest/src/utreexo.rs`) — the code that runs in the zkVM and carries the
soundness. The harness `#[path]`-includes that exact file (zero drift); its only non-portable
dependency is `sha2`, whose host output is bit-identical to the RISC0-accelerated build.

**Method:** coverage-guided libFuzzer (`cargo-fuzz`) + `arbitrary`-structured scenarios. A
ground-truth `Forest` oracle (the honest bridge, from the reference crate) is kept in lockstep
with a guest `Stump`. The fuzzer drives honest adds/deletes **and forged deletes** — attacker
chosen `i`, borrowed proofs from other coins, tampered positions/leaves/siblings, wrong-height
proofs, non-rightmost `proof_last`, out-of-range `i`. After every op the harness asserts:

| Property | Assertion |
|---|---|
| **Soundness** | if `delete` returns `true`, the resulting roots MUST equal honestly deleting the coin genuinely at position `i` (swap-and-shrink). Otherwise a spent coin survived / the wrong coin was removed / state was injected. |
| **Atomicity** | if `delete` returns `false`, the accumulator is byte-for-byte unchanged (no partial mutation on a rejected spend). |
| **Completeness** | an honest spend of a real coin is always accepted (else valid blocks can't be proven). |
| **No panic** | no input may panic (libFuzzer treats a panic as a crash). |

## Results

### Guest (the authority) — CLEAN
- `delete_soundness` target across three campaigns — 344,161 / 91 s, 314,271 / 241 s, and
  (after adding fully-arbitrary-proof ops) **235,174 / 201 s** — **~893k adversarial executions
  total, zero failures** (soundness / atomicity / completeness / panic). Adding the
  `DeleteArbitrary` + `VerifyArbitrary` ops raised coverage from cov 486 / ft 3007 to
  **cov 606 / ft 3533**, confirming the new structural proof space is genuinely reached. RSS flat
  ~500 MB.
- Ops exercised: honest add/delete, tamper-from-honest forged deletes (borrowed proofs, mangled
  positions/leaves/siblings, wrong heights, non-rightmost `proof_last`, out-of-range `i`), **fully
  attacker-authored proofs** (junk leaf / giant position / arbitrary sibling stack), and arbitrary
  `verify` calls (panic-safety).
- The single input that crashes the unhardened reference (below) is handled by the guest
  cleanly in 1 ms (rejected, no mutation).

### Reference `Stump` — positive control (expected, NOT a shipping bug)
- `delete_soundness_reference` target: **crashes in < 1 s**, panic at
  `accumulator/src/lib.rs:120` (`tree_of` "position out of range").
- Mechanism: the reference `delete` verifies `proof_i`'s *membership* but trusts the caller's
  global index `i`; feeding an in-set `proof_i` with an out-of-range `i` reaches `tree_of(i)`
  and panics — the SEC-2 "trusted an unverified position" class. The guest closes it with the
  `if i >= self.num_leaves { return false }` guard (line 115) plus the position pin
  (`proof_i.position == i - off`, lines 118-121).
- This is a **positive control**, not a vulnerability: the reference `Stump` is documented as
  the readable spec, never run in the zkVM, and the host only ever feeds it honest proofs. Its
  value here is proving the harness is sensitive to exactly the bug class in question — a fuzzer
  that finds nothing is only meaningful once you've shown it *can* find something.

## Reproduce

```bash
cd audit-fuzz
cargo test --release                              # harness self-tests + SEC-2 rejection unit test
cargo +nightly fuzz run delete_soundness -- -max_total_time=120        # guest (clean)
cargo +nightly fuzz run delete_soundness_reference -- -max_total_time=60  # reference (fast crash, control)
```

## Honest limits of this pass

- Covers `add` / `verify` / `delete` on the accumulator only. It does **not** touch the
  recursion/fold binding, the `METHOD_ID` plumbing, the coordinator seam checks, or the
  Core-in-zkVM script validation — all separately flagged in `SECURITY.md` and worth their own
  harnesses.
- The guest logic is fuzzed **natively** (host `sha2`). That is sound for the accumulator's
  index/tree arithmetic — the property under test is combinatorial, not hash-dependent — but it
  does not exercise the actual RISC0 execution environment.
- Absence of a finding in a time-boxed campaign is not a proof of correctness; it raises
  confidence in the hardened `delete` against the SEC-2 class, nothing more.

## 2026-08-04 — `forest_cache_equivalence`: the cached Forest vs the one it replaced (hazync#50, item 2)

New target. #40 rewrote `Forest` to cache internal nodes (a 185x speedup) *underneath* the exhaustive
small-n equivalence the accumulator's assurance rests on. The evidence offered then was byte-identical
bundle output over 100 real blocks — strong, but evidence about those blocks, not a property over all
inputs. #50 asked for the structure to be driven directly. This does that.

**Result: 192,101 runs, no crashes, no divergence.**

It asserts three things at every step, ordered by what they can catch:

1. the **leaf vector** against a plain `swap_remove` model — deliberately not derived from the forest,
   since (2) and (3) recompute over the model and would agree with each other even if the forest's own
   leaf bookkeeping were wrong;
2. the **cached roots** against `hazync_utreexo::reference::naive_roots`;
3. **proofs** — all of them while the forest is small, otherwise both ends plus a moving interior probe.

The reference is the crate's own `reference` module, the same code its unit tests compare against. That
matters: a private copy in this crate would only ever prove the copy self-consistent, which is the
failure the oracle exists to avoid. Lifting it out of `#[cfg(test)]` is what made one shared reference
possible.

**Mutation-checked** — deleting the `repair(i)` call after a delete's swap makes it fail on the first
divergent root, so a clean campaign means something.

Complements rather than repeats `exhaustive_single_delete_matches_naive`, which covers every index of
every size up to 40 but only a short fixed op sequence. This reaches long interleavings, where a cache
error can survive several operations before anything reads it.

`cargo run --release --example drive` runs the same property on stable without libFuzzer.

### Limits

- Still native-hash, like the rest of this crate: combinatorial index/tree arithmetic, not RISC0.
- Bounded to 300 ops per input, so unbounded-growth behaviour is out of scope.
- A time-boxed campaign finding nothing is not proof. #50's item 3, external review, is untouched by
  this and remains the highest-value spend.

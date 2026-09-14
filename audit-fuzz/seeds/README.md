# Fuzz seeds

The libFuzzer **corpus** (`fuzz/corpus/`) and **artifacts** (`fuzz/artifacts/`) are gitignored: they are
machine-generated coverage inputs that libFuzzer regenerates, and they were 1,160 files / 4.6 MB —
96% of the repo's file count for something reproducible on demand.

What is kept here is the small amount that carries meaning:

- **`sec2-position-crash.bin`** — the input that crashed the reference `Stump`
  (`delete_soundness_reference`, the positive control) on the SEC-2 location-confusion class: an
  attacker-chosen out-of-range position, on which the reference panicked in `tree_of`
  (`accumulator/src/lib.rs`). The **hardened guest rejects the same input cleanly** and returns `false`.

  ⚠ **The seed predates the reference's own hardening.** External review L-2 (`8e789a9`, #63) made the
  reference return `false` on an out-of-range `i` instead of panicking, so this input should no longer
  crash it. **A control that does not crash on this seed is therefore expected, and is not by itself
  evidence that the harness broke.** Whether the reference still trips the harness on some *other*
  input — it still lacks the guest's position pin — is unverified; `audit-fuzz/FINDINGS.md` gives the
  rerun that settles it.

  `scripts/check-test-surfaces.sh` checks only that this file exists, not that anything crashes on it.

```bash
# the hardened guest MUST NOT crash:
cd audit-fuzz && cargo +nightly fuzz run delete_soundness seeds/sec2-position-crash.bin
# the reference crashed on this before #63; since #63 it is expected to reject it (unverified):
cargo +nightly fuzz run delete_soundness_reference seeds/sec2-position-crash.bin
```

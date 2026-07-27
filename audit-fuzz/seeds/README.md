# Fuzz seeds

The libFuzzer **corpus** (`fuzz/corpus/`) and **artifacts** (`fuzz/artifacts/`) are gitignored: they are
machine-generated coverage inputs that libFuzzer regenerates, and they were 1,160 files / 4.6 MB —
96% of the repo's file count for something reproducible on demand.

What is kept here is the small amount that carries meaning:

- **`sec2-position-crash.bin`** — the input that crashes the *unhardened* reference `Stump`
  (`delete_soundness_reference`, the positive control) on the SEC-2 location-confusion class: an
  attacker-chosen out-of-range position, which the pre-SEC-2 code panics on at
  `accumulator/src/lib.rs`. The **hardened guest rejects the same input cleanly** and returns `false`.

  This is the evidence that the accumulator fuzzing means anything. A clean run of
  `delete_soundness` only demonstrates soundness if the harness provably detects the bug class —
  and this input is that proof. If the control ever stops crashing on it, the harness has been
  broken, not the bug fixed.

```bash
# control MUST crash:
cd audit-fuzz && cargo +nightly fuzz run delete_soundness_reference seeds/sec2-position-crash.bin
# the hardened guest MUST NOT:
cargo +nightly fuzz run delete_soundness seeds/sec2-position-crash.bin
```

# hazync#119 — instrumented preflight

`risc0-circuit-rv32im` is **not** vendored in the tree; this patch reconstructs the instrumented
copy from the registry so the 15 MB crate stays out of git history.

```sh
C=$(ls -d ~/.cargo/registry/src/*/risc0-circuit-rv32im-4.0.5 | head -1)
cp -r "$C" vendor/risc0-circuit-rv32im && chmod -R u+w vendor/risc0-circuit-rv32im
patch -p0 -d vendor/risc0-circuit-rv32im/src/prove/witgen \
  < patches/0119-instrument-preflight-checksum.patch
```

`prover/Cargo.toml`'s `[patch.crates-io]` already points at `../vendor/risc0-circuit-rv32im`, so a
build picks it up with no further change. The guest is untouched, so **METHOD_ID does not move**.

## Why here

The CPU and CUDA backends emit a **byte-identical** invalid seal for the same `(input, seed,
rand_z)` — `aabc2e71…`, 304,738 bytes, both. Preflight is the only code they share, so the bad
witness exists before either backend runs.

`HAZYNC_119_DUMP_CHECKSUM=1` dumps every input to the Poseidon2 memory checksum, so a failing
`rand_z` and a passing one can be diffed against the *same* memory transactions.

⛔ Diagnostic only, and slow — it writes a line per memory transaction.

## 0119-scan-check-poly.patch — risc0-zkp

Same reconstruct-from-registry approach:

```sh
Z=$(ls -d ~/.cargo/registry/src/*/risc0-zkp-3.0.5 | head -1)
cp -r "$Z" vendor/risc0-zkp && chmod -R u+w vendor/risc0-zkp
patch -p1 -d vendor/risc0-zkp/src/prove < patches/0119-scan-check-poly.patch
```

`HAZYNC_119_SCAN_CHECK=1` reports non-zero entries in the check polynomial — a witness satisfying
every constraint leaves it zero on the trace-domain points, so a non-zero entry names the row.

⛔ **The upstream `circuit_debug` feature cannot be used for this.** It does not compile in 3.0.5
(`verify/mod.rs:315`, `from_subelems` over the wrong item type), and it **changes the protocol**:
under that feature the verifier *reads* the DEEP point from the transcript instead of deriving it,
so a binary built with it cannot verify ordinary seals. This patch only observes.

## 0119-row-scan.patch — risc0-circuit-rv32im

```sh
patch -p1 -d vendor/risc0-circuit-rv32im/src/prove/hal < patches/0119-row-scan.patch
```

`HAZYNC_119_ROW_SCAN=1` reports **trace rows** at which a constraint is violated, by evaluating
`poly_ext(mix, u, args)` with the tap vector `u` read from the trace at row `r` — the same function
the verifier calls, but at a row instead of at the DEEP point.

Why it has to be here: the seal is rejected at risc0-zkp's DEEP-ALI check, so the witness violates a
constraint on the trace domain. `eval_check` cannot show which row — it evaluates on the extended
coset and divides by the vanishing polynomial, so it is non-zero everywhere by construction. Scanning
it reported 2,097,152 non-zero entries of 2,097,152 in **both** a failing and a passing run.

At the `commit_group(REGISTER_GROUP_ACCUM, ...)` call site the code/data/accum buffers are still in
**trace** form, so `poly_fp` can be evaluated at rate 1. `poly_mix` is arbitrary there: any mixing
non-zero at a row proves some constraint is violated at that row.

### Two approaches that do NOT work — recorded so nobody repeats them

1. **Scanning `check_poly`.** It is the quotient on the extended coset, non-zero everywhere by
   construction: 2,097,152 non-zero entries of 2,097,152, in *both* a failing and a passing run.
2. **Evaluating `poly_fp` at rate 1.** The vanishing polynomial is `(3x)^n - 1`, so risc0's trace
   domain is `(1/3)·μ_n`, **not** `μ_n` — and `poly_fp` derives `x` internally from
   `(cycle, domain)` with no way to pass an arbitrary point. It reported every row of every segment,
   including segments that verify.

Constraints are algebraic relations among tap values, so the evaluator works on values and has no
domain arithmetic to get wrong.

⚠ This finds the ROW. Naming *which* constraint additionally needs a `PolyExtStepDef` walk, for
which `circuit_debug`'s `mix_index` tracking is the mechanism.

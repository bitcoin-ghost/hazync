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

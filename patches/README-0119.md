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

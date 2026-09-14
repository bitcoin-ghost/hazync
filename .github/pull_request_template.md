## What changed


## Evidence
<!-- Commands run and their result. A test counts only if it can fail: say which positive control
     you ran, or why none applies. Measured numbers say what, on what, and what is still unmeasured. -->


## METHOD_ID impact
- [ ] Touches nothing the guest compiles: no change under `prover/methods/`, and none in a crate or
      `#[path]`-included file listed by `scripts/check-guest-inputs.sh` / `reproduce/METHOD_ID`
- [ ] Changes the guest. Even a comment moves the id. Say why this is worth a re-baseline and
      update `reproduce/METHOD_ID` (`scripts/rebaseline-id.sh`, `scripts/lineage.sh --check`)

## Docs
- [ ] Docs updated, or not needed because:

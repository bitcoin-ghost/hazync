# Hazync coordinator — range-seam fuzz report

**Target:** `server._frontier_chain()` — the coordinator's range-chaining seam logic, the S1/F1/H9
trust boundary in `SECURITY.md`. This is where independently-verified ranges are stitched into the
genesis-anchored frontier, and where earlier reviews found the coordinator chaining on a *weaker*
seam than the guest fold (a false-low-height / weak-flags splice, H9; a UTXO/difficulty/MTP
discontinuity, S1/F1).

**Threat model.** A submitter must hold a real STARK proof per range, so the boundary metadata
(`in_tip`/`out_tip`/`in_bhash`/`out_bhash`/`range_work`/`out_leaves`) is bound by `verify-any` and
cannot be freely forged. The attacker's freedom is *which* verified ranges to submit and what
legitimate boundary values they carry (e.g. proofs of alternative/low-difficulty histories). The
coordinator must never assemble those into a frontier that isn't a genuine genesis-anchored,
seam-continuous, **height-contiguous** chain.

**Method.** `seam_fuzz.py` — a randomized model-checker over a tiny symbolic alphabet (so tips and
boundary digests collide frequently and the seam logic is actually exercised), driving the **real,
DB-backed `_frontier_chain`**. Each scenario is checked three ways:

1. **No crash** — the real code must not raise on any row set (non-numeric `range_work`, NULL
   `out_leaves`, empty `in_bhash`, duplicate `in_tip`, cyclic `out_tip→in_tip`, …).
2. **Model fidelity** — a pure re-model of the walk must reproduce the real output exactly (guards
   against the oracle drifting from the code).
3. **Soundness (independent oracle)** — a DFS that enumerates *every* legitimate genesis-anchored
   seam-path (independent of the linear greatest-hi walk) must contain the real frontier. A splice,
   cycle-inflation, or mis-anchor produces a frontier tuple absent from that set.

> **2026-07-24 — MOST-WORK frontier + re-validation.** `_frontier_chain` now selects the **most-work**
> genesis-anchored chain (Bitcoin's rule): the seam graph is a DAG (`b.lo == a.hi+1` ⇒ `lo` strictly
> increases), and a single lo-order DP finds the chain with the greatest cumulative `range_work`, so a
> taller LOW-difficulty fork cannot out-vote the real chain. (This subsumes the earlier greatest-hi rule,
> itself the fix for the single-block-at-a-boundary stall — on the honest chain more blocks ⇒ more work.)
> The re-model (#2) mirrors the DP; soundness (#3) now requires the real frontier to be the **max-work**
> legitimate tuple, not merely *a* legitimate one. Tips stay **decoupled from `lo`** on purpose: `in_tip`
> is the block's real-parent hash but `lo` is the *claimed* height (forgeable for a pre-BIP34 block, since
> bip34 doesn't bind it), so the false-height splice H9 rejects is exactly a shared-`in_tip`/different-`lo`
> row — the harness must generate it, and the `--control` run (H9 removed) confirms the oracle still
> catches it. Re-run clean (real 0 findings; control >0).

The seam graph is a DAG (a valid seam requires `b.lo == a.hi + 1`, so `lo` strictly increases), so
the DFS enumeration terminates and is cheap.

## Results

### Real `_frontier_chain` — CLEAN
- `python3 seam_fuzz.py 200000` — **200,000 adversarial scenarios, 0 findings** (no crash, no
  model drift, no soundness violation).

### Positive control — CAUGHT (expected)
- `python3 seam_fuzz.py --control` removes the H9 height guard (`r.lo == prev_hi + 1`) — the
  pre-fix coordinator. The oracle catches a splice **within the first scenarios**: a range whose
  `lo` is not height-contiguous from genesis (e.g. `lo=3` spliced onto genesis) is chained even
  though the only legitimate frontier is the empty `(0, GEN, 0, 0)`.
- This proves the harness detects exactly the H9/S1 splice class and that the height guard is
  load-bearing — a clean result on the real code is therefore meaningful, not vacuous.

## Untrusted-input handlers — CLEAN (`parse_fuzz.py`)

Separate from the seam soundness, the coordinator's wire-facing parsers were property-fuzzed:

- `parse_range` — **300,000 random/adversarial claim ids, 0 findings.** No crash; every accepted id
  is a single block in `[0,TIP)` or an aligned exactly-`RANGE_SIZE` range in bounds; and all
  accepted *range*-form ids are pairwise equal-or-disjoint (the no-partial-overlap / no-double-claim
  invariant its docstring promises).
- `clean_handle` — output is always printable, length-capped, non-empty, and free of `<>&"'`
  (the stored-XSS choke-point holds against arbitrary unicode/control input).
- `is_hex` — never raises; accepts only exactly-`n`-byte round-trippable hex.

## Reproduce

```bash
cd coordinator
python3 seam_fuzz.py 200000     # seam soundness — expect "0 findings"
python3 seam_fuzz.py --control  # weakened (pre-H9) — expect ">0 findings" (harness has teeth)
python3 parse_fuzz.py 300000    # untrusted-input handlers — expect "0 findings"
```

## Honest limits

- This fuzzes the **chaining logic only**. It assumes the boundary digest from `verify-any` is a
  faithful, collision-resistant commitment to the full boundary (UTXO roots + difficulty + MTP +
  tip). That assumption is the Rust host's / guest's job and is **not** tested here — verifying it
  needs real receipts (the prover), out of scope for a GPU-less pass. If the digest under-binds
  (e.g. omits a field), this harness would not see it; that is the single most important thing left
  to check on this boundary.
- The genesis range's `in_bhash` is taken as pinned by `verify-any` (`assert_genesis_in_boundary`);
  the harness models it as trusted, matching the code's contract.
- **Frontier is most-work (resolved 2026-07-24).** Chain selection is arbitrated by cumulative
  `range_work`, matching Bitcoin — a taller low-difficulty genesis fork with less total work loses. (An
  attacker must still actually *prove* any fork's blocks with valid PoW to place them on the board; the
  most-work rule means doing so on a low-difficulty fork buys nothing.) Per-proof guarantees are
  unchanged; this only governs which of several *validly proven* chains the board treats as canonical.
- Symbolic alphabets are small by design (to force seam collisions); they exercise the control-flow
  thoroughly but are not real 32-byte digests. The property under test is combinatorial, not
  hash-dependent, so this is appropriate — but, as always, absence of a finding raises confidence,
  it does not prove correctness.

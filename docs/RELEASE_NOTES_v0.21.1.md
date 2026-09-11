# Hazync v0.21.1 — #119 is fixed

**The prover no longer emits receipts that fail their own `verify()`.** hazync#119 has cost
proving time since 2026-08-16. Across three weeks it was read as flaky silicon, then as
card-specific, then as a degree overflow. It was none of those. It's a witness-generation bug in
risc0's rv32im prover, it's deterministic once `rand_z` is pinned, and it's now fixed and
measured.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. The guest, the circuit
> and the verifier are unchanged. **Every existing proof stays valid, and the board does not
> reset.** v0.21.0 workers remain compatible with the board. They just keep hitting #119.

---

## What was wrong

A prover-side bug in `risc0-circuit-rv32im-sys` 4.0.3. It's line-for-line the same in the CPU and
CUDA kernels, which is why both backends emitted the same bad seal:

1. `step_TopAccum` stores only the LogUp columns a row's instruction uses. About 99% of rows leave
   some of columns 1..18 at `INVALID = 0xFFFFFFFF`.
2. Phase 3 of the accum pass then adds the running total `prev[k]` into every column, with
   `checked = false`. `INVALID + prev = prev − 1` is canonical garbage in an unconstrained cell,
   which is harmless. But when `prev[k] == 0` it's `0xFFFFFFFF − P = 0x87FFFFFE`, **≥ P**, and
   `eltwise_zeroize` (which clears only exact `INVALID`) doesn't catch it.
3. The NTT butterfly subtraction mishandles an input ≥ P when its partner is small, so one pair of
   rows of the committed polynomial stops matching the witness. The proof then fails to verify.

It's a completeness failure only: an honest proof failed to verify. Nothing invalid was ever
accepted.

## The fix

Zero the cell before adding `prev[k]`. That's two lines per kernel, in a vendored copy of
`risc0-circuit-rv32im-sys` 4.0.3 (`vendor/`, byte-for-byte from crates.io except the two sites
marked `HAZYNC_119_ACCUM_FIX`). Upstream PR: risc0/risc0#3804.

## Evidence

| | before | after |
|---|---|---|
| block 962,000 chunk 11 segment 21, pinned failing `rand_z` | rc=101, `check != result` 3/3 | **rc=0, 22/22 segments verify** |
| non-canonical witness cells in that segment | 13 | **0** |
| predicted fault rate, po2=21 segment | **4.5×10⁻⁴ (1 in ~2,200)** | 0 |

The mechanism predicted which 13 cells go non-canonical, and which 5 of them break, 13/13 correct.
The rate was measured by injecting the phase-3 value into the production GPU NTT on 44 real
segments (2,112 samples; the NTT breaks 11.1% of the time given `prev == 0`). It matches the
2026-09-08 campaign: **5 faults observed in 11,600 segments, 5.25 predicted.**

The full chain, with every elimination and the corrections along the way, is in hazync#119.

## Also in this release

- **#240: `prove-chunk` and `agg-chunks` retry per segment.** Before, a #119 fault surfaced after a
  chunk's whole run and took the chunk with it. The retry stays as defence in depth. Now that the
  root cause is fixed, a retry means *something new*, and it's still printed loudly so it gets seen.
- **#236: `seg-serve` streams segments** instead of executing and serialising everything before the
  listener binds. The throughput effect has **not been measured** yet.
- **#230: `agg-chunks` honours `HAZYNC_RECEIPTS`** (it resolved receipt names against its own CWD).
- **#231: spine races are no longer blamed on the guest id.**
- **#233: `COST_PER_EC_OP_REPEAT` is tunable and measured.**
- **#232: the decommissioned prover's rate-limit exemption is removed** (coordinator).
- #239 warning cleanup; #234 archive tags; #238 / #241 / #242 / #243 benchmark docs and tools.

## Upgrading

Replace the host binary; nothing else changes. The worker, bundles, identity (`~/.hazync`) and
board are untouched. Verify the download against the signed `SHA256SUMS.txt`, and confirm
`hazync-host method-id` prints `37987b85…`.

⚠ The vendored crate adds ~20 MB of generated kernel source to the repo. It goes once upstream
ships a fixed 4.0.x.

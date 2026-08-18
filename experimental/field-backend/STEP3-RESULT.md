# Step 3 measurement: 1.67x with `sys_bigint`

Block 962,000, 8,006 inputs, execute mode, same machine, same day.

```
baseline (10x26)     17,394,637,671 cycles   2,172,700 per input   838s
hzfe + sys_bigint    10,388,552,466 cycles   1,297,596 per input   334s
                              1.67x            40% fewer cycles
```

**Correct.** Identical `tip_hash 4403cf83deb7d52f04ed0c2a1d70aa9d48d435ef2a1201000000000000000000`,
all consensus flags true. The backend validates a real near-tip block to the same answer as stock
libsecp. The 1.4M differential comparisons held up in production use.

**Far below the 6-8x projected.** Working backwards, a `sys_bigint` call costs about **678 cycles**,
against the 10-100 instruction-equivalents the projection assumed, and only ~40% less than the
1,141-instruction software multiply it replaces. Every other input to that projection was measured;
the one that was estimated is the one that was wrong, and it was the one that mattered.

## The likely reason, stated as a hypothesis rather than a conclusion

**This used the wrong precompile.** `docs/ACCELERATION.md` is titled "accelerate libsecp256k1 modular
multiplication via the RISC0 **bigint2** precompile" and refers to bigint2 throughout. The ~7x
precedent comes from the removed k256 experiment, and Step 0 describes k256 as using `risc0-bigint2`.

This implementation used `sys_bigint` — the older 256-bit `OP_MULTIPLY` syscall — because that is what
`risc0-zkvm-platform` exposes directly and what Step 0's own "Precompile API (unknown #2) - resolved"
paragraph documents. They are different accelerators:

| | |
|---|---|
| `sys_bigint(result, op, x, y, modulus)` | one 256-bit modmul, direct syscall, what was used here |
| `sys_bigint2_*(blob_ptr, a1..a6)` | invokes a compiled bigint2 *program*; needs the `risc0-bigint2` crate for blobs and wrappers |

`risc0-bigint2` is not vendored in the local registry, so switching is a real piece of work rather than
a one-line change, and whether it reaches 7x here is **untested**.

## What this does and does not settle

Settled:

- The field backend is **correct** end to end on real consensus data.
- The backend approach **works**: 40% of the cycles genuinely disappear, and the EC layer runs happily
  without a magnitude system.
- `sys_bigint` alone is **not enough** to justify a re-baseline. 1.67x takes a near-tip block from
  9.72 GPU-hours to about 5.8. Real, but not the step change the project needs.

Not settled:

- Whether bigint2 delivers the 7x the k256 experiment recorded.
- Whether that 7x is even comparable: k256 substituted the entire EC implementation, which this
  deliberately does not.

## Against the stop condition

#129 said: under ~4x, reconsider rather than proceed to Step 4. **1.67x is under 4x.** So Step 4 is not
started. The next move is either to try bigint2 or to stop, and that is a decision about how much more
to spend, not a technical unknown to grind at.

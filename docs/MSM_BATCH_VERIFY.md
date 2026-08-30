# Batch-verifying ECDSA with an MSM — design, and what needs review

**Status: the MSM PRIMITIVE is written and self-tested. The batch-verification protocol below is
DESIGN ONLY and is deliberately not implemented.** The MSM is ordinary engineering; the protocol is
consensus-critical cryptography and should be reviewed by someone other than its author before any
code exists.

## 1. Why this is the last big chunk-side lever

MEASURED on the current stack: `hazync_ecmult_verify` is the largest single term in the chunk work,
**561 M cycles over 7,015 verifies = 79,971 cycles each**. Everything else has been cut around it, so
it is now the dominant cost and nothing short of changing the *algorithm* touches it.

`sys_ec_add` / `sys_ec_double` are direct bigint2 FFI calls, so a point addition is ONE accelerated
operation rather than a software affine inversion. That is what makes Pippenger viable here — the
usual reason MSM needs projective coordinates does not apply.

| | point ops | vs naive | cycles |
|---|---|---|---|
| naive, 7,015 double-scalar-muls | 3,591,680 | — | 561 M (measured) |
| **Pippenger, w = 10, N = 14,031** | **418,310** | **8.6x** | **~65 M (derived)** |

⚠ The 156 cycles/op that scales this comes from a MEASURED total divided by an ASSUMED 512 ops per
Shamir double-scalar-mul. If `double_scalar_mul` uses windowing or GLV the real op count is lower and
the per-op cost higher, shrinking the advantage. **Count `sys_ec_add` calls before believing 8.6x.**

## 2. The obstacle: ECDSA does not batch natively

Verification checks `r == x(u1*G + u2*Q) mod n`. That is an **x-coordinate comparison**, which is not
linear, so verifications cannot simply be summed. And Bitcoin signatures carry no recovery id, so `R`
cannot be recovered cheaply.

## 3. The proposal: hint `R`, verify it, batch only the linear part

Same advice-and-verify shape as #205's Y hint.

1. The host supplies `R_i` for each signature.
2. The guest checks `x(R_i) == r_i` per signature — cheap, and it is the actual ECDSA acceptance test.
3. The guest must still establish `R_i = u1_i*G + u2_i*Q_i`. That part IS linear, so batch it:

```
   z_i  = H(domain ‖ i ‖ r_i ‖ s_i ‖ m_i ‖ Q_i ‖ R_i)      Fiat-Shamir, 128-bit
   check   (Σ z_i*u1_i)*G  +  Σ (z_i*u2_i)*Q_i  −  Σ z_i*R_i  =  O
```

One MSM over `2n+1` points.

**Soundness sketch.** If any `R_i != u1_i*G + u2_i*Q_i`, the sum is a non-trivial linear combination
of a non-zero point with coefficients the prover could not predict, so it vanishes with probability
~`2^-128`. All-or-nothing is the CORRECT semantics for consensus: a block with any invalid signature
is invalid, and no one needs to know which.

## 4. What must be got right, and what I would want reviewed

⛔ **The `z_i` must be unpredictable to whoever supplies `R_i`.** They are derived by hashing every
input INCLUDING `R_i`, so a prover choosing `R_i` cannot steer them. If any input is omitted from that
hash the scheme breaks. **This is the part to review hardest.**
⛔ **`z_i` must be per-signature.** A single shared `z` collapses to summing the equations and is
trivially forgeable.
⛔ **Degenerate points.** `R_i` at infinity, `Q_i` off-curve or not in the subgroup, `s_i = 0` — every
one must be rejected BEFORE the batch, on the individual path, exactly as libsecp does today.
⚠ **This replaces libsecp's per-signature verification entirely.** Far beyond #139's middle path,
which keeps every check and moves only the group arithmetic. Firmly Ghost-class, and it should never
be enabled in a Core-model build.
⚠ **438 KB of `R` hints** on the wire at 64 B per signature, against a 3.6 MB packed witness.

## 5. Status and the honest next step

✅ `prover/methods/guest/src/msm.rs` — Pippenger, feature `msm`, guest mode 13 self-test comparing
against an independently written naive reference **and a negative control** (a perturbed scalar must
change the result; without that a `msm` returning the reference's own answer would pass).
⛔ Nothing above is measured on hardware. ⛔ No batch verification exists.

⏰ **Next: run mode 13, count the `sys_ec_add` calls, and confirm the 8.6x before writing a line of
protocol.** If the op-count model is wrong the whole case changes, and that is cheap to find out.

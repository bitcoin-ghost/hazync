//! Pippenger multi-scalar multiplication over the bigint2 coprocessor.
//!
//! ⛔ EXPERIMENTAL. This module is a PRIMITIVE ONLY — it is deliberately NOT wired into any
//! verification path. Batch-verifying ECDSA on top of it is a separate, larger step with a soundness
//! argument that wants review by someone other than its author. See `docs/MSM_BATCH_VERIFY.md`.
//!
//! ## Why this is worth having
//!
//! `hazync_ecmult_verify` is the largest single term left in the chunk work. MEASURED: 561 M cycles
//! over 7,015 verifies = **79,971 cycles per double-scalar-mul**. A Shamir double-scalar-mul over 256
//! bits is ~256 doublings + ~256 additions, so a coprocessor point op costs **~156 cycles**.
//!
//! `sys_ec_add` and `sys_ec_double` are direct bigint2 FFI calls (`curve/ops.rs`), so a point
//! addition is ONE accelerated operation rather than a software affine inversion. That is what makes
//! Pippenger worth doing here — the usual reason MSM needs projective coordinates does not apply.
//!
//! DERIVED, at n = 7,015 signatures (2n+1 = 14,031 points):
//!
//! ```text
//!   naive   7,015 x 512 ops        = 3,591,680 ops
//!   w = 10  windows x (N + 2^11)   =   418,310 ops   8.6x fewer   ~65 M cycles vs 561 M
//! ```
//!
//! ⚠ Window 10 is the optimum of the op-count model, not a measurement. The model ignores bucket
//! memory traffic (2^10 points is 64 KB of live state), which the guest pays for in paging. **Sweep
//! it against a real run before trusting 10.**
//!
//! ## What is NOT claimed
//!
//! ⛔ No speedup here is measured. The 156 cycles/op comes from a measured total divided by an
//! ASSUMED op count for Shamir's trick; if `double_scalar_mul` uses windowing or GLV, the real op
//! count is lower and the per-op cost higher, which would shrink the advantage. The honest way to
//! settle it is to count `sys_ec_add` calls directly, which `selftest` below makes possible.

use alloc::vec;
use alloc::vec::Vec;
use risc0_crypto::curves::secp256k1::{Affine, Fr};

/// Big-endian bit `i` (0 = least significant) of a 32-byte scalar.
#[inline]
fn bit_be(b: &[u8; 32], i: usize) -> u32 {
    ((b[31 - (i >> 3)] >> (i & 7)) & 1) as u32
}

/// The `window`-bit digit starting at bit `lo`.
#[inline]
fn digit(b: &[u8; 32], lo: usize, window: usize) -> usize {
    let mut d = 0usize;
    for k in 0..window {
        if lo + k < 256 {
            d |= (bit_be(b, lo + k) as usize) << k;
        }
    }
    d
}

/// `Σ scalars[i] * points[i]`, by Pippenger's bucket method.
///
/// Returns the identity for an empty input. `points` and `scalars` must be the same length.
///
/// The scalars are consumed as 32-byte big-endian, matching `Fr::to_bigint().write_be_bytes()` and
/// therefore `secp256k1_scalar_get_b32` — the same interchange the rest of this tree uses.
pub fn msm(points: &[Affine], scalars: &[Fr], window: usize) -> Affine {
    assert_eq!(points.len(), scalars.len(), "msm: length mismatch");
    assert!((1..=16).contains(&window), "msm: window out of range");
    if points.is_empty() {
        return Affine::IDENTITY;
    }

    let bytes: Vec<[u8; 32]> = scalars
        .iter()
        .map(|s| {
            let mut b = [0u8; 32];
            s.to_bigint().write_be_bytes(&mut b);
            b
        })
        .collect();

    let nbuckets = 1usize << window;
    let mut acc = Affine::IDENTITY;
    let mut first = true;

    // Most significant window first, so the running accumulator is doubled `window` times per step.
    let mut lo = (256 / window) * window;
    loop {
        if !first {
            for _ in 0..window {
                acc = acc.double();
            }
        }

        let mut buckets = vec![Affine::IDENTITY; nbuckets];
        for (p, b) in points.iter().zip(bytes.iter()) {
            let d = digit(b, lo, window);
            if d != 0 {
                let cur = buckets[d];
                buckets[d].add_into(&cur, p);
            }
        }

        // Σ j*bucket[j] without multiplying: walk down accumulating a running suffix sum.
        let mut running = Affine::IDENTITY;
        let mut total = Affine::IDENTITY;
        for j in (1..nbuckets).rev() {
            let r = running;
            running.add_into(&r, &buckets[j]);
            let t = total;
            total.add_into(&t, &running);
        }

        let a = acc;
        acc.add_into(&a, &total);
        first = false;

        if lo == 0 {
            break;
        }
        lo -= window;
    }
    acc
}

/// Naive reference: `Σ scalars[i] * points[i]` by double-and-add, one point at a time.
///
/// Exists ONLY to check `msm` against. It is the obvious implementation, written separately rather
/// than sharing helpers, so a bug in the fast path is unlikely to be mirrored here.
pub fn msm_reference(points: &[Affine], scalars: &[Fr]) -> Affine {
    let mut acc = Affine::IDENTITY;
    for (p, s) in points.iter().zip(scalars.iter()) {
        let mut b = [0u8; 32];
        s.to_bigint().write_be_bytes(&mut b);
        let mut term = Affine::IDENTITY;
        for i in (0..256).rev() {
            term = term.double();
            if bit_be(&b, i) == 1 {
                let t = term;
                term.add_into(&t, p);
            }
        }
        let a = acc;
        acc.add_into(&a, &term);
    }
    acc
}

/// Compare `msm` against `msm_reference` on `n` deterministic points and report.
///
/// ⚠ A self-test that only ever compares equal values proves nothing, so this also runs a NEGATIVE
/// case: one scalar is perturbed and the two results must then DIFFER. Without that, a `msm` that
/// returned the reference's own answer — or one that returned identity for both — would pass.
pub fn selftest(n: usize, window: usize) -> (bool, bool) {
    let mut pts = Vec::with_capacity(n);
    let mut scs = Vec::with_capacity(n);
    let mut seed = [0u8; 32];
    seed[31] = 7;
    for i in 0..n {
        seed[0] = (i & 0xff) as u8;
        seed[1] = ((i >> 8) & 0xff) as u8;
        let s = Fr::from_be_bytes_mod_order(&seed);
        // GENERATOR * (i+1) keeps every point on-curve and in the subgroup without a decompression.
        let mut p = Affine::IDENTITY;
        for _ in 0..=(i % 5) {
            let q = p;
            p.add_into(&q, &Affine::GENERATOR);
        }
        pts.push(p);
        scs.push(s);
    }
    let fast = msm(&pts, &scs, window);
    let slow = msm_reference(&pts, &scs);
    let positive = fast.xy() == slow.xy();

    // Negative control: perturb one scalar; the results must diverge.
    let mut scs2 = scs.clone();
    if let Some(s0) = scs2.first_mut() {
        let mut b = [0u8; 32];
        s0.to_bigint().write_be_bytes(&mut b);
        b[31] ^= 1;
        *s0 = Fr::from_be_bytes_mod_order(&b);
    }
    let perturbed = msm(&pts, &scs2, window);
    let negative = perturbed.xy() != slow.xy();

    (positive, negative)
}

//! hazync#205 / G1 — recover a public key's Y through the bigint2 coprocessor instead of libsecp's
//! software modular square root.
//!
//! ⛔ EXPERIMENTAL, opt-in, and NOT part of the shipped guest. Enabled by the `liftx-accel` cargo
//! feature, so a default build compiles exactly as before and `METHOD_ID` is unmoved.
//!
//! ## Why this is the largest term left
//!
//! After #139 accelerates the group arithmetic, a pprof of block 962,000 (whole block as one chunk,
//! execute mode) attributes **1,415,786,221 cycles — 9.83% of the block and ~45% of all post-#139
//! work — to `secp256k1_ge_set_xo_var`**, reached via `ec_pubkey_parse` for compressed ECDSA keys
//! and via `xonly_pubkey_load`/`lift_x` for taproot. `patches/0005` does not touch it: it lives
//! outside `secp256k1_ecmult`.
//!
//! libsecp's own `bench_internal` prices the operation at `field_sqrt` **6.50 us** against
//! `field_mul` **0.0246 us** — **264x** — matching a first-principles count of ~265 field ops
//! (exponentiation by (p+1)/4 is ~255 squarings plus ~10 muls). Measured ratio and op count agreeing
//! to one part in 265 is what says the cost is ALGORITHMIC, and so carries from x86 to rv32im.
//!
//! ## Why this is nearly free to take
//!
//! risc0-crypto already does exactly this operation on the accelerator:
//!
//! ```text
//! pub fn sqrt(&self) -> Option<Self> {
//!     let root = self.pow(&P::MODULUS_PLUS_ONE_DIV_FOUR);
//!     if (&root * &root).check_is_eq(self) { Some(root) } else { None }
//! }
//! ```
//!
//! The same exponentiation libsecp performs, over bigint2 field arithmetic rather than the guest's
//! software `field_10x26`. ⇒ No witness change, no host change and no hints — unlike #205's
//! advice-and-verify route, which needs all three.
//!
//! ## What is and is not replaced
//!
//! ONLY the square root. `secp256k1_ge_set_xo_var` still computes `x3 = x^3 + 7` itself, still
//! normalises `y`, and still flips the sign to match the requested parity — so this returns the EVEN
//! root and libsecp's own code decides the rest. On failure it returns 0 and libsecp falls back to
//! its own `fe_sqrt`, which is also what rejects an x that is not on the curve.
//!
//! ⚠ Same posture as #139: a precompile is *constrained, not trusted* — the circuit proves it
//! computed correctly — so this adds no trust assumption beyond RISC0 itself. What it adds is a
//! plumbing obligation, that the big-endian limb <-> byte conversion here is right. That is what a
//! differential digest gate tests.

use risc0_crypto::curves::secp256k1::{Affine, Fq};

/// Recover the EVEN Y for the 32-byte big-endian `x`, writing it big-endian to `out_y`.
///
/// Returns 1 on success, 0 if `x` is not the x-coordinate of any curve point — in which case the
/// caller falls back to libsecp's own sqrt, which reaches the same verdict.
///
/// # Safety
/// `xb` must be valid for 32 bytes and `out_y` writable for 32. Called only from
/// `secp256k1_ge_set_xo_var` under `patches/0007`.
#[no_mangle]
pub unsafe extern "C" fn hazync_lift_x(xb: *const u8, out_y: *mut u8) -> i32 {
    let mut x = [0u8; 32];
    core::ptr::copy_nonoverlapping(xb, x.as_mut_ptr(), 32);

    // A normalised libsecp field element is always < p, so the reduction is exact rather than lossy.
    let fx = Fq::from_be_bytes_mod_order(&x);

    // is_y_odd = false: return the EVEN root. ge_set_xo_var negates it itself when the caller asked
    // for the odd one, so one call serves both parities and the parity logic stays libsecp's.
    let Some(p) = Affine::decompress(fx, false) else { return 0 };
    let Some((_, y)) = p.xy() else { return 0 };

    y.to_bigint().write_be_bytes(core::slice::from_raw_parts_mut(out_y, 32));
    1
}

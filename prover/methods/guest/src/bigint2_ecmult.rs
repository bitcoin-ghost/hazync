//! hazync#139, MIDDLE PATH — swap ONLY the group arithmetic under libsecp's ECDSA verify.
//!
//! ⛔ EXPERIMENTAL, opt-in, and NOT part of the shipped guest. Enabled by the `bigint2-ecdsa`
//! cargo feature, which also gates the `risc0-crypto` dependency, so a default build compiles
//! exactly as before and `METHOD_ID` is unmoved.
//!
//! ## What is and is not replaced
//!
//! `secp256k1_ecdsa_sig_verify` (`ecdsa_impl.h`) is:
//!
//! ```text
//!   zero checks on r and s          <- libsecp
//!   scalar_inverse_var(sn, s)       <- libsecp
//!   u1 = sn*m ; u2 = sn*r           <- libsecp
//!   secp256k1_ecmult(pr, Q, u2, u1) <- THE ONLY LINE THIS REPLACES
//!   infinity check                  <- libsecp
//!   r == x(pr) mod n                <- libsecp
//! ```
//!
//! ⇒ DER parsing, low-S normalisation, pubkey parsing, the sighash and the final comparison all
//! remain Core's and libsecp's literal code. What changes is the elliptic-curve point arithmetic —
//! which is where essentially all of the cost is.
//!
//! ⚠ This does NOT keep libsecp's wNAF and GLV, contrary to how the middle path was first
//! described in hazync#139: `double_scalar_mul` is Shamir's trick over the accelerator, so the
//! scalar-multiplication *strategy* changes too. The equivalence surface is still far smaller than
//! substituting the whole `ecdsa` module, but it is not zero, and it should not be described as
//! "only the field arithmetic".
//!
//! ## Soundness posture
//!
//! A precompile is *constrained, not trusted* — the circuit proves it computed correctly. That is
//! the same argument already accepted for the SHA-256 accelerator in `patches/0002`, so this adds
//! no trust assumption beyond RISC0 itself. What it adds is a plumbing-correctness obligation:
//! that the limb <-> byte conversion here is right. That is what a differential gate tests.

use risc0_crypto::curves::secp256k1::{Affine, Fq, Fr};

fn rd(p: *const u8) -> [u8; 32] {
    let mut b = [0u8; 32];
    unsafe { core::ptr::copy_nonoverlapping(p, b.as_mut_ptr(), 32) };
    b
}

/// `out = u1*G + u2*Q`, all scalars and coordinates 32-byte BIG-ENDIAN, matching libsecp's
/// `secp256k1_scalar_get_b32` / `secp256k1_fe_get_b32`.
///
/// Returns 1 on success with `out_x`/`out_y` filled, 0 if the result is the point at infinity
/// (which libsecp treats as verification failure) or if the pubkey is not on the curve.
///
/// # Safety
/// All six pointers must be valid for 32 bytes. Called only from `secp256k1_ecdsa_sig_verify`.
#[no_mangle]
pub unsafe extern "C" fn hazync_ecmult_verify(
    out_x: *mut u8,
    out_y: *mut u8,
    u1: *const u8,
    u2: *const u8,
    qx: *const u8,
    qy: *const u8,
) -> i32 {
    // Scalars: libsecp's u1/u2 are already reduced mod n, so reducing again is a no-op and keeps
    // this total. double_scalar_mul documents that it accepts scalars >= n in any case.
    let s1 = Fr::from_be_bytes_mod_order(&rd(u1));
    let s2 = Fr::from_be_bytes_mod_order(&rd(u2));

    // Coordinates: a parsed libsecp pubkey always has x, y < p, so mod-p reduction is exact.
    let x = Fq::from_be_bytes_mod_order(&rd(qx));
    let y = Fq::from_be_bytes_mod_order(&rd(qy));

    // libsecp has already validated the pubkey; new() re-checks on-curve, which is cheap next to
    // the multiplication and keeps this function total rather than trusting the caller.
    let Some(q) = Affine::new(x, y) else { return 0 };

    let r = Affine::double_scalar_mul(&s1, &Affine::GENERATOR, &s2, &q);
    let Some((rx, ry)) = r.xy() else { return 0 };  // None == point at infinity

    rx.to_bigint().write_be_bytes(core::slice::from_raw_parts_mut(out_x, 32));
    ry.to_bigint().write_be_bytes(core::slice::from_raw_parts_mut(out_y, 32));
    1
}

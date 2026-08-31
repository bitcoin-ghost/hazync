//! Coprocessor primitives for libsecp's `field_bigint2` backend.
//!
//! ⛔ EXPERIMENTAL, opt-in. Compiled only with the `field-bigint2` feature; see
//! `docs/FIELD_BIGINT2_BACKEND.md` and `patches/0012`.
//!
//! ## Why this exists
//!
//! MEASURED in-guest against libsecp's own functions: software `fe_mul` is **1,167 cycles**, the
//! coprocessor is **83 in native form** and **854 once 10x26 conversion is added**. The conversion
//! costs 771 cycles — 9.3x the operation — so a bolt-on that keeps libsecp's representation is
//! worthless (1.10x on the block) and *slower than software on squaring*.
//!
//! The way to take the 14x is to make the representation canonical, so no conversion ever happens.
//! That is what the backend does, and this is the half that talks to the coprocessor.
//!
//! ⚠ **Limb layout is load-bearing.** libsecp's canonical form is 8 little-endian `uint32_t`, which
//! is byte-identical to the 32-byte little-endian form `BigInt` uses, so these are pointer casts and
//! not conversions. If either side ever changes representation this silently produces wrong field
//! elements — which is a consensus break, not a slowdown.

use risc0_crypto::bigint::BigInt;
use risc0_crypto::curves::secp256k1::Fq;

/// ⛔ **This boundary was 48% of the block before it was written this way.** MEASURED on block
/// 962,000 (`RISC0_PPROF_OUT`, execute mode): 13.07 M coprocessor calls costing 296 cy/call for
/// multiply and 208 for square, against a coprocessor operation measured at **83**. `memcpy` alone
/// went from 178 M cycles in the control to **790 M** — 4.4x — and essentially all of it was here.
///
/// The cause was three 32-byte copies per call where zero are needed. `BigInt<N>` is
/// `#[repr(transparent)]` over `[u32; N]` and secp256k1's `fe` is `uint32_t[8]`, so the two are the
/// same eight little-endian words: the limbs can be read and written straight through. The old code
/// copied into a `[u8; 32]` staging buffer, then `BigInt::from_le_bytes` copied *again* (it is a
/// `bytemuck` `copy_from_slice`, plus a length assert), and `store` copied a third time.
///
/// ⚠ Keep these as raw `read`/`write` of `[u32; 8]`. Anything that routes through a byte slice
/// reintroduces the staging copy, and it does not show up as a correctness failure — only as a
/// third of the block.
#[inline]
unsafe fn load(p: *const u32) -> Fq {
    // The lazy invariant admits values in [0, 2^256), so the reduction is required, not optional.
    // It is cheap: p has its MSB set, so reduce_from_bigint takes the msb_set() single-subtract path.
    Fq::reduce_from_bigint(BigInt::new(core::ptr::read(p as *const [u32; 8])))
}

#[inline]
unsafe fn store(v: &Fq, out: *mut u32) {
    core::ptr::write(out as *mut [u32; 8], v.as_bigint().0);
}

/// `out = a * b mod p`. Inputs and output are 8 little-endian `u32` limbs, canonical.
///
/// # Safety
/// All three pointers must be valid for 32 bytes. Called only from `secp256k1_fe_impl_mul`.
#[no_mangle]
pub unsafe extern "C" fn hazync_fq_mul_limbs(a: *const u32, b: *const u32, out: *mut u32) {
    store(&(&load(a) * &load(b)), out);
}

/// `out = a^2 mod p`.
///
/// # Safety
/// Both pointers must be valid for 32 bytes. Called only from `secp256k1_fe_impl_sqr`.
#[no_mangle]
pub unsafe extern "C" fn hazync_fq_sqr_limbs(a: *const u32, out: *mut u32) {
    let x = load(a);
    store(&(&x * &x), out);
}

/// `out = a^-1 mod p`, and `0` maps to `0` — which is what libsecp's own `fe_inv` contract says.
///
/// # Safety
/// Both pointers must be valid for 32 bytes. Called only from `secp256k1_fe_impl_inv{,_var}`.
#[no_mangle]
pub unsafe extern "C" fn hazync_fq_inv_limbs(a: *const u32, out: *mut u32) {
    let x = load(a);
    if x.is_zero() {
        core::ptr::write_bytes(out as *mut u8, 0, 32);
        return;
    }
    store(&x.inverse(), out);
}

/// Square root. Returns 1 and writes a root when `a` is a quadratic residue, 0 otherwise.
///
/// Used for `is_square_var` and by `fe_sqrt`; the caller decides which root it wants by parity, as
/// it already does with the software backend.
///
/// # Safety
/// Both pointers must be valid for 32 bytes.
#[no_mangle]
pub unsafe extern "C" fn hazync_fq_sqrt_limbs(a: *const u32, out: *mut u32) -> i32 {
    match load(a).sqrt() {
        Some(r) => { store(&r, out); 1 }
        None => 0,
    }
}

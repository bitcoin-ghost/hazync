//! hazync#205 — supply a public key's Y coordinate as a witness hint and VERIFY it, instead of
//! recovering it with a modular square root.
//!
//! ⛔ EXPERIMENTAL, opt-in, and NOT part of the shipped guest. Enabled by the `liftx-hint` cargo
//! feature, so a default build compiles exactly as before and `METHOD_ID` is unmoved.
//!
//! ## Why this is the largest remaining term
//!
//! After #139 accelerates the group arithmetic, a profile of block 962,000 (whole block as one
//! chunk, execute mode, `RISC0_PPROF_OUT`) attributes **1,415,786,221 cycles — 9.83% of the block
//! and ~45% of all post-#139 work — to `secp256k1_ge_set_xo_var`**, reached both via
//! `ec_pubkey_parse` (compressed ECDSA keys) and `xonly_pubkey_load`/`lift_x` (taproot keys).
//! `patches/0005` does not touch it: it lives outside `secp256k1_ecmult`.
//!
//! libsecp's own `bench_internal` prices the operation directly: `field_sqrt` 6.50 us against
//! `field_mul` 0.0246 us — **264x**, against a first-principles op count of ~265 (exponentiation
//! by (p+1)/4 is ~255 squarings + ~10 muls). Measured ratio and op count agreeing to within one
//! part in 265 is what says the cost is ALGORITHMIC, and therefore carries from native x86 to the
//! guest's rv32im.
//!
//! ## Soundness
//!
//! `secp256k1_ge_set_xo_var` already computes `x3 = x^3 + 7`, and already normalises `y` and flips
//! its sign to match the requested parity. So the hint only has to supply *a* root: the check
//! `y^2 == x3` plus that existing parity fixup accepts exactly what the sqrt would have returned.
//! An `x` that is not on the curve admits no such `y`, so the patch falls through to libsecp's own
//! sqrt and fails there as it always did. secp256k1 has prime order and no point of order 2, so
//! `y == 0` cannot arise.
//!
//! ⇒ **Advice-and-verify, not substitution.** Unlike #139 this replaces no group arithmetic and
//! argues no equivalence surface; libsecp still decides the result. A missing or wrong hint costs
//! only the sqrt we were paying anyway.

use alloc::vec::Vec;

/// Sorted by `x`, ascending, big-endian — the order `secp256k1_fe_get_b32` produces.
/// Guest execution is single-threaded, so a plain static is sufficient.
static mut TABLE: Vec<([u8; 32], [u8; 32])> = Vec::new();

/// Hint-hit accounting. A silently-empty table would simply reinstate the sqrt cost while every
/// gate still passed, so the host prints these and a run with 0 hits is a FAILED experiment rather
/// than a null result.
static mut HITS: u32 = 0;
static mut MISSES: u32 = 0;

/// Install the hint table. `pairs` must be sorted ascending by `x`; duplicates are harmless.
pub fn install(mut pairs: Vec<([u8; 32], [u8; 32])>) {
    pairs.sort_unstable_by(|a, b| a.0.cmp(&b.0));
    unsafe {
        TABLE = pairs;
        HITS = 0;
        MISSES = 0;
    }
}

/// `(hits, misses)` since `install`.
pub fn stats() -> (u32, u32) {
    unsafe { (HITS, MISSES) }
}

/// Look up `y` for the 32-byte big-endian `xb`. Returns 1 and fills `yb` on a hit, 0 on a miss.
///
/// The caller VERIFIES `y^2 == x^3 + 7` before using the value, so a wrong or hostile table
/// cannot change a verification outcome — only make it slower.
///
/// # Safety
/// `xb` must be valid for 32 bytes and `yb` writable for 32. Called only from
/// `secp256k1_ge_set_xo_var` under `patches/0006`.
#[no_mangle]
pub unsafe extern "C" fn hazync_lift_x_hint(xb: *const u8, yb: *mut u8) -> i32 {
    let mut x = [0u8; 32];
    core::ptr::copy_nonoverlapping(xb, x.as_mut_ptr(), 32);

    let t = &*core::ptr::addr_of!(TABLE);
    match t.binary_search_by(|probe| probe.0.cmp(&x)) {
        Ok(i) => {
            core::ptr::copy_nonoverlapping(t[i].1.as_ptr(), yb, 32);
            HITS += 1;
            1
        }
        Err(_) => {
            MISSES += 1;
            0
        }
    }
}

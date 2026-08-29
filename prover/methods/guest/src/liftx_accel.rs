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

use alloc::vec::Vec;
use risc0_crypto::curves::secp256k1::{Affine, Fq};

/// MEASURED on block 962,000: 6,913 verifying inputs decompress only **2,160 distinct keys** — the
/// same square root is computed 3.2x over. G1 made each one cheap; it did not stop us doing each one
/// three times. `hazync_lift_x` is still 12.15% of the post-G1 chunk work, so removing that
/// redundancy is worth ~8.4% of the block for a lookup.
///
/// Memoising a pure function changes nothing observable, so this is the `identical` fidelity class
/// in docs/MODELS.md -- not even advice-and-verify.
///
/// Sorted by `x`; binary search. A 2,160-entry table is ~135 KB, and a hit costs ~11 comparisons of
/// 32 bytes against ~265 field operations. Guest execution is single-threaded.
static mut MEMO: Vec<([u8; 32], [u8; 32])> = Vec::new();
static mut MEMO_HITS: u32 = 0;
static mut MEMO_MISS: u32 = 0;

/// `(hits, misses)` — printed by the chunk so an empty cache reads as a FAILED experiment rather
/// than a null result. A cache that silently never hits reinstates the full cost while every gate
/// still passes.
pub fn memo_stats() -> (u32, u32) { unsafe { (MEMO_HITS, MEMO_MISS) } }

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

    // Memo first: 3.2x of these calls are repeats of a key already decompressed.
    let memo = &mut *core::ptr::addr_of_mut!(MEMO);
    match memo.binary_search_by(|p| p.0.cmp(&x)) {
        Ok(i) => {
            core::ptr::copy_nonoverlapping(memo[i].1.as_ptr(), out_y, 32);
            MEMO_HITS += 1;
            return 1;
        }
        Err(_) => MEMO_MISS += 1,
    }

    // A normalised libsecp field element is always < p, so the reduction is exact rather than lossy.
    let fx = Fq::from_be_bytes_mod_order(&x);

    // is_y_odd = false: return the EVEN root. ge_set_xo_var negates it itself when the caller asked
    // for the odd one, so one call serves both parities and the parity logic stays libsecp's.
    let Some(p) = Affine::decompress(fx, false) else { return 0 };
    let Some((_, y)) = p.xy() else { return 0 };

    let mut yb = [0u8; 32];
    y.to_bigint().write_be_bytes(&mut yb);
    core::ptr::copy_nonoverlapping(yb.as_ptr(), out_y, 32);

    // Insert in sorted position so the search above stays valid. Insertion is O(n) memmove on a
    // 2,160-entry table, paid once per DISTINCT key, against ~265 field ops saved on every repeat.
    let memo = &mut *core::ptr::addr_of_mut!(MEMO);
    if let Err(pos) = memo.binary_search_by(|p| p.0.cmp(&x)) {
        memo.insert(pos, (x, yb));
    }
    1
}

/// hazync#205 / GHOST_GAINS — the ECDSA scalar inverse, on the coprocessor.
///
/// `patches/0005` deliberately KEEPS `secp256k1_scalar_inverse_var` as literal libsecp: the middle
/// path moves only the group arithmetic. That was invisible when an ECDSA verify cost 1.72 M cycles.
/// After bigint2 and G1 it is the fourth-largest term in the chunk work — a profile of the current
/// stack puts `secp256k1_modinv32_var` + `modinv32_update_de_30` at **146.7 M, 7.0%**.
///
/// `Fr::inverse()` is the same value over bigint2 arithmetic. libsecp has already rejected a zero
/// `s` by this point (the r/s zero checks run before the inversion), but this stays total anyway and
/// returns 0 on a zero input, which is what `secp256k1_scalar_inverse_var` does.
///
/// # Safety
/// `inb` valid for 32 bytes, `outb` writable for 32. Called only from `secp256k1_ecdsa_sig_verify`
/// under `patches/0008`.
#[no_mangle]
pub unsafe extern "C" fn hazync_scalar_inverse(inb: *const u8, outb: *mut u8) -> i32 {
    use risc0_crypto::curves::secp256k1::Fr;
    let mut a = [0u8; 32];
    core::ptr::copy_nonoverlapping(inb, a.as_mut_ptr(), 32);
    let x = Fr::from_be_bytes_mod_order(&a);
    if x.is_zero() {
        core::ptr::write_bytes(outb, 0, 32);
        return 1;
    }
    x.inverse().to_bigint().write_be_bytes(core::slice::from_raw_parts_mut(outb, 32));
    1
}

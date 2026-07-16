// Hazync EC acceleration (ACCELERATION.md) — route libsecp256k1's field/scalar modular multiplication
// through the RISC0 `sys_bigint` precompile (a 256-bit (x*y) mod m with an arbitrary modulus).
//
// This does NOT substitute libsecp's EC/ECDSA logic (unlike patches/0003 k256). It replaces ONLY the
// modmul primitive. libsecp's patched C (patches/0004) converts its field/scalar operands to 32-byte
// big-endian via its own get_b32 helpers and calls the two `#[no_mangle]` functions below.
//
// Soundness: sys_bigint is a CONSTRAINED precompile — the circuit proves (x*y) mod m — so this adds no
// trust beyond the RISC0 zkVM (same posture as the SHA-256 accelerator, patches/0002). The only
// correctness risk is this plumbing (byte order / reduction), which the differential-fuzz test gates.
//
// STATUS: draft, staged for box validation. VERIFY before relying on it:
//   1. `sys_bigint` path + `OP_MULTIPLY`/`WIDTH_WORDS` names in the pinned risc0 (3.0.5).
//   2. sys_bigint operand constraints (operands < modulus — satisfied: field elts < p, scalars < n).
//   3. Endianness end-to-end via the diff-fuzz test in ACCELERATION.md Step 2.

use risc0_zkvm_platform::syscall::{bigint, sys_bigint};

// secp256k1 field prime p and group order n, as [u32; 8] LITTLE-ENDIAN words (word 0 = least significant).
// p = 0xFFFFFFFF...FFFFFFFE FFFFFC2F ; n = 0xFFFFFFFF...FFFFFFFE BAAEDCE6 AF48A03B BFD25E8C D0364141
const P: [u32; 8] = [0xFFFFFC2F, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF];
const N: [u32; 8] = [0xD0364141, 0xBFD25E8C, 0xAF48A03B, 0xBAAEDCE6, 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF];

// 32-byte big-endian (libsecp get_b32 output) -> [u32; 8] little-endian words (sys_bigint input).
#[inline]
fn be32_to_le_words(b: &[u8; 32]) -> [u32; 8] {
    let mut w = [0u32; 8];
    for i in 0..8 {
        let j = 32 - 4 * (i + 1); // word i (LSW) is the last 4 bytes for i=0
        w[i] = u32::from_be_bytes([b[j], b[j + 1], b[j + 2], b[j + 3]]);
    }
    w
}

#[inline]
fn le_words_to_be32(w: &[u32; 8]) -> [u8; 32] {
    let mut b = [0u8; 32];
    for i in 0..8 {
        let j = 32 - 4 * (i + 1);
        b[j..j + 4].copy_from_slice(&w[i].to_be_bytes());
    }
    b
}

#[inline]
fn modmul(out: *mut u8, a: *const u8, b: *const u8, modulus: &[u32; 8]) {
    debug_assert!(!out.is_null() && !a.is_null() && !b.is_null());
    unsafe {
        let a32 = &*(a as *const [u8; 32]);
        let b32 = &*(b as *const [u8; 32]);
        let x = be32_to_le_words(a32);
        let y = be32_to_le_words(b32);
        let mut r = [0u32; 8];
        // result = (x * y) mod modulus, 256-bit, constrained by the precompile circuit.
        sys_bigint(&mut r as *mut [u32; bigint::WIDTH_WORDS],
                   bigint::OP_MULTIPLY,
                   &x as *const [u32; bigint::WIDTH_WORDS],
                   &y as *const [u32; bigint::WIDTH_WORDS],
                   modulus as *const [u32; bigint::WIDTH_WORDS]);
        let ob = le_words_to_be32(&r);
        core::ptr::copy_nonoverlapping(ob.as_ptr(), out, 32);
    }
}

/// (a * b) mod p — field multiplication. `a`,`b`,`out` are 32-byte big-endian, `a`,`b` < p.
#[no_mangle]
pub extern "C" fn hazync_modmul_p(out: *mut u8, a: *const u8, b: *const u8) {
    modmul(out, a, b, &P);
}

/// (a * b) mod n — scalar multiplication. `a`,`b`,`out` are 32-byte big-endian, `a`,`b` < n.
#[no_mangle]
pub extern "C" fn hazync_modmul_n(out: *mut u8, a: *const u8, b: *const u8) {
    modmul(out, a, b, &N);
}

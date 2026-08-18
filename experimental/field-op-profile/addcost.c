/* What does a field add cost in each representation, in rv32im instructions?
 *
 * hazync#129. The profile showed ~1,602 add-class operations per ECDSA verify against ~1,635 mul+sqr.
 * The rewrite makes muls cheap and adds dearer, so the achievable factor is bounded by how much dearer.
 * This measures that, in the unit that matters: RISC-V instructions, because in the zkVM each retired
 * instruction is trace, and host wall-clock says nothing about proving cost.
 *
 * Compiled for riscv32im at -O2 with the same toolchain the guest uses, then counted from objdump.
 * Nothing here runs; the point is the static instruction count of each body.
 *
 * The two representations:
 *
 *   10x26   what libsecp selects on riscv32im. LAZY: 10 limbs of 26 bits with 6 spare bits, so an add
 *           is ten word adds and no reduction at all. Magnitude grows; normalisation is deferred.
 *
 *   u32x8   what sys_bigint needs. FULLY REDUCED: 8 words, always < p, so an add must propagate carries
 *           and conditionally subtract p to stay in range.
 */
#include <stdint.h>

/* ---- 10x26, lifted verbatim from field_10x26_impl.h (v0.5.1) ---------------------------------- */
typedef struct { uint32_t n[10]; } fe10x26;

void hz_add_10x26(fe10x26 *r, const fe10x26 *a) {
    r->n[0] += a->n[0]; r->n[1] += a->n[1]; r->n[2] += a->n[2]; r->n[3] += a->n[3];
    r->n[4] += a->n[4]; r->n[5] += a->n[5]; r->n[6] += a->n[6]; r->n[7] += a->n[7];
    r->n[8] += a->n[8]; r->n[9] += a->n[9];
}

/* negate_unchecked at magnitude m: r = -a, done by adding a multiple of p that exceeds a. */
void hz_negate_10x26(fe10x26 *r, const fe10x26 *a, int m) {
    r->n[0] = 0x3FFFC2FUL * 2 * (uint32_t)(m + 1) - a->n[0];
    r->n[1] = 0x3FFFFBFUL * 2 * (uint32_t)(m + 1) - a->n[1];
    r->n[2] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[2];
    r->n[3] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[3];
    r->n[4] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[4];
    r->n[5] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[5];
    r->n[6] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[6];
    r->n[7] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[7];
    r->n[8] = 0x3FFFFFFUL * 2 * (uint32_t)(m + 1) - a->n[8];
    r->n[9] = 0x03FFFFFUL * 2 * (uint32_t)(m + 1) - a->n[9];
}

/* ---- u32x8, fully reduced, what sys_bigint consumes ------------------------------------------- */
/* p = 2^256 - 2^32 - 977, little-endian words. */
static const uint32_t P[8] = {
    0xFFFFFC2FU, 0xFFFFFFFEU, 0xFFFFFFFFU, 0xFFFFFFFFU,
    0xFFFFFFFFU, 0xFFFFFFFFU, 0xFFFFFFFFU, 0xFFFFFFFFU
};

/* r = (a + b) mod p. Constant time: always compute the subtraction, select with a mask.
 * This is the honest cost -- a data-dependent branch would leak, and libsecp does not take one. */
void hz_add_u32x8(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]) {
    uint32_t t[8];
    uint64_t c = 0;
    for (int i = 0; i < 8; i++) { c += (uint64_t)a[i] + (uint64_t)b[i]; t[i] = (uint32_t)c; c >>= 32; }
    uint32_t carry = (uint32_t)c;

    /* t - p, borrowing */
    uint32_t s[8];
    uint64_t br = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t d = (uint64_t)t[i] - (uint64_t)P[i] - br;
        s[i] = (uint32_t)d;
        br = (d >> 63) & 1;
    }
    /* take the subtracted value if the add overflowed, or if t >= p (no borrow out) */
    uint32_t mask = (uint32_t)0 - (carry | (uint32_t)(1 - br));
    for (int i = 0; i < 8; i++) r[i] = (t[i] & ~mask) | (s[i] & mask);
}

/* r = (-a) mod p, i.e. p - a, with zero handled (p - 0 == p, must fold to 0). */
void hz_negate_u32x8(uint32_t r[8], const uint32_t a[8]) {
    uint32_t nz = 0;
    for (int i = 0; i < 8; i++) nz |= a[i];
    uint32_t mask = (uint32_t)0 - (uint32_t)(nz != 0);
    uint64_t br = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t d = (uint64_t)P[i] - (uint64_t)a[i] - br;
        r[i] = (uint32_t)d & mask;
        br = (d >> 63) & 1;
    }
}

/* ---- reference: what a 10x26 multiply costs, for scale ---------------------------------------- */
/* Not the real fe_mul (which is ~300 lines of scheduled partial products); this is the schoolbook
 * shape at the same limb count, purely to show the order of magnitude the precompile removes. */
void hz_mul_ref_10x26(uint64_t out[19], const uint32_t a[10], const uint32_t b[10]) {
    for (int i = 0; i < 19; i++) out[i] = 0;
    for (int i = 0; i < 10; i++)
        for (int j = 0; j < 10; j++)
            out[i + j] += (uint64_t)a[i] * (uint64_t)b[j];
}

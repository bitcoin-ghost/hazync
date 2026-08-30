/***********************************************************************
 * hazync: field backend backed by the RISC0 bigint2 coprocessor.      *
 * Distributed under the MIT software license, see the accompanying    *
 * file COPYING or https://www.opensource.org/licenses/mit-license.php.*
 ***********************************************************************/

#ifndef SECP256K1_FIELD_REPR_IMPL_H
#define SECP256K1_FIELD_REPR_IMPL_H

#include "checkmem.h"
#include "util.h"
#include "field.h"
#include "modinv32_impl.h"

/* p = 2^256 - 2^32 - 977, little-endian 32-bit limbs. */
static const uint32_t HZ_P[8] = {
    0xFFFFFC2FUL, 0xFFFFFFFEUL, 0xFFFFFFFFUL, 0xFFFFFFFFUL,
    0xFFFFFFFFUL, 0xFFFFFFFFUL, 0xFFFFFFFFUL, 0xFFFFFFFFUL
};

/* Coprocessor primitives. Limb pointers are 8 little-endian uint32_t, which is byte-identical to the
 * 32-byte little-endian form risc0-crypto uses, so no conversion happens at this boundary either. */
extern void hazync_fq_mul_limbs(const uint32_t *a, const uint32_t *b, uint32_t *out);
extern void hazync_fq_sqr_limbs(const uint32_t *a, uint32_t *out);
extern void hazync_fq_inv_limbs(const uint32_t *a, uint32_t *out);
extern int  hazync_fq_sqrt_limbs(const uint32_t *a, uint32_t *out);

/* ---- canonical-form helpers, all constant-time ---- */

/* r = t - p when (carry || t >= p), else r = t. `carry` is the 2^256 bit of an 8-limb sum. */
static SECP256K1_INLINE void hz_condsub_p(uint32_t *r, const uint32_t *t, uint32_t carry) {
    uint32_t d[8];
    uint64_t b = 0;
    uint32_t mask;
    int i;
    for (i = 0; i < 8; i++) {
        uint64_t x = (uint64_t)t[i] - HZ_P[i] - b;
        d[i] = (uint32_t)x;
        b = (x >> 63) & 1;   /* borrow out */
    }
    /* Take the difference if the subtraction did not borrow (t >= p), or if the sum carried out of
     * 2^256 — in which case t + 2^256 exceeds p regardless. */
    mask = (uint32_t)0 - (uint32_t)((carry | (uint32_t)(1 - (uint32_t)b)) & 1);
    for (i = 0; i < 8; i++) {
        r[i] = (t[i] & ~mask) | (d[i] & mask);
    }
}

/* r = a + b mod p */
static SECP256K1_INLINE void hz_addmod(uint32_t *r, const uint32_t *a, const uint32_t *b) {
    uint32_t t[8];
    uint64_t c = 0;
    int i;
    for (i = 0; i < 8; i++) {
        c += (uint64_t)a[i] + b[i];
        t[i] = (uint32_t)c;
        c >>= 32;
    }
    hz_condsub_p(r, t, (uint32_t)c);
}

/* r = p - a mod p (0 maps to 0) */
static SECP256K1_INLINE void hz_negmod(uint32_t *r, const uint32_t *a) {
    uint32_t t[8], nz = 0, mask;
    uint64_t b = 0;
    int i;
    for (i = 0; i < 8; i++) nz |= a[i];
    for (i = 0; i < 8; i++) {
        uint64_t x = (uint64_t)HZ_P[i] - a[i] - b;
        t[i] = (uint32_t)x;
        b = (x >> 63) & 1;
    }
    /* a == 0 must give 0, not p. */
    mask = (uint32_t)0 - (uint32_t)((nz | (nz >> 1) | (nz >> 2) | (nz >> 4) | (nz >> 8) | (nz >> 16)) & 1);
    { uint32_t any = 0; for (i = 0; i < 8; i++) any |= a[i];
      mask = (uint32_t)0 - (uint32_t)((any != 0) ? 1u : 0u); }
    for (i = 0; i < 8; i++) r[i] = t[i] & mask;
}

static SECP256K1_INLINE int hz_is_zero(const uint32_t *a) {
    uint32_t x = 0;
    int i;
    for (i = 0; i < 8; i++) x |= a[i];
    return x == 0;
}

/* ---- the 27 backend entry points ---- */

static void secp256k1_fe_impl_normalize(secp256k1_fe *r) { (void)r; }        /* already canonical */
static void secp256k1_fe_impl_normalize_weak(secp256k1_fe *r) { (void)r; }
static void secp256k1_fe_impl_normalize_var(secp256k1_fe *r) { (void)r; }
static int secp256k1_fe_impl_normalizes_to_zero(const secp256k1_fe *r) { return hz_is_zero(r->n); }
static int secp256k1_fe_impl_normalizes_to_zero_var(const secp256k1_fe *r) { return hz_is_zero(r->n); }

static void secp256k1_fe_impl_set_int(secp256k1_fe *r, int a) {
    int i;
    r->n[0] = (uint32_t)a;
    for (i = 1; i < 8; i++) r->n[i] = 0;
}

static void secp256k1_fe_impl_clear(secp256k1_fe *a) {
    int i;
    for (i = 0; i < 8; i++) a->n[i] = 0;
}

static int secp256k1_fe_impl_is_zero(const secp256k1_fe *a) { return hz_is_zero(a->n); }
static int secp256k1_fe_impl_is_odd(const secp256k1_fe *a) { return a->n[0] & 1; }

static int secp256k1_fe_impl_cmp_var(const secp256k1_fe *a, const secp256k1_fe *b) {
    int i;
    for (i = 7; i >= 0; i--) {
        if (a->n[i] > b->n[i]) return 1;
        if (a->n[i] < b->n[i]) return -1;
    }
    return 0;
}

/* Big-endian bytes in, canonical limbs out. */
static void secp256k1_fe_impl_set_b32_mod(secp256k1_fe *r, const unsigned char *a) {
    int i;
    for (i = 0; i < 8; i++) {
        r->n[i] = ((uint32_t)a[31 - 4*i]) | ((uint32_t)a[30 - 4*i] << 8)
                | ((uint32_t)a[29 - 4*i] << 16) | ((uint32_t)a[28 - 4*i] << 24);
    }
    /* "mod": a 256-bit input may be >= p, so fold once. p > 2^255 so one conditional subtract is
     * sufficient — the input cannot be >= 2p. */
    hz_condsub_p(r->n, r->n, 0);
}

static int secp256k1_fe_impl_set_b32_limit(secp256k1_fe *r, const unsigned char *a) {
    int i;
    uint32_t t[8];
    uint64_t b = 0;
    for (i = 0; i < 8; i++) {
        t[i] = ((uint32_t)a[31 - 4*i]) | ((uint32_t)a[30 - 4*i] << 8)
             | ((uint32_t)a[29 - 4*i] << 16) | ((uint32_t)a[28 - 4*i] << 24);
    }
    /* Reject rather than fold: this variant must fail when the input is >= p. */
    for (i = 0; i < 8; i++) {
        uint64_t x = (uint64_t)t[i] - HZ_P[i] - b;
        b = (x >> 63) & 1;
    }
    if (!b) return 0;                       /* no borrow => t >= p */
    for (i = 0; i < 8; i++) r->n[i] = t[i];
    return 1;
}

static void secp256k1_fe_impl_get_b32(unsigned char *r, const secp256k1_fe *a) {
    int i;
    for (i = 0; i < 8; i++) {
        r[31 - 4*i] = (unsigned char)(a->n[i]);
        r[30 - 4*i] = (unsigned char)(a->n[i] >> 8);
        r[29 - 4*i] = (unsigned char)(a->n[i] >> 16);
        r[28 - 4*i] = (unsigned char)(a->n[i] >> 24);
    }
}

static void secp256k1_fe_impl_add(secp256k1_fe *r, const secp256k1_fe *a) {
    hz_addmod(r->n, r->n, a->n);
}

static void secp256k1_fe_impl_add_int(secp256k1_fe *r, int a) {
    uint32_t t[8];
    int i;
    t[0] = (uint32_t)a;
    for (i = 1; i < 8; i++) t[i] = 0;
    hz_addmod(r->n, r->n, t);
}

/* `m` is the caller's magnitude bound; irrelevant here because every element is already canonical.
 * The contract is only that the result is congruent to -a, which p - a satisfies exactly. */
static void secp256k1_fe_impl_negate_unchecked(secp256k1_fe *r, const secp256k1_fe *a, int m) {
    (void)m;
    hz_negmod(r->n, a->n);
}

/* r *= a for small non-negative a, by double-and-add on the canonical form. */
static void secp256k1_fe_impl_mul_int_unchecked(secp256k1_fe *r, int a) {
    uint32_t acc[8], base[8];
    unsigned int k = (unsigned int)a;
    int i;
    for (i = 0; i < 8; i++) { acc[i] = 0; base[i] = r->n[i]; }
    while (k) {
        if (k & 1) hz_addmod(acc, acc, base);
        hz_addmod(base, base, base);
        k >>= 1;
    }
    for (i = 0; i < 8; i++) r->n[i] = acc[i];
}

/* x/2 mod p: exact shift when even, (x+p)/2 when odd. Constant-time. */
static void secp256k1_fe_impl_half(secp256k1_fe *r) {
    uint32_t t[8], mask;
    uint64_t c = 0;
    int i;
    mask = (uint32_t)0 - (uint32_t)(r->n[0] & 1);
    for (i = 0; i < 8; i++) {
        c += (uint64_t)r->n[i] + (HZ_P[i] & mask);
        t[i] = (uint32_t)c;
        c >>= 32;
    }
    /* x + p < 2^257, so `c` is the 2^256 bit and shifts into the top limb. */
    for (i = 0; i < 7; i++) t[i] = (t[i] >> 1) | (t[i + 1] << 31);
    t[7] = (t[7] >> 1) | ((uint32_t)c << 31);
    for (i = 0; i < 8; i++) r->n[i] = t[i];
}

static void secp256k1_fe_impl_cmov(secp256k1_fe *r, const secp256k1_fe *a, int flag) {
    uint32_t mask = (uint32_t)0 - (uint32_t)(flag != 0);
    int i;
    for (i = 0; i < 8; i++) r->n[i] = (r->n[i] & ~mask) | (a->n[i] & mask);
}

static void secp256k1_fe_impl_to_storage(secp256k1_fe_storage *r, const secp256k1_fe *a) {
    int i;
    for (i = 0; i < 8; i++) r->n[i] = a->n[i];
}

static void secp256k1_fe_impl_from_storage(secp256k1_fe *r, const secp256k1_fe_storage *a) {
    int i;
    for (i = 0; i < 8; i++) r->n[i] = a->n[i];
}

/* Magnitude is meaningless here; the bound for any m is simply p-1, the largest canonical value. */
static void secp256k1_fe_impl_get_bounds(secp256k1_fe *r, int m) {
    int i;
    (void)m;
    for (i = 0; i < 8; i++) r->n[i] = HZ_P[i];
    r->n[0] -= 1;
}

/* ---- coprocessor-backed ---- */

static void secp256k1_fe_impl_mul(secp256k1_fe *r, const secp256k1_fe *a, const secp256k1_fe * SECP256K1_RESTRICT b) {
    hazync_fq_mul_limbs(a->n, b->n, r->n);
}

static void secp256k1_fe_impl_sqr(secp256k1_fe *r, const secp256k1_fe *a) {
    hazync_fq_sqr_limbs(a->n, r->n);
}

static void secp256k1_fe_impl_inv(secp256k1_fe *r, const secp256k1_fe *x) {
    hazync_fq_inv_limbs(x->n, r->n);
}

static void secp256k1_fe_impl_inv_var(secp256k1_fe *r, const secp256k1_fe *x) {
    hazync_fq_inv_limbs(x->n, r->n);
}

static int secp256k1_fe_impl_is_square_var(const secp256k1_fe *x) {
    uint32_t tmp[8];
    return hazync_fq_sqrt_limbs(x->n, tmp);
}

#ifdef VERIFY
static void secp256k1_fe_impl_verify(const secp256k1_fe *a) {
    /* The single invariant of this backend: every element is canonical. */
    uint64_t b = 0;
    int i;
    for (i = 0; i < 8; i++) {
        uint64_t x = (uint64_t)a->n[i] - HZ_P[i] - b;
        b = (x >> 63) & 1;
    }
    VERIFY_CHECK(b == 1);   /* borrowed => a < p */
}
#endif

#endif /* SECP256K1_FIELD_REPR_IMPL_H */

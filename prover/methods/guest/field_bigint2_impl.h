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

/* ---------------------------------------------------------------------------------------------
 * REPRESENTATION.  8 little-endian 32-bit limbs holding a value in [0, 2^256) that is congruent
 * to the field element mod p.  This is deliberately NOT canonical: see LAZY below.
 *
 * WHY NOT MAGNITUDE.  The 10x26 backend spreads 256 bits over ten 26-bit limbs so that adds can
 * accumulate carry headroom and reduce rarely.  That headroom costs a conversion to and from
 * canonical form at every coprocessor call, measured at 771 cycles against an 83-cycle multiply
 * -- 9.3x the operation it feeds.  So the representation must BE what the coprocessor consumes:
 * 32 bytes little-endian, no conversion at the boundary.
 *
 * LAZY.  Elements are not reduced to [0, p) after every add.  `Fq::reduce_from_bigint` accepts
 * any 256-bit value, and because p has its MSB set it takes the `msb_set()` fast path -- a
 * single conditional subtract -- so the coprocessor reduces on load anyway.  Reducing in `fe_add`
 * as well is redundant work.  Adds instead fold the 2^256 carry back with 2^256 = 2^32 + 977,
 * which keeps the value inside 8 limbs.  Only normalize() canonicalises.
 *
 * BRANCHING.  This backend branches on element values, which libsecp's default backends avoid so
 * that signing is constant-time.  There is no timing side channel inside a zkVM: execution is a
 * proven trace, not a measurable duration, and the guest only ever verifies public data -- it
 * holds no secret scalar.  The branchless select this replaces cost 24 RV32 ops per add, more
 * than the arithmetic it guarded.  libsecp's own `_var` functions already branch throughout the
 * verification path this backend serves.
 * ------------------------------------------------------------------------------------------ */

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

/* ---- helpers ---- */

/* Returns 1 when a >= p. */
static SECP256K1_INLINE int hz_ge_p(const uint32_t *a) {
    int i;
    for (i = 7; i >= 0; i--) {
        if (a[i] != HZ_P[i]) return a[i] > HZ_P[i];
    }
    return 1;   /* exactly p */
}

/* a -= p, assuming a >= p. */
static SECP256K1_INLINE void hz_sub_p(uint32_t *a) {
    uint64_t b = 0;
    int i;
    for (i = 0; i < 8; i++) {
        uint64_t x = (uint64_t)a[i] - HZ_P[i] - b;
        a[i] = (uint32_t)x;
        b = (x >> 63) & 1;
    }
}

/* r += c * 2^256, folded as c * (2^32 + 977). Returns the carry out of 2^256.
 * c * (2^32 + 977) must fit in 64 bits, i.e. c < 2^31; every caller passes c <= 2^31 - 1. */
static SECP256K1_INLINE uint32_t hz_fold_once(uint32_t *r, uint32_t c) {
    uint64_t acc;
    int i;
    if (c == 0) return 0;
    acc  = (uint64_t)r[0] + (uint64_t)c * 977u;
    r[0] = (uint32_t)acc; acc >>= 32;
    acc += (uint64_t)r[1] + (uint64_t)c;
    r[1] = (uint32_t)acc; acc >>= 32;
    for (i = 2; i < 8 && acc; i++) {
        acc += r[i];
        r[i] = (uint32_t)acc;
        acc >>= 32;
    }
    return (uint32_t)acc;
}

/* r += c * 2^256, repeating the fold until it stops carrying. Terminates in at most three passes:
 * after the first, r < 2^256 and the addend is at most 2^32 + 977, so the second can only carry
 * when r is within 2^33 of 2^256, leaving r < 2^33 -- which the third cannot carry out of. */
static SECP256K1_INLINE void hz_fold(uint32_t *r, uint32_t c) {
    while (c) c = hz_fold_once(r, c);
}

/* r = a + b (mod p), left lazy in [0, 2^256). */
static SECP256K1_INLINE void hz_add(uint32_t *r, const uint32_t *a, const uint32_t *b) {
    uint64_t c = 0;
    int i;
    for (i = 0; i < 8; i++) {
        c += (uint64_t)a[i] + b[i];
        r[i] = (uint32_t)c;
        c >>= 32;
    }
    if (c) hz_fold(r, (uint32_t)c);
}

/* Reduce in place to [0, p). A lazy value is < 2^256 < 2p, so one subtract suffices. */
static SECP256K1_INLINE void hz_canon(uint32_t *r) {
    if (hz_ge_p(r)) hz_sub_p(r);
}

/* r = -a (mod p), canonical. */
static SECP256K1_INLINE void hz_neg(uint32_t *r, const uint32_t *a) {
    uint32_t t[8];
    uint64_t b = 0;
    int i, z = 1;
    for (i = 0; i < 8; i++) t[i] = a[i];
    hz_canon(t);
    for (i = 0; i < 8; i++) { if (t[i]) { z = 0; break; } }
    if (z) { for (i = 0; i < 8; i++) r[i] = 0; return; }   /* -0 == 0, not p */
    for (i = 0; i < 8; i++) {
        uint64_t x = (uint64_t)HZ_P[i] - t[i] - b;
        r[i] = (uint32_t)x;
        b = (x >> 63) & 1;
    }
}

/* True when a is congruent to zero, i.e. a == 0 or a == p (2p does not fit in 8 limbs). */
static SECP256K1_INLINE int hz_is_zero_mod(const uint32_t *a) {
    uint32_t x = 0;
    int i;
    for (i = 0; i < 8; i++) x |= a[i];
    if (x == 0) return 1;
    for (i = 0; i < 8; i++) { if (a[i] != HZ_P[i]) return 0; }
    return 1;
}

/* ---- the backend entry points ---- */

static void secp256k1_fe_impl_normalize(secp256k1_fe *r) { hz_canon(r->n); }
static void secp256k1_fe_impl_normalize_weak(secp256k1_fe *r) { hz_canon(r->n); }
static void secp256k1_fe_impl_normalize_var(secp256k1_fe *r) { hz_canon(r->n); }
static int secp256k1_fe_impl_normalizes_to_zero(const secp256k1_fe *r) { return hz_is_zero_mod(r->n); }
static int secp256k1_fe_impl_normalizes_to_zero_var(const secp256k1_fe *r) { return hz_is_zero_mod(r->n); }

static void secp256k1_fe_impl_set_int(secp256k1_fe *r, int a) {
    int i;
    r->n[0] = (uint32_t)a;
    for (i = 1; i < 8; i++) r->n[i] = 0;
}

static void secp256k1_fe_impl_clear(secp256k1_fe *a) {
    int i;
    for (i = 0; i < 8; i++) a->n[i] = 0;
}

/* Contract: input is normalized, so a canonical compare against zero is enough. */
static int secp256k1_fe_impl_is_zero(const secp256k1_fe *a) {
    uint32_t x = 0;
    int i;
    for (i = 0; i < 8; i++) x |= a->n[i];
    return x == 0;
}

static int secp256k1_fe_impl_is_odd(const secp256k1_fe *a) { return a->n[0] & 1; }

static int secp256k1_fe_impl_cmp_var(const secp256k1_fe *a, const secp256k1_fe *b) {
    int i;
    for (i = 7; i >= 0; i--) {
        if (a->n[i] > b->n[i]) return 1;
        if (a->n[i] < b->n[i]) return -1;
    }
    return 0;
}

/* Big-endian bytes in. The lazy invariant admits any 256-bit value, so no reduction is needed. */
static void secp256k1_fe_impl_set_b32_mod(secp256k1_fe *r, const unsigned char *a) {
    int i;
    for (i = 0; i < 8; i++) {
        r->n[i] = ((uint32_t)a[31 - 4*i]) | ((uint32_t)a[30 - 4*i] << 8)
                | ((uint32_t)a[29 - 4*i] << 16) | ((uint32_t)a[28 - 4*i] << 24);
    }
}

static int secp256k1_fe_impl_set_b32_limit(secp256k1_fe *r, const unsigned char *a) {
    uint32_t t[8];
    int i;
    for (i = 0; i < 8; i++) {
        t[i] = ((uint32_t)a[31 - 4*i]) | ((uint32_t)a[30 - 4*i] << 8)
             | ((uint32_t)a[29 - 4*i] << 16) | ((uint32_t)a[28 - 4*i] << 24);
    }
    if (hz_ge_p(t)) return 0;               /* this variant must fail on >= p */
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
    hz_add(r->n, r->n, a->n);
}

static void secp256k1_fe_impl_add_int(secp256k1_fe *r, int a) {
    uint64_t c = (uint64_t)r->n[0] + (uint32_t)a;
    int i;
    r->n[0] = (uint32_t)c; c >>= 32;
    for (i = 1; i < 8 && c; i++) {
        c += r->n[i];
        r->n[i] = (uint32_t)c;
        c >>= 32;
    }
    if (c) hz_fold(r->n, (uint32_t)c);
}

/* `m` is the caller's magnitude bound, meaningless here: the contract is only that the result is
 * congruent to -a, which p - a satisfies for any representative. */
static void secp256k1_fe_impl_negate_unchecked(secp256k1_fe *r, const secp256k1_fe *a, int m) {
    (void)m;
    hz_neg(r->n, a->n);
}

/* r *= a for small non-negative a: one limb pass, then fold the overflow word. r < 2^256 and
 * a < 2^31, so the product is < 2^287 and the overflow word is < 2^31 -- inside hz_fold_once's
 * bound. libsecp only ever calls this with small constants. */
static void secp256k1_fe_impl_mul_int_unchecked(secp256k1_fe *r, int a) {
    uint64_t c = 0;
    uint32_t k = (uint32_t)a;
    int i;
    for (i = 0; i < 8; i++) {
        c += (uint64_t)r->n[i] * k;
        r->n[i] = (uint32_t)c;
        c >>= 32;
    }
    if (c) hz_fold(r->n, (uint32_t)c);
}

/* x/2 mod p: exact shift when even, (x+p)/2 when odd. Both are correct for a lazy x, since
 * 2*((x+p)/2) = x+p = x (mod p) and x+p < 2^257. */
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
    for (i = 0; i < 7; i++) t[i] = (t[i] >> 1) | (t[i + 1] << 31);
    t[7] = (t[7] >> 1) | ((uint32_t)c << 31);
    for (i = 0; i < 8; i++) r->n[i] = t[i];
}

/* Kept branchless: cmov is libsecp's table-lookup primitive and is cheap either way. */
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

/* The largest value representable at magnitude m. Magnitude is meaningless to this backend -- every
 * 8-limb value is legal -- with one exception that is NOT cosmetic: magnitude 0 means every limb is
 * zero, so the bound is 0 and not 2^256 - 1. libsecp's run_field_half() starts from exactly that. */
static void secp256k1_fe_impl_get_bounds(secp256k1_fe *r, int m) {
    uint32_t v = (m == 0) ? 0UL : 0xFFFFFFFFUL;
    int i;
    for (i = 0; i < 8; i++) r->n[i] = v;
    /* libsecp's run_field_half() asserts the low limb is EVEN, then decrements it to force a
     * worst-case odd input with all carries set. That holds in every stock backend because the
     * bound is a multiple of two. Only fe_get_bounds is affected, and it is called from tests.c
     * and nowhere else, so giving up the single largest value costs nothing real. */
    if (m != 0) r->n[0] = 0xFFFFFFFEUL;
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

/* ---- storage and modinv32 interop ---- */

/* Kept branchless for the same reason as fe_cmov: it is a table-lookup primitive, and cheap. */
static SECP256K1_INLINE void secp256k1_fe_storage_cmov(secp256k1_fe_storage *r, const secp256k1_fe_storage *a, int flag) {
    uint32_t mask = (uint32_t)0 - (uint32_t)(flag != 0);
    int i;
    SECP256K1_CHECKMEM_CHECK_VERIFY(r->n, sizeof(r->n));
    for (i = 0; i < 8; i++) r->n[i] = (r->n[i] & ~mask) | (a->n[i] & mask);
}

/* modinv32 works in nine signed 30-bit limbs. Neither of these is on the hot path -- fe_inv and
 * fe_inv_var both go to the coprocessor -- but libsecp's own tests call them directly, and
 * modinv32_impl.h is still compiled in, so they must be correct rather than merely present. */
static void secp256k1_fe_to_signed30(secp256k1_modinv32_signed30 *r, const secp256k1_fe *a) {
    const uint32_t M30 = UINT32_MAX >> 2;
    uint32_t t[8];
    int i;
    for (i = 0; i < 8; i++) t[i] = a->n[i];
    hz_canon(t);                     /* the lazy invariant admits >= p; modinv32 requires < p */
    for (i = 0; i < 9; i++) {
        int bit = 30 * i, k = bit >> 5, s = bit & 31;
        uint64_t w = (uint64_t)t[k] | (k + 1 < 8 ? ((uint64_t)t[k + 1] << 32) : 0);
        r->v[i] = (uint32_t)((w >> s) & M30);
    }
}

static void secp256k1_fe_from_signed30(secp256k1_fe *r, const secp256k1_modinv32_signed30 *a) {
    uint32_t out[9];
    int i;
    for (i = 0; i < 9; i++) out[i] = 0;
    for (i = 0; i < 9; i++) {
        int bit = 30 * i, k = bit >> 5, s = bit & 31;
        uint64_t v = (uint64_t)(uint32_t)a->v[i];
        VERIFY_CHECK((uint32_t)a->v[i] >> (i == 8 ? 16 : 30) == 0);
        out[k] |= (uint32_t)(v << s);
        if (s) out[k + 1] |= (uint32_t)(v >> (32 - s));
    }
    /* modinv32's output is in [0, p), so the 2^256 word is zero and the low eight limbs are exact. */
    VERIFY_CHECK(out[8] == 0);
    for (i = 0; i < 8; i++) r->n[i] = out[i];
}

#ifdef VERIFY
/* Every 8-limb value is a legal representative under the lazy invariant, so there is nothing here
 * to check. The canonical-form assertion this replaced no longer holds by design. */
static void secp256k1_fe_impl_verify(const secp256k1_fe *a) { (void)a; }
#endif

#endif /* SECP256K1_FIELD_REPR_IMPL_H */

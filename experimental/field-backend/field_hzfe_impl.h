/* libsecp256k1 field backend implementation on fully-reduced [u32; 8]. hazync#129.
 *
 * The 28 entry points field.h expects, mapped onto the hzfe core in hzfe.c. That core is separately
 * gated by 1.4M differential comparisons against the stock 10x26 backend, so this file is deliberately
 * thin: it adapts calling conventions and does no arithmetic of its own.
 *
 * The magnitude system does not exist here. Values are reduced after every operation, so:
 *
 *   normalize / normalize_weak / normalize_var   no-ops
 *   normalizes_to_zero{,_var}                    is_zero
 *   the `m` argument to negate_unchecked         accepted, ignored
 *   verify                                       asserts what is structurally true: value < p
 *
 * Callers that relied on lazy reduction are not broken by this, they are merely doing redundant work:
 * asking for a normalisation that already happened is free rather than wrong.
 */
#ifndef SECP256K1_FIELD_REPR_IMPL_H
#define SECP256K1_FIELD_REPR_IMPL_H

#include <string.h>
#include "util.h"
#include "field.h"
#include "hzfe.h"

#ifdef VERIFY
static void secp256k1_fe_impl_verify(const secp256k1_fe *a) {
    /* Reduced by construction. Check it, because "by construction" is a claim about code that could be
     * wrong, and this is the one place the claim is cheap to test. */
    VERIFY_CHECK(hzfe_cmp(a->n, HZFE_P) < 0);
}

static void secp256k1_fe_impl_get_bounds(secp256k1_fe *r, int m) {
    /* The largest value representable at magnitude m. There is only one magnitude here, so the answer
     * is p-1 for any m >= 1, and 0 for m == 0. */
    (void)m;
    if (m == 0) {
        hzfe_set_int(r->n, 0);
    } else {
        memcpy(r->n, HZFE_P, sizeof(r->n));
        r->n[0] -= 1;            /* p is odd, so p-1 needs no borrow */
    }
}
#endif

/* --- normalisation: all no-ops, because nothing is ever denormalised ---------------------------- */

static void secp256k1_fe_impl_normalize(secp256k1_fe *r) { (void)r; }
static void secp256k1_fe_impl_normalize_weak(secp256k1_fe *r) { (void)r; }
static void secp256k1_fe_impl_normalize_var(secp256k1_fe *r) { (void)r; }

static int secp256k1_fe_impl_normalizes_to_zero(const secp256k1_fe *r) { return hzfe_is_zero(r->n); }
static int secp256k1_fe_impl_normalizes_to_zero_var(const secp256k1_fe *r) { return hzfe_is_zero(r->n); }

/* --- construction and inspection ---------------------------------------------------------------- */

static void secp256k1_fe_impl_set_int(secp256k1_fe *r, int a) { hzfe_set_int(r->n, (uint32_t)a); }
static void secp256k1_fe_impl_clear(secp256k1_fe *a) { memset(a->n, 0, sizeof(a->n)); }
static int  secp256k1_fe_impl_is_zero(const secp256k1_fe *a) { return hzfe_is_zero(a->n); }
static int  secp256k1_fe_impl_is_odd(const secp256k1_fe *a) { return hzfe_is_odd(a->n); }

static int secp256k1_fe_impl_cmp_var(const secp256k1_fe *a, const secp256k1_fe *b) {
    return hzfe_cmp(a->n, b->n);
}

static void secp256k1_fe_impl_get_b32(unsigned char *r, const secp256k1_fe *a) {
    hzfe_get_b32(r, a->n);
}

static void secp256k1_fe_impl_set_b32_mod(secp256k1_fe *r, const unsigned char *a) {
    hzfe_set_b32_mod(r->n, a);
}

static int secp256k1_fe_impl_set_b32_limit(secp256k1_fe *r, const unsigned char *a) {
    return hzfe_set_b32_limit(r->n, a);
}

/* --- arithmetic ---------------------------------------------------------------------------------- */

/* The magnitude argument is part of the interface and meaningless here. Accepted and ignored, rather
 * than removed, so call sites in the EC layer need no change. */
static void secp256k1_fe_impl_negate_unchecked(secp256k1_fe *r, const secp256k1_fe *a, int m) {
    (void)m;
    hzfe_neg(r->n, a->n);
}

static void secp256k1_fe_impl_mul_int_unchecked(secp256k1_fe *r, int a) {
    hzfe_mul_int(r->n, r->n, a);
}

static void secp256k1_fe_impl_add_int(secp256k1_fe *r, int a) {
    hzfe_add_int(r->n, r->n, a);
}

static void secp256k1_fe_impl_add(secp256k1_fe *r, const secp256k1_fe *a) {
    hzfe_add(r->n, r->n, a->n);
}

static void secp256k1_fe_impl_mul(secp256k1_fe *r, const secp256k1_fe *a,
                                  const secp256k1_fe * SECP256K1_RESTRICT b) {
    hzfe_mul(r->n, a->n, b->n);
}

static void secp256k1_fe_impl_sqr(secp256k1_fe *r, const secp256k1_fe *a) {
    hzfe_sqr(r->n, a->n);
}

static void secp256k1_fe_impl_half(secp256k1_fe *r) {
    hzfe_half(r->n, r->n);
}

static void secp256k1_fe_impl_cmov(secp256k1_fe *r, const secp256k1_fe *a, int flag) {
    hzfe_cmov(r->n, a->n, flag);
}

/* --- storage: bit-identical, so a straight copy -------------------------------------------------- */

static void secp256k1_fe_impl_to_storage(secp256k1_fe_storage *r, const secp256k1_fe *a) {
    memcpy(r->n, a->n, sizeof(r->n));
}

static void secp256k1_fe_impl_from_storage(secp256k1_fe *r, const secp256k1_fe_storage *a) {
    memcpy(r->n, a->n, sizeof(r->n));
}

/* Note the name: this one is secp256k1_fe_storage_cmov, NOT _impl_. field.h declares it directly for
 * the backend to supply rather than routing it through the impl indirection, so a search for
 * `secp256k1_fe_impl_*` misses it -- which is how it came to be the one omission out of 28. */
static void secp256k1_fe_storage_cmov(secp256k1_fe_storage *r, const secp256k1_fe_storage *a, int flag) {
    hzfe_cmov(r->n, a->n, flag);
}

/* --- inverse and residue test: libsecp's own safegcd, via signed30 ------------------------------- */

static void secp256k1_fe_impl_inv(secp256k1_fe *r, const secp256k1_fe *x) { hzfe_inv(r->n, x->n); }
static void secp256k1_fe_impl_inv_var(secp256k1_fe *r, const secp256k1_fe *x) { hzfe_inv_var(r->n, x->n); }
static int  secp256k1_fe_impl_is_square_var(const secp256k1_fe *x) { return hzfe_is_square_var(x->n); }

#endif /* SECP256K1_FIELD_REPR_IMPL_H */

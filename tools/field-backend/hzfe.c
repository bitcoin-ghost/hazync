/* hzfe: secp256k1 field arithmetic on fully-reduced [u32; 8]. hazync#129. See hzfe.h.
 *
 * Everything here is constant time with respect to the VALUES it operates on: no data-dependent
 * branches, no data-dependent memory access. libsecp is careful about this and a replacement backend
 * that is not would be a regression in a property that matters more than speed.
 */
#include "hzfe.h"
#include <string.h>

const uint32_t HZFE_P[8] = {
    0xFFFFFC2FU, 0xFFFFFFFEU, 0xFFFFFFFFU, 0xFFFFFFFFU,
    0xFFFFFFFFU, 0xFFFFFFFFU, 0xFFFFFFFFU, 0xFFFFFFFFU
};

/* --- helpers ------------------------------------------------------------------------------------ */

/* r = a - b, returning the borrow out. Constant time. */
static uint32_t sub_borrow(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]) {
    uint64_t br = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t d = (uint64_t)a[i] - (uint64_t)b[i] - br;
        r[i] = (uint32_t)d;
        br = (d >> 63) & 1;      /* the subtraction wrapped, so a borrow propagates */
    }
    return (uint32_t)br;
}

/* r = a if flag==0 else b. Constant time select. */
static void select_ct(uint32_t r[8], const uint32_t a[8], const uint32_t b[8], uint32_t flag) {
    uint32_t m = (uint32_t)0 - (flag & 1);
    for (int i = 0; i < 8; i++) r[i] = (a[i] & ~m) | (b[i] & m);
}

/* Reduce t (which must be < 2p) into [0, p). */
static void reduce_once(uint32_t r[8], const uint32_t t[8], uint32_t carry) {
    uint32_t s[8];
    uint32_t br = sub_borrow(s, t, HZFE_P);
    /* Take t-p when the addition carried out (value >= 2^256 > p) or when t >= p (no borrow). */
    select_ct(r, t, s, carry | (1u - br));
}

/* --- add / sub / neg ---------------------------------------------------------------------------- */

void hzfe_add(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]) {
    uint32_t t[8];
    uint64_t c = 0;
    for (int i = 0; i < 8; i++) { c += (uint64_t)a[i] + (uint64_t)b[i]; t[i] = (uint32_t)c; c >>= 32; }
    reduce_once(r, t, (uint32_t)c);
}

void hzfe_sub(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]) {
    uint32_t t[8], u[8];
    uint32_t br = sub_borrow(t, a, b);
    /* If it went negative, add p back. */
    uint64_t c = 0;
    for (int i = 0; i < 8; i++) { c += (uint64_t)t[i] + (uint64_t)HZFE_P[i]; u[i] = (uint32_t)c; c >>= 32; }
    select_ct(r, t, u, br);
}

void hzfe_neg(uint32_t r[8], const uint32_t a[8]) {
    uint32_t z[8] = {0,0,0,0,0,0,0,0};
    hzfe_sub(r, z, a);           /* 0 - a mod p, which folds 0 -> 0 correctly */
}

/* --- multiply ----------------------------------------------------------------------------------- */

void hzfe_mul(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]) { hzfe_modmul(r, a, b); }
void hzfe_sqr(uint32_t r[8], const uint32_t a[8]) { hzfe_modmul(r, a, a); }

/* --- half -------------------------------------------------------------------------------------- */

/* r = a/2 mod p. If a is even, a>>1. If odd, (a+p)>>1 -- p is odd so a+p is even. */
void hzfe_half(uint32_t r[8], const uint32_t a[8]) {
    uint32_t t[8];
    uint32_t odd = a[0] & 1;
    uint32_t m = (uint32_t)0 - odd;
    uint64_t c = 0;
    for (int i = 0; i < 8; i++) { c += (uint64_t)a[i] + (uint64_t)(HZFE_P[i] & m); t[i] = (uint32_t)c; c >>= 32; }
    uint32_t top = (uint32_t)c;                       /* a+p can exceed 2^256; keep the carry bit */
    for (int i = 0; i < 7; i++) r[i] = (t[i] >> 1) | (t[i + 1] << 31);
    r[7] = (t[7] >> 1) | (top << 31);
}

/* --- serialisation ------------------------------------------------------------------------------ */

void hzfe_get_b32(unsigned char out[32], const uint32_t a[8]) {
    for (int i = 0; i < 8; i++) {
        uint32_t w = a[7 - i];                         /* big-endian output, most significant first */
        out[i * 4 + 0] = (unsigned char)(w >> 24);
        out[i * 4 + 1] = (unsigned char)(w >> 16);
        out[i * 4 + 2] = (unsigned char)(w >> 8);
        out[i * 4 + 3] = (unsigned char)(w);
    }
}

static void from_b32_raw(uint32_t r[8], const unsigned char in[32]) {
    for (int i = 0; i < 8; i++) {
        const unsigned char *p = in + (7 - i) * 4;
        r[i] = ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
    }
}

void hzfe_set_b32_mod(uint32_t r[8], const unsigned char in[32]) {
    uint32_t t[8];
    from_b32_raw(t, in);
    reduce_once(r, t, 0);        /* a 256-bit value is < 2p, so one conditional subtract suffices */
}

int hzfe_set_b32_limit(uint32_t r[8], const unsigned char in[32]) {
    uint32_t t[8], s[8];
    from_b32_raw(t, in);
    uint32_t br = sub_borrow(s, t, HZFE_P);
    memcpy(r, t, sizeof(t));
    return (int)br;              /* borrow out means t < p, i.e. in range */
}

/* --- predicates --------------------------------------------------------------------------------- */

int hzfe_is_zero(const uint32_t a[8]) {
    uint32_t acc = 0;
    for (int i = 0; i < 8; i++) acc |= a[i];
    return acc == 0;
}

int hzfe_is_odd(const uint32_t a[8]) { return (int)(a[0] & 1); }

int hzfe_equal(const uint32_t a[8], const uint32_t b[8]) {
    uint32_t acc = 0;
    for (int i = 0; i < 8; i++) acc |= (a[i] ^ b[i]);
    return acc == 0;
}

void hzfe_cmov(uint32_t r[8], const uint32_t a[8], int flag) {
    uint32_t m = (uint32_t)0 - (uint32_t)(flag & 1);
    for (int i = 0; i < 8; i++) r[i] = (r[i] & ~m) | (a[i] & m);
}

void hzfe_set_int(uint32_t r[8], uint32_t v) {
    r[0] = v;
    for (int i = 1; i < 8; i++) r[i] = 0;
}

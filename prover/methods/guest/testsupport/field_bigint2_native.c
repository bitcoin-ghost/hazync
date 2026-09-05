/* Host-only reference for the four coprocessor primitives the bigint2 field backend calls.
 *
 * This exists so libsecp256k1's OWN test suite can be run against the backend on a workstation,
 * with no zkVM and no GPU. It is never compiled into the guest: there the same four symbols are
 * provided by field_bigint2.rs, which calls risc0-crypto's Fq. Correctness of THIS file is not
 * the thing under test -- it is deliberately the dumbest possible schoolbook implementation, so
 * that when it and the backend disagree, the backend is what is wrong.
 */
#include <stdint.h>
#include <string.h>

static const uint32_t NP[8] = {
    0xFFFFFC2FUL, 0xFFFFFFFEUL, 0xFFFFFFFFUL, 0xFFFFFFFFUL,
    0xFFFFFFFFUL, 0xFFFFFFFFUL, 0xFFFFFFFFUL, 0xFFFFFFFFUL
};

static int np_ge(const uint32_t *a) {
    int i;
    for (i = 7; i >= 0; i--) { if (a[i] != NP[i]) return a[i] > NP[i]; }
    return 1;
}
static void np_sub(uint32_t *a) {
    uint64_t b = 0; int i;
    for (i = 0; i < 8; i++) { uint64_t x = (uint64_t)a[i] - NP[i] - b; a[i] = (uint32_t)x; b = (x >> 63) & 1; }
}
static void np_canon(uint32_t *a) { while (np_ge(a)) np_sub(a); }

/* r[0..7] = w[0..15] mod p, folding 2^256 = 2^32 + 977. */
static void np_reduce(uint32_t *r, const uint32_t *w) {
    uint32_t acc[16]; int i, pass;
    memcpy(acc, w, 16 * sizeof(uint32_t));
    for (pass = 0; pass < 4; pass++) {
        uint32_t hi[8]; uint64_t c = 0; int any = 0;
        for (i = 0; i < 8; i++) { hi[i] = acc[8 + i]; if (hi[i]) any = 1; acc[8 + i] = 0; }
        if (!any) break;
        /* acc += hi * (2^32 + 977) */
        for (i = 0; i < 8; i++) {                      /* hi * 977 at limb 0 */
            c += (uint64_t)acc[i] + (uint64_t)hi[i] * 977u;
            acc[i] = (uint32_t)c; c >>= 32;
        }
        for (i = 8; i < 16 && c; i++) { c += acc[i]; acc[i] = (uint32_t)c; c >>= 32; }
        c = 0;
        for (i = 0; i < 8; i++) {                      /* hi * 2^32 at limb 1 */
            c += (uint64_t)acc[i + 1] + hi[i];
            acc[i + 1] = (uint32_t)c; c >>= 32;
        }
        for (i = 9; i < 16 && c; i++) { c += acc[i]; acc[i] = (uint32_t)c; c >>= 32; }
    }
    for (i = 0; i < 8; i++) r[i] = acc[i];
    np_canon(r);
}

static void np_mul(uint32_t *r, const uint32_t *a, const uint32_t *b) {
    uint32_t w[16]; uint64_t c; int i, j;
    memset(w, 0, sizeof(w));
    for (i = 0; i < 8; i++) {
        c = 0;
        for (j = 0; j < 8; j++) {
            uint64_t t = (uint64_t)a[i] * b[j] + w[i + j] + c;
            w[i + j] = (uint32_t)t; c = t >> 32;
        }
        for (j = i + 8; j < 16 && c; j++) { c += w[j]; w[j] = (uint32_t)c; c >>= 32; }
    }
    np_reduce(r, w);
}

/* r = a^e mod p, e given as 8 little-endian limbs. Square-and-multiply, MSB first. */
static void np_pow(uint32_t *r, const uint32_t *a, const uint32_t *e) {
    uint32_t acc[8], base[8]; int bit, started = 0;
    memcpy(base, a, 32);
    memset(acc, 0, 32); acc[0] = 1;
    for (bit = 255; bit >= 0; bit--) {
        if (started) np_mul(acc, acc, acc);
        if ((e[bit >> 5] >> (bit & 31)) & 1) {
            if (started) np_mul(acc, acc, base); else { memcpy(acc, base, 32); started = 1; }
        }
    }
    memcpy(r, acc, 32);
}

void hazync_fq_mul_limbs(const uint32_t *a, const uint32_t *b, uint32_t *out) {
    uint32_t x[8], y[8];
    memcpy(x, a, 32); memcpy(y, b, 32); np_canon(x); np_canon(y);
    np_mul(out, x, y);
}

void hazync_fq_sqr_limbs(const uint32_t *a, uint32_t *out) {
    uint32_t x[8];
    memcpy(x, a, 32); np_canon(x);
    np_mul(out, x, x);
}

/* a^(p-2) mod p. Zero maps to zero, matching libsecp's inverse contract. */
void hazync_fq_inv_limbs(const uint32_t *a, uint32_t *out) {
    uint32_t x[8], e[8]; int i;
    memcpy(x, a, 32); np_canon(x);
    for (i = 0; i < 8; i++) e[i] = NP[i];
    e[0] -= 2;
    np_pow(out, x, e);
}

/* a^((p+1)/4); returns 1 when that is a genuine square root of a. */
int hazync_fq_sqrt_limbs(const uint32_t *a, uint32_t *out) {
    uint32_t x[8], e[8], chk[8]; uint64_t c = 0; int i;
    memcpy(x, a, 32); np_canon(x);
    for (i = 0; i < 8; i++) e[i] = NP[i];          /* p + 1 */
    c = (uint64_t)e[0] + 1; e[0] = (uint32_t)c; c >>= 32;
    for (i = 1; i < 8 && c; i++) { c += e[i]; e[i] = (uint32_t)c; c >>= 32; }
    for (i = 0; i < 7; i++) e[i] = (e[i] >> 2) | (e[i + 1] << 30);   /* / 4 */
    e[7] >>= 2;
    np_pow(out, x, e);
    np_mul(chk, out, out);
    for (i = 0; i < 8; i++) { if (chk[i] != x[i]) return 0; }
    return 1;
}

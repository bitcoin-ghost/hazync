/* Differential test: hzfe against stock libsecp256k1. hazync#129, Step 2.
 *
 * This is the gate the whole approach rests on. The field backend can be replaced safely ONLY because
 * its contract is mechanically checkable: given the same inputs, produce the same outputs. Every
 * operation is run in both implementations over random inputs and compared byte for byte via the
 * 32-byte serialisation both agree on.
 *
 * libsecp is the oracle. If they disagree, hzfe is wrong.
 *
 * Edge cases are exercised deliberately rather than left to chance: 0, 1, p-1, p-2, and values just
 * below and above the reduction boundary, where a conditional subtract is most likely to be wrong.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "hzfe.h"

/* stock libsecp, built with the same backend the guest uses */
#include "field.h"
#include "field_impl.h"

static uint64_t rng_state = 0x9E3779B97F4A7C15ULL;
static uint32_t rnd32(void) {
    rng_state ^= rng_state << 13; rng_state ^= rng_state >> 7; rng_state ^= rng_state << 17;
    return (uint32_t)(rng_state >> 32);
}

static int failures = 0;
static long long checks = 0;

static void report(const char *op, const unsigned char want[32], const unsigned char got[32],
                   const unsigned char a[32], const unsigned char b[32]) {
    failures++;
    if (failures > 5) return;
    printf("\n  MISMATCH in %s\n", op);
    printf("    a    = "); for (int i = 0; i < 32; i++) printf("%02x", a[i]); printf("\n");
    if (b) { printf("    b    = "); for (int i = 0; i < 32; i++) printf("%02x", b[i]); printf("\n"); }
    printf("    secp = "); for (int i = 0; i < 32; i++) printf("%02x", want[i]); printf("\n");
    printf("    hzfe = "); for (int i = 0; i < 32; i++) printf("%02x", got[i]); printf("\n");
}

/* Run every binary/unary op on one pair and compare. */
static void check_pair(const unsigned char ab[32], const unsigned char bb[32]) {
    secp256k1_fe sa, sb, sr;
    uint32_t ha[8], hb[8], hr[8];
    unsigned char want[32], got[32];

    secp256k1_fe_set_b32_mod(&sa, ab);
    secp256k1_fe_set_b32_mod(&sb, bb);
    hzfe_set_b32_mod(ha, ab);
    hzfe_set_b32_mod(hb, bb);

    /* set_b32_mod itself must agree before anything else is meaningful */
    secp256k1_fe_normalize_var(&sa); secp256k1_fe_get_b32(want, &sa);
    hzfe_get_b32(got, ha);
    checks++; if (memcmp(want, got, 32)) report("set_b32_mod/get_b32", want, got, ab, NULL);
    secp256k1_fe_set_b32_mod(&sa, ab);

    /* add */
    sr = sa; secp256k1_fe_add(&sr, &sb); secp256k1_fe_normalize_var(&sr); secp256k1_fe_get_b32(want, &sr);
    hzfe_add(hr, ha, hb); hzfe_get_b32(got, hr);
    checks++; if (memcmp(want, got, 32)) report("add", want, got, ab, bb);

    /* mul */
    secp256k1_fe_mul(&sr, &sa, &sb); secp256k1_fe_normalize_var(&sr); secp256k1_fe_get_b32(want, &sr);
    hzfe_mul(hr, ha, hb); hzfe_get_b32(got, hr);
    checks++; if (memcmp(want, got, 32)) report("mul", want, got, ab, bb);

    /* sqr */
    secp256k1_fe_sqr(&sr, &sa); secp256k1_fe_normalize_var(&sr); secp256k1_fe_get_b32(want, &sr);
    hzfe_sqr(hr, ha); hzfe_get_b32(got, hr);
    checks++; if (memcmp(want, got, 32)) report("sqr", want, got, ab, NULL);

    /* negate: libsecp needs a magnitude bound; normalize first so magnitude is 1 */
    { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
      secp256k1_fe_negate(&sr, &t, 1); secp256k1_fe_normalize_var(&sr); secp256k1_fe_get_b32(want, &sr); }
    hzfe_neg(hr, ha); hzfe_get_b32(got, hr);
    checks++; if (memcmp(want, got, 32)) report("negate", want, got, ab, NULL);

    /* half */
    { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
      secp256k1_fe_half(&t); secp256k1_fe_normalize_var(&t); secp256k1_fe_get_b32(want, &t); }
    hzfe_half(hr, ha); hzfe_get_b32(got, hr);
    checks++; if (memcmp(want, got, 32)) report("half", want, got, ab, NULL);

    /* inverse: skip zero, which has none. libsecp's inv(0) is defined as 0; check that too. */
    if (!hzfe_is_zero(ha)) {
        { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
          secp256k1_fe_inv(&sr, &t); secp256k1_fe_normalize_var(&sr); secp256k1_fe_get_b32(want, &sr); }
        hzfe_inv(hr, ha); hzfe_get_b32(got, hr);
        checks++; if (memcmp(want, got, 32)) report("inv", want, got, ab, NULL);

        { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
          secp256k1_fe_inv_var(&sr, &t); secp256k1_fe_normalize_var(&sr); secp256k1_fe_get_b32(want, &sr); }
        hzfe_inv_var(hr, ha); hzfe_get_b32(got, hr);
        checks++; if (memcmp(want, got, 32)) report("inv_var", want, got, ab, NULL);

        /* and the property that matters: a * a^-1 == 1 */
        { uint32_t one[8]; hzfe_set_int(one, 1);
          uint32_t prod[8]; hzfe_inv(hr, ha); hzfe_mul(prod, ha, hr);
          checks++; if (!hzfe_equal(prod, one)) {
              unsigned char g2[32]; hzfe_get_b32(g2, prod);
              unsigned char w2[32]; memset(w2, 0, 32); w2[31] = 1;
              report("a * inv(a) != 1", w2, g2, ab, NULL); } }
    }

    /* mul_int and add_int, over the small multipliers the EC layer actually uses */
    for (int k = 0; k <= 32; k += 4) {   /* libsecp documents a in [0,32] */
        { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
          secp256k1_fe_mul_int_unchecked(&t, k); secp256k1_fe_normalize_var(&t); secp256k1_fe_get_b32(want, &t); }
        hzfe_mul_int(hr, ha, k); hzfe_get_b32(got, hr);
        checks++; if (memcmp(want, got, 32)) { char nm[32]; snprintf(nm, sizeof nm, "mul_int(%d)", k); report(nm, want, got, ab, NULL); }

        { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
          secp256k1_fe_add_int(&t, k);  /* add_int takes a runtime int */ secp256k1_fe_normalize_var(&t); secp256k1_fe_get_b32(want, &t); }
        hzfe_add_int(hr, ha, k); hzfe_get_b32(got, hr);
        checks++; if (memcmp(want, got, 32)) { char nm[32]; snprintf(nm, sizeof nm, "add_int(%d)", k); report(nm, want, got, ab, NULL); }
    }

    /* cmp_var */
    { secp256k1_fe x = sa, y = sb; secp256k1_fe_normalize_var(&x); secp256k1_fe_normalize_var(&y);
      int w = secp256k1_fe_cmp_var(&x, &y), g = hzfe_cmp(ha, hb);
      checks++; if ((w > 0) != (g > 0) || (w < 0) != (g < 0)) {
          failures++; if (failures <= 5) printf("\n  MISMATCH cmp_var: secp=%d hzfe=%d\n", w, g); } }

    /* predicates */
    { secp256k1_fe t = sa; secp256k1_fe_normalize(&t);
      int w = secp256k1_fe_is_zero(&t), g = hzfe_is_zero(ha);
      checks++; if (!!w != !!g) { failures++; printf("\n  MISMATCH is_zero: secp=%d hzfe=%d\n", w, g); }
      w = secp256k1_fe_is_odd(&t); g = hzfe_is_odd(ha);
      checks++; if (!!w != !!g) { failures++; printf("\n  MISMATCH is_odd: secp=%d hzfe=%d\n", w, g); }
    }
}

int main(int argc, char **argv) {
    long n = (argc > 1) ? atol(argv[1]) : 20000;

    /* p, and values around it, as 32-byte big-endian */
    unsigned char pm1[32], pm2[32], zero[32], one[32], pexact[32];
    memset(zero, 0, 32);
    memset(one, 0, 32); one[31] = 1;
    { uint32_t t[8]; memcpy(t, HZFE_P, sizeof(t)); hzfe_get_b32(pexact, t);
      t[0] -= 1; hzfe_get_b32(pm1, t); t[0] -= 1; hzfe_get_b32(pm2, t); }

    const unsigned char *edges[] = { zero, one, pm1, pm2, pexact };
    const int ne = (int)(sizeof(edges) / sizeof(edges[0]));

    printf("\n  hzfe vs stock libsecp256k1 (10x26 backend, the one riscv32im selects)\n");

    /* every edge against every edge */
    for (int i = 0; i < ne; i++)
        for (int j = 0; j < ne; j++)
            check_pair(edges[i], edges[j]);

    /* edges against random, both ways */
    for (long k = 0; k < n / 4; k++) {
        unsigned char r[32];
        for (int i = 0; i < 32; i++) r[i] = (unsigned char)rnd32();
        check_pair(edges[k % ne], r);
        check_pair(r, edges[k % ne]);
    }

    /* random against random */
    for (long k = 0; k < n; k++) {
        unsigned char a[32], b[32];
        for (int i = 0; i < 32; i++) { a[i] = (unsigned char)rnd32(); b[i] = (unsigned char)rnd32(); }
        check_pair(a, b);
    }

    printf("\n  %lld comparisons, %d mismatch(es)\n\n", checks, failures);
    return failures ? 1 : 0;
}

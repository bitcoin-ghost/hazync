/* Count the field operations one ECDSA verification performs, on the 10x26 backend.
 *
 * The vector is a real signature over a real message, verified with the library's public API, so the
 * counted path is the one the guest executes: secp256k1_ecdsa_verify -> ecmult -> field arithmetic.
 * Nothing is stubbed.
 *
 * hazync#129. See ../README.md for why the mul:add ratio decides whether the field backend rewrite
 * can pay for itself.
 */
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <secp256k1.h>

#define HZ_DECL_ONLY
extern unsigned long long hz_field_ops[];
extern const char *hz_field_op_names[];
#ifndef HZ_FIELD_OP_COUNT
/* Set by the generated table; declared here so this TU compiles standalone. */
extern int hz_field_op_count_probe;
#endif

/* A known-good secp256k1 ECDSA vector: privkey 1, message = sha256("hazync"), signature produced by
 * this same library at build time so the verification genuinely succeeds. */
static const unsigned char seckey[32] = {
    0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,1
};

int main(int argc, char **argv) {
    int n = (argc > 1) ? atoi(argv[1]) : 100;
    if (n < 1) n = 1;

    secp256k1_context *ctx = secp256k1_context_create(SECP256K1_CONTEXT_NONE);
    secp256k1_pubkey pub;
    secp256k1_ecdsa_signature sig;
    unsigned char msg[32];
    for (int i = 0; i < 32; i++) msg[i] = (unsigned char)(i * 7 + 3);

    if (!secp256k1_ec_pubkey_create(ctx, &pub, seckey)) { fprintf(stderr, "pubkey_create failed\n"); return 1; }
    if (!secp256k1_ecdsa_sign(ctx, &sig, msg, seckey, NULL, NULL)) { fprintf(stderr, "sign failed\n"); return 1; }

    /* Zero the counters AFTER setup, so keygen and signing are excluded and only verification counts. */
    extern int hz_n_ops(void);
    int nops = hz_n_ops();
    for (int i = 0; i < nops; i++) hz_field_ops[i] = 0;

    for (int i = 0; i < n; i++) {
        if (!secp256k1_ecdsa_verify(ctx, &sig, msg, &pub)) { fprintf(stderr, "verify failed at %d\n", i); return 1; }
    }

    unsigned long long total = 0;
    for (int i = 0; i < nops; i++) total += hz_field_ops[i];

    /* A profile that counted nothing is not a result, it is a broken harness. This fired when the build
     * selected the 5x52 backend and the 10x26 counters were never reached. */
    if (total == 0) {
        fprintf(stderr, "\nFATAL: every counter is zero.\n"
                "  The 10x26 backend was not compiled -- libsecp selected 5x52 because __int128 was\n"
                "  available. Build with -DUSE_FORCE_WIDEMUL_INT64=1.\n\n");
        return 2;
    }

    printf("\n  %d ECDSA verification(s), 10x26 backend (the one riscv32im selects)\n\n", n);
    printf("  %-26s %14s %12s %7s\n", "operation", "total", "per verify", "share");
    printf("  %-26s %14s %12s %7s\n", "--------------------------", "--------------", "------------", "-------");

    /* Simple selection sort by count, descending: the table is ~28 entries. */
    int idx[64]; for (int i = 0; i < nops && i < 64; i++) idx[i] = i;
    for (int i = 0; i < nops; i++)
        for (int j = i + 1; j < nops; j++)
            if (hz_field_ops[idx[j]] > hz_field_ops[idx[i]]) { int t = idx[i]; idx[i] = idx[j]; idx[j] = t; }

    for (int k = 0; k < nops; k++) {
        int i = idx[k];
        if (!hz_field_ops[i]) continue;
        printf("  %-26s %14llu %12.1f %6.1f%%\n", hz_field_op_names[i], hz_field_ops[i],
               (double)hz_field_ops[i] / n, 100.0 * (double)hz_field_ops[i] / (double)total);
    }

    /* The decision number. mul+sqr are what the precompile accelerates; add/negate/normalize are what a
     * fully-reduced [u32;8] representation makes MORE expensive, because lazy reduction disappears. */
    unsigned long long fast = 0, slow = 0;
    for (int i = 0; i < nops; i++) {
        const char *nm = hz_field_op_names[i];
        if (!strcmp(nm, "mul") || !strcmp(nm, "sqr")) fast += hz_field_ops[i];
        else if (!strncmp(nm, "normalize", 9) || !strcmp(nm, "add") || !strcmp(nm, "add_int")
                 || !strcmp(nm, "negate_unchecked") || !strcmp(nm, "mul_int_unchecked")) slow += hz_field_ops[i];
    }
    printf("\n  accelerated by sys_bigint (mul+sqr):        %llu  (%.1f per verify)\n", fast, (double)fast / n);
    printf("  penalised by losing lazy reduction:        %llu  (%.1f per verify)\n", slow, (double)slow / n);
    if (slow) printf("  ratio mul+sqr : add-class                  %.2f : 1\n", (double)fast / (double)slow);
    printf("\n  Interpretation: a high ratio means the rewrite has room to win. A ratio near or below 1\n");
    printf("  means the add-class penalty likely eats the multiply gain -- see ../README.md.\n\n");

    secp256k1_context_destroy(ctx);
    return 0;
}

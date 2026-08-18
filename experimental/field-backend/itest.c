/* Does the EC layer work on a backend with no magnitude system? hazync#129. */
#include <stdio.h>
#include <string.h>
#include <secp256k1.h>
#include <secp256k1_schnorrsig.h>
#include <secp256k1_extrakeys.h>

int main(void) {
    secp256k1_context *ctx = secp256k1_context_create(SECP256K1_CONTEXT_NONE);
    unsigned char sk[32], msg[32];
    int ok = 1, n = 0;
    for (int t = 1; t <= 50; t++) {
        for (int i = 0; i < 32; i++) { sk[i] = (unsigned char)(i * 3 + t); msg[i] = (unsigned char)(i * 7 + t); }
        sk[0] |= 1;
        secp256k1_pubkey pub; secp256k1_ecdsa_signature sig;
        if (!secp256k1_ec_pubkey_create(ctx, &pub, sk)) { printf("  pubkey_create FAILED t=%d\n", t); ok = 0; break; }
        if (!secp256k1_ecdsa_sign(ctx, &sig, msg, sk, NULL, NULL)) { printf("  sign FAILED t=%d\n", t); ok = 0; break; }
        if (!secp256k1_ecdsa_verify(ctx, &sig, msg, &pub)) { printf("  ECDSA verify FAILED t=%d\n", t); ok = 0; break; }
        /* a wrong message must NOT verify -- a backend that says yes to everything would pass above */
        unsigned char bad[32]; memcpy(bad, msg, 32); bad[0] ^= 1;
        if (secp256k1_ecdsa_verify(ctx, &sig, bad, &pub)) { printf("  ECDSA accepted a WRONG message t=%d\n", t); ok = 0; break; }
        secp256k1_keypair kp; secp256k1_xonly_pubkey xp; unsigned char s64[64];
        if (!secp256k1_keypair_create(ctx, &kp, sk)) { printf("  keypair FAILED\n"); ok = 0; break; }
        if (!secp256k1_keypair_xonly_pub(ctx, &xp, NULL, &kp)) { printf("  xonly FAILED\n"); ok = 0; break; }
        if (!secp256k1_schnorrsig_sign32(ctx, s64, msg, &kp, NULL)) { printf("  schnorr sign FAILED\n"); ok = 0; break; }
        if (!secp256k1_schnorrsig_verify(ctx, s64, msg, 32, &xp)) { printf("  Schnorr verify FAILED t=%d\n", t); ok = 0; break; }
        if (secp256k1_schnorrsig_verify(ctx, s64, bad, 32, &xp)) { printf("  Schnorr accepted a WRONG message t=%d\n", t); ok = 0; break; }
        n++;
    }
    printf("\n  %d/50 rounds: ECDSA sign+verify, Schnorr sign+verify, and both rejecting a tampered message\n", n);
    printf("  %s\n\n", ok ? "PASS -- the EC layer works on a magnitude-free backend" : "FAIL");
    secp256k1_context_destroy(ctx);
    return ok ? 0 : 1;
}

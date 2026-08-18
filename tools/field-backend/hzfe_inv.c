/* Modular inverse for hzfe, reusing libsecp's safegcd unchanged. hazync#129.
 *
 * MEASURED FIRST. Per verification, on the 10x26 backend:
 *
 *     ECDSA     inv, inv_var:  0 calls
 *     Schnorr   inv_var:       1 call        (against 936 mul and 947 add)
 *
 * Inversion is not on the hot path, so the two usual options are both wrong. Reimplementing safegcd in
 * [u32; 8] would be the most delicate code in this backend, written for 0.03% of the field operations.
 * Fermat inversion (a^(p-2)) is easy and would cost ~384 precompile calls, about 23% on top of the
 * 1,643 mul+sqr a verify already performs -- a bad trade to serve one call.
 *
 * REVISED 2026-08-18. The first version of this file converted to `secp256k1_fe` and called
 * `secp256k1_fe_inv`. That works while hzfe sits alongside the stock backend, and breaks the moment
 * hzfe IS the backend: the call becomes circular. Delegating to a thing you are about to replace is
 * only a strategy until you replace it.
 *
 * What actually survives is that `secp256k1_modinv32` and `secp256k1_jacobi32_maybe_var` operate on
 * `secp256k1_modinv32_signed30`, not on `secp256k1_fe`. They are representation-independent. So the
 * only thing needed is a conversion between [u32; 8] and signed30, and libsecp's safegcd is reused
 * exactly as written -- which is the point, because it is the subtlest code in the library.
 */
#include "hzfe.h"
#include <string.h>
#include "modinv32.h"
#include "modinv32_impl.h"

/* The field modulus in the form safegcd wants. Lifted verbatim from field_10x26_impl.h, where it is
 * `secp256k1_const_modinfo_fe`: p in signed30 limbs, and 1/p mod 2^30. */
static const secp256k1_modinv32_modinfo hzfe_modinfo_p = {
    {{-0x3D1, -4, 0, 0, 0, 0, 0, 0, 65536}},
    0x2DDACACFL
};

#define M30 ((uint32_t)(UINT32_MAX >> 2))     /* 0x3FFFFFFF */

/* [u32; 8] little-endian -> 9 limbs of 30 bits. 8*32 = 256 bits into 9*30 = 270, so the top limb is
 * partial. Done through a rolling 64-bit window rather than per-limb shifts, because a shift by 32 on
 * a 32-bit type is undefined and the boundary cases are exactly where that bites. */
static void hzfe_to_signed30(secp256k1_modinv32_signed30 *r, const uint32_t a[8]) {
    uint64_t acc = 0;
    int bits = 0, src = 0;
    for (int i = 0; i < 9; i++) {
        while (bits < 30 && src < 8) {
            acc |= (uint64_t)a[src++] << bits;
            bits += 32;
        }
        r->v[i] = (int32_t)((uint32_t)acc & M30);
        acc >>= 30;
        bits -= 30;
        if (bits < 0) bits = 0;
    }
}

/* 9 limbs of 30 bits -> [u32; 8]. The input is normalised to [0, p) by modinv32, so it fits. */
static void hzfe_from_signed30(uint32_t r[8], const secp256k1_modinv32_signed30 *a) {
    uint64_t acc = 0;
    int bits = 0, dst = 0;
    for (int i = 0; i < 9 && dst < 8; i++) {
        acc |= (uint64_t)(uint32_t)a->v[i] << bits;
        bits += 30;
        while (bits >= 32 && dst < 8) {
            r[dst++] = (uint32_t)acc;
            acc >>= 32;
            bits -= 32;
        }
    }
    while (dst < 8) r[dst++] = (uint32_t)acc, acc >>= 32;
}

void hzfe_inv(uint32_t r[8], const uint32_t a[8]) {
    secp256k1_modinv32_signed30 s;
    hzfe_to_signed30(&s, a);
    secp256k1_modinv32(&s, &hzfe_modinfo_p);
    hzfe_from_signed30(r, &s);
}

void hzfe_inv_var(uint32_t r[8], const uint32_t a[8]) {
    secp256k1_modinv32_signed30 s;
    hzfe_to_signed30(&s, a);
    secp256k1_modinv32_var(&s, &hzfe_modinfo_p);
    hzfe_from_signed30(r, &s);
}

/* Quadratic residue test. Measured at 0 calls on both verification paths, so correctness matters and
 * speed does not. Same delegation: jacobi32 takes signed30 and is representation-independent. */
int hzfe_is_square_var(const uint32_t a[8]) {
    secp256k1_modinv32_signed30 s;
    if (hzfe_is_zero(a)) return 1;            /* jacobi32 cannot take 0; libsecp returns 1 for it */
    hzfe_to_signed30(&s, a);
    return secp256k1_jacobi32_maybe_var(&s, &hzfe_modinfo_p) >= 0;
}

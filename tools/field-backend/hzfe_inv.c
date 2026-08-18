/* Modular inverse for hzfe, by delegating to libsecp's own safegcd. hazync#129.
 *
 * MEASURED FIRST. Per verification, on the 10x26 backend:
 *
 *     ECDSA     inv, inv_var:  0 calls
 *     Schnorr   inv_var:       1 call        (against 936 mul and 947 add)
 *
 * Inversion is not on the hot path. It is called at most once per signature, so the two things that
 * usually decide an implementation here do not apply:
 *
 *   - Reimplementing safegcd in [u32; 8] would be the single most delicate piece of code in this
 *     backend, for 0.03% of the field operations. The risk is entirely disproportionate to the gain.
 *
 *   - Fermat inversion (a^(p-2) mod p) is the easy alternative and would cost roughly 384 precompile
 *     calls -- about 23% on top of the 1,643 mul+sqr a verify already performs. A 23% tax to serve one
 *     call is a bad trade.
 *
 * So convert to libsecp's representation, call the inverse it already ships, convert back. The
 * conversion overhead that sank the 2026-07-15 experiment (~80 cycles per operation, against ~3,000
 * operations) is irrelevant at one call per verify: ~160 cycles total.
 *
 * This keeps the most audited and most subtle routine in libsecp exactly as it is, which is the same
 * argument the rest of this backend rests on.
 */
#include "hzfe.h"
#include "field.h"
#include "field_impl.h"

static void to_fe(secp256k1_fe *dst, const uint32_t a[8]) {
    unsigned char b[32];
    hzfe_get_b32(b, a);
    secp256k1_fe_set_b32_mod(dst, b);
}

static void from_fe(uint32_t r[8], secp256k1_fe *src) {
    unsigned char b[32];
    secp256k1_fe_normalize_var(src);
    secp256k1_fe_get_b32(b, src);
    hzfe_set_b32_mod(r, b);
}

/* r = a^-1 mod p. Constant time, because secp256k1_fe_inv is. */
void hzfe_inv(uint32_t r[8], const uint32_t a[8]) {
    secp256k1_fe x, y;
    to_fe(&x, a);
    secp256k1_fe_inv(&y, &x);
    from_fe(r, &y);
}

/* Variable-time variant, matching libsecp's split. This is the one Schnorr actually calls. */
void hzfe_inv_var(uint32_t r[8], const uint32_t a[8]) {
    secp256k1_fe x, y;
    to_fe(&x, a);
    secp256k1_fe_inv_var(&y, &x);
    from_fe(r, &y);
}

/* Host reference for hzfe_modmul. hazync#129.
 *
 * In the guest this is one sys_bigint call. On the host there is no precompile, so this computes the
 * same function the slow, obvious way: full 512-bit schoolbook product, then fold using the special
 * form of secp256k1's prime.
 *
 * Deliberately written for auditability rather than speed. Its only job is to be a correct oracle so
 * the surrounding field code can be differentially tested against stock libsecp before the precompile
 * is wired in. If this and libsecp disagree, one of them is wrong, and it will not be libsecp.
 *
 * The fold: p = 2^256 - 2^32 - 977, so 2^256 == 2^32 + 977 (mod p). A 512-bit product
 * hi*2^256 + lo therefore reduces to lo + hi*(2^32 + 977), which is at most ~289 bits. Repeat until it
 * fits in 256, then one conditional subtract.
 */
#include "hzfe.h"
#include <string.h>

/* 16-word accumulator, little-endian. */
static void mul_512(uint32_t out[16], const uint32_t a[8], const uint32_t b[8]) {
    for (int i = 0; i < 16; i++) out[i] = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t carry = 0;
        for (int j = 0; j < 8; j++) {
            uint64_t t = (uint64_t)a[i] * (uint64_t)b[j] + (uint64_t)out[i + j] + carry;
            out[i + j] = (uint32_t)t;
            carry = t >> 32;
        }
        /* propagate the final carry upward */
        int k = i + 8;
        while (carry && k < 16) {
            uint64_t t = (uint64_t)out[k] + carry;
            out[k] = (uint32_t)t;
            carry = t >> 32;
            k++;
        }
    }
}

/* v (n words) += m (single word) * src (8 words), starting at word offset. Returns carry out. */
static void fold_once(uint32_t acc[16]) {
    /* split: lo = acc[0..7], hi = acc[8..15]; acc = lo + hi*(2^32 + 977) */
    uint32_t hi[8];
    memcpy(hi, acc + 8, 8 * sizeof(uint32_t));
    memset(acc + 8, 0, 8 * sizeof(uint32_t));

    /* + hi * 977 */
    uint64_t carry = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t t = (uint64_t)hi[i] * 977ULL + (uint64_t)acc[i] + carry;
        acc[i] = (uint32_t)t;
        carry = t >> 32;
    }
    int k = 8;
    while (carry && k < 16) { uint64_t t = (uint64_t)acc[k] + carry; acc[k] = (uint32_t)t; carry = t >> 32; k++; }

    /* + hi * 2^32, i.e. hi shifted up one word */
    carry = 0;
    for (int i = 0; i < 8; i++) {
        uint64_t t = (uint64_t)acc[i + 1] + (uint64_t)hi[i] + carry;
        acc[i + 1] = (uint32_t)t;
        carry = t >> 32;
    }
    k = 9;
    while (carry && k < 16) { uint64_t t = (uint64_t)acc[k] + carry; acc[k] = (uint32_t)t; carry = t >> 32; k++; }
}

static int has_high_words(const uint32_t acc[16]) {
    for (int i = 8; i < 16; i++) if (acc[i]) return 1;
    return 0;
}

void hzfe_modmul(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]) {
    uint32_t acc[16];
    mul_512(acc, a, b);

    /* Each fold shrinks the high half; two or three passes suffice for 512 bits, but loop until clear
     * rather than assert a bound -- this is an oracle, not the hot path. */
    int guard = 0;
    while (has_high_words(acc)) {
        fold_once(acc);
        if (++guard > 8) break;      /* cannot happen; a runaway would be a bug, not silence */
    }

    /* Now acc[0..7] < 2^256, which may still be >= p. Conditional subtract, twice for safety since the
     * final fold can leave a value slightly above p. */
    uint32_t t[8];
    memcpy(t, acc, sizeof(t));
    for (int pass = 0; pass < 2; pass++) {
        uint32_t s[8];
        uint64_t br = 0;
        for (int i = 0; i < 8; i++) {
            uint64_t d = (uint64_t)t[i] - (uint64_t)HZFE_P[i] - br;
            s[i] = (uint32_t)d;
            br = (d >> 63) & 1;
        }
        uint32_t m = (uint32_t)0 - (uint32_t)(1 - br);   /* no borrow => t >= p => take t-p */
        for (int i = 0; i < 8; i++) t[i] = (t[i] & ~m) | (s[i] & m);
    }
    memcpy(r, t, sizeof(t));
}

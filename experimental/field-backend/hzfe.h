/* hzfe: a secp256k1 field element in sys_bigint-native form. hazync#129.
 *
 * Representation: 8 little-endian 32-bit words, ALWAYS fully reduced (value < p).
 *
 * This is the shape RISC0's bigint precompile consumes:
 *     sys_bigint(result: *mut [u32; 8], OP_MULTIPLY, x, y, modulus)
 *
 * Why this rather than intercepting libsecp's multiply in place: that was tried and measured at +10%
 * (docs/ACCELERATION.md, 2026-07-15). Converting 10x26 <-> [u32;8] per operation costs about as much as
 * the multiply it replaces. The speedup only exists if elements stay in this form the entire time,
 * which is what makes this a backend rather than a call swap.
 *
 * What being fully reduced buys, beyond feeding the precompile: libsecp's 10x26 backend carries a
 * magnitude/normalised invariant system, because it defers reduction. Here there is nothing to defer,
 * so normalize / normalize_weak / normalizes_to_zero / get_bounds / verify collapse to no-ops or
 * one-liners. Most of the 28-function backend contract disappears.
 *
 * What it costs: every add needs carry propagation and a conditional subtract of p. Measured at 156
 * rv32im instructions against 57 for the 10x26 add. That is 2.7x on an operation that is 3.3% of a
 * verify, against removing a 1,141-instruction multiply that is 64.5% of one.
 */
#ifndef HZFE_H
#define HZFE_H

#include <stdint.h>

/* p = 2^256 - 2^32 - 977 */
extern const uint32_t HZFE_P[8];

/* The modular multiply is pluggable so the same field code runs in two places:
 *   - on the host, against a reference implementation, for differential testing
 *   - in the guest, against sys_bigint
 * Only this one function differs between them. */
void hzfe_modmul(uint32_t r[8], const uint32_t a[8], const uint32_t b[8]);

void hzfe_add (uint32_t r[8], const uint32_t a[8], const uint32_t b[8]);
void hzfe_sub (uint32_t r[8], const uint32_t a[8], const uint32_t b[8]);
void hzfe_neg (uint32_t r[8], const uint32_t a[8]);
void hzfe_mul (uint32_t r[8], const uint32_t a[8], const uint32_t b[8]);
void hzfe_sqr (uint32_t r[8], const uint32_t a[8]);
void hzfe_half(uint32_t r[8], const uint32_t a[8]);

/* 32-byte big-endian, matching secp256k1_fe_get_b32 / set_b32. Inputs >= p are reduced (set_b32_mod
 * semantics); hzfe_set_b32_limit returns 0 if the input was >= p, like libsecp's variant. */
void hzfe_get_b32(unsigned char out[32], const uint32_t a[8]);
void hzfe_set_b32_mod(uint32_t r[8], const unsigned char in[32]);
int  hzfe_set_b32_limit(uint32_t r[8], const unsigned char in[32]);

/* Inverse delegates to libsecp's safegcd (hzfe_inv.c). Measured at <=1 call per verification, so the
 * conversion at the boundary is free and reimplementing safegcd would be risk without reward. */
void hzfe_inv    (uint32_t r[8], const uint32_t a[8]);
void hzfe_inv_var(uint32_t r[8], const uint32_t a[8]);
int  hzfe_is_square_var(const uint32_t a[8]);

/* r = a * k mod p, for the small integer multipliers the EC layer uses (typically 2..8). Done by
 * double-and-add rather than a modmul: at 127 calls per verify, a precompile invocation each would
 * cost more than the additions it replaces. */
void hzfe_mul_int(uint32_t r[8], const uint32_t a[8], int k);

/* r = a + k mod p, k a small non-negative integer. */
void hzfe_add_int(uint32_t r[8], const uint32_t a[8], int k);

/* Integer comparison of two reduced elements: -1, 0, 1. Variable time, matching cmp_var. */
int  hzfe_cmp(const uint32_t a[8], const uint32_t b[8]);

int  hzfe_is_zero(const uint32_t a[8]);
int  hzfe_is_odd (const uint32_t a[8]);
int  hzfe_equal  (const uint32_t a[8], const uint32_t b[8]);
void hzfe_cmov   (uint32_t r[8], const uint32_t a[8], int flag);
void hzfe_set_int(uint32_t r[8], uint32_t v);

#endif

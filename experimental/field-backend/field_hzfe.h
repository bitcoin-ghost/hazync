/* libsecp256k1 field backend: fully-reduced [u32; 8], for RISC0's sys_bigint precompile. hazync#129.
 *
 * A third alternative to field_5x52.h (64-bit hosts) and field_10x26.h (32-bit hosts). Selected in
 * field.h alongside those.
 *
 * The representation is 8 little-endian 32-bit words holding a value ALWAYS in [0, p). That differs
 * from both stock backends in one structural way: they use lazy reduction, deferring normalisation and
 * tracking how far a value has drifted with a "magnitude". Here nothing is deferred, so:
 *
 *   - magnitude is always 1 and normalized is always true
 *   - normalize, normalize_weak and normalize_var are no-ops
 *   - the magnitude argument to negate and mul_int is accepted and ignored
 *
 * The VERIFY fields are still declared, because field.h's shared verification macros reference them.
 * They are maintained at their constant values so a VERIFY build agrees with itself.
 *
 * Storage is bit-identical to the value, since both are 8 fully-reduced words -- to_storage and
 * from_storage are memcpy, where 10x26 has to repack limbs.
 */
#ifndef SECP256K1_FIELD_REPR_H
#define SECP256K1_FIELD_REPR_H

#include <stdint.h>

typedef struct {
    /* A field element f represents the integer sum(i=0..7, f.n[i] << (i*32)), which is always < p.
     * Unlike the 5x52 and 10x26 backends there is no excess: every limb is a full 32 bits and the
     * value is reduced after every operation. */
    uint32_t n[8];
    SECP256K1_FE_VERIFY_FIELDS
} secp256k1_fe;

/* Unpacking a constant is the identity here, up to word order: the input is already eight 32-bit
 * words, least significant last. 10x26 has to redistribute them across ten 26-bit limbs. */
#define SECP256K1_FE_CONST_INNER(d7, d6, d5, d4, d3, d2, d1, d0) { \
    (uint32_t)(d0), (uint32_t)(d1), (uint32_t)(d2), (uint32_t)(d3), \
    (uint32_t)(d4), (uint32_t)(d5), (uint32_t)(d6), (uint32_t)(d7) \
}

typedef struct {
    uint32_t n[8];
} secp256k1_fe_storage;

#define SECP256K1_FE_STORAGE_CONST(d7, d6, d5, d4, d3, d2, d1, d0) {{ (d0), (d1), (d2), (d3), (d4), (d5), (d6), (d7) }}
#define SECP256K1_FE_STORAGE_CONST_GET(d) d.n[7], d.n[6], d.n[5], d.n[4], d.n[3], d.n[2], d.n[1], d.n[0]

#endif /* SECP256K1_FIELD_REPR_H */

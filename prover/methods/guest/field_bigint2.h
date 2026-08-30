/***********************************************************************
 * hazync: field backend backed by the RISC0 bigint2 coprocessor.      *
 * Distributed under the MIT software license, see the accompanying    *
 * file COPYING or https://www.opensource.org/licenses/mit-license.php.*
 ***********************************************************************/

#ifndef SECP256K1_FIELD_REPR_H
#define SECP256K1_FIELD_REPR_H

#include <stdint.h>

/** Field elements are stored CANONICALLY: 8 little-endian 32-bit limbs, value always < p, always
 *  normalized, magnitude always 1 (or 0 for zero).
 *
 *  This is deliberately NOT the 10x26 lazy-reduction scheme. The coprocessor takes canonical
 *  values, and MEASURED in-guest: a coprocessor multiply is 83 cycles in native form but 854 once
 *  10x26 conversion is added — the conversion costs 771 cycles, 9.3x the operation itself. Keeping
 *  the representation canonical means conversion never happens.
 *
 *  The cost of that choice: `add` can no longer be ten bare limb additions with the magnitude
 *  absorbing the excess (~10 cycles); it must reduce every time (~50 cycles in C). There are more
 *  adds than multiplies, so this is a real cost — it is simply much smaller than the 1,084 cycles
 *  each multiply saves. See docs/FIELD_BIGINT2_BACKEND.md §3.
 *
 *  Because every element is always canonical, magnitude is not tracked: normalize, normalize_weak,
 *  normalize_var and get_bounds are no-ops or trivial. */
typedef struct {
    uint32_t n[8];
    SECP256K1_FE_VERIFY_FIELDS
} secp256k1_fe;

/* Canonical limbs are exactly the constant's words, so this is a direct unpack — unlike 10x26,
 * which has to re-split them across 26-bit boundaries. */
#define SECP256K1_FE_CONST_INNER(d7, d6, d5, d4, d3, d2, d1, d0) { \
    (d0), (d1), (d2), (d3), (d4), (d5), (d6), (d7) \
}

typedef struct {
    uint32_t n[8];
} secp256k1_fe_storage;

#define SECP256K1_FE_STORAGE_CONST(d7, d6, d5, d4, d3, d2, d1, d0) {{ (d0), (d1), (d2), (d3), (d4), (d5), (d6), (d7) }}
#define SECP256K1_FE_STORAGE_CONST_GET(d) d.n[7], d.n[6], d.n[5], d.n[4],d.n[3], d.n[2], d.n[1], d.n[0]

#endif /* SECP256K1_FIELD_REPR_H */

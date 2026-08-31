#ifndef HZ_TESTSTUB_MODINV_H
#define HZ_TESTSTUB_MODINV_H
#include <stdint.h>
/* Minimal stand-ins so field_bigint2_impl.h compiles standalone. The harness never calls the
 * modinv32 machinery -- it exercises the mod-p arithmetic -- so these only need to typecheck. */
typedef struct { int32_t v[9]; } secp256k1_modinv32_signed30;
typedef struct { secp256k1_modinv32_signed30 modulus; uint32_t modulus_inv30; } secp256k1_modinv32_modinfo;
static int secp256k1_jacobi32_maybe_var(const secp256k1_modinv32_signed30 *x,
                                        const secp256k1_modinv32_modinfo *m) { (void)x; (void)m; return 0; }
#endif

#ifndef HZ_TESTSTUB_FIELD_H
#define HZ_TESTSTUB_FIELD_H
#include "util.h"
typedef struct { uint32_t n[8]; } secp256k1_fe;
typedef struct { uint32_t n[8]; } secp256k1_fe_storage;
static void secp256k1_fe_normalize_var(secp256k1_fe *r);
static int secp256k1_fe_is_zero(const secp256k1_fe *a);
static int secp256k1_fe_sqrt(secp256k1_fe *r, const secp256k1_fe *a);
#endif

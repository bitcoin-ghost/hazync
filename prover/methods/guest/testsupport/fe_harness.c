/* Host driver for the bigint2 field backend's mod-p operations.
 *
 * Reads commands on stdin, prints results on stdout; scripts/field-backend-tests.sh cross-checks
 * them against Python arbitrary precision. It compiles the REAL field_bigint2_impl.h against the
 * stubs in stub/, so it validates the shipped file rather than a copy of it.
 *
 * This exists because libsecp's own suite cannot reach the lazy states -- see README.md.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>

void hazync_fq_mul_limbs(const uint32_t *a, const uint32_t *b, uint32_t *o){(void)a;(void)b;(void)o;}
void hazync_fq_sqr_limbs(const uint32_t *a, uint32_t *o){(void)a;(void)o;}
void hazync_fq_inv_limbs(const uint32_t *a, uint32_t *o){(void)a;(void)o;}
int  hazync_fq_sqrt_limbs(const uint32_t *a, uint32_t *o){(void)a;(void)o;return 0;}

#include "field_bigint2_impl.h"

static void pr(const uint32_t *n){ int i; printf("R "); for(i=7;i>=0;i--) printf("%08x",n[i]); printf("\n"); }
static void rd(uint32_t *n){ int i; unsigned v; for(i=0;i<8;i++){ if(scanf("%x",&v)!=1) v=0; n[i]=v; } }
static void rdb(unsigned char *b){ int i; unsigned v; for(i=0;i<32;i++){ if(scanf("%x",&v)!=1) v=0; b[i]=(unsigned char)v; } }

int main(void){
    char op[32];
    while (scanf("%31s", op) == 1) {
        secp256k1_fe x, y; uint32_t a[8]; unsigned char b32[32]; int k, i;
        if (!strcmp(op,"add"))            { rd(a); memcpy(x.n,a,32); rd(a); memcpy(y.n,a,32);
                                            secp256k1_fe_impl_add(&x,&y); pr(x.n); }
        else if (!strcmp(op,"addint"))    { rd(x.n); if(scanf("%d",&k)!=1) k=0;
                                            secp256k1_fe_impl_add_int(&x,k); pr(x.n); }
        else if (!strcmp(op,"neg"))       { rd(x.n); secp256k1_fe_impl_negate_unchecked(&x,&x,1); pr(x.n); }
        else if (!strcmp(op,"mulint"))    { rd(x.n); if(scanf("%d",&k)!=1) k=0;
                                            secp256k1_fe_impl_mul_int_unchecked(&x,k); pr(x.n); }
        else if (!strcmp(op,"half"))      { rd(x.n); secp256k1_fe_impl_half(&x); pr(x.n); }
        else if (!strcmp(op,"canon"))     { rd(x.n); secp256k1_fe_impl_normalize(&x); pr(x.n); }
        else if (!strcmp(op,"iszero"))    { rd(x.n); printf("R %d\n", secp256k1_fe_impl_normalizes_to_zero(&x)); }
        else if (!strcmp(op,"bounds"))    { if(scanf("%d",&k)!=1) k=0; secp256k1_fe_impl_get_bounds(&x,k); pr(x.n); }
        else if (!strcmp(op,"limit"))     { rdb(b32); printf("R %d\n", secp256k1_fe_impl_set_b32_limit(&x,b32)); }
        else if (!strcmp(op,"roundtrip")) { rdb(b32); secp256k1_fe_impl_set_b32_mod(&x,b32);
                                            secp256k1_fe_impl_normalize(&x);
                                            secp256k1_fe_impl_get_b32(b32,&x);
                                            printf("R "); for(i=0;i<32;i++) printf("%02x",b32[i]); printf("\n"); }
        /* the modinv32 interop, on LAZY inputs -- the path libsecp's own tests never reach */
        else if (!strcmp(op,"signed30"))  { secp256k1_modinv32_signed30 s;
                                            rd(x.n);
                                            secp256k1_fe_to_signed30(&s,&x);
                                            secp256k1_fe_from_signed30(&y,&s);
                                            pr(y.n); }
        else if (!strcmp(op,"storagecmov")){ secp256k1_fe_storage r, s;
                                            rd(r.n); rd(s.n); if(scanf("%d",&k)!=1) k=0;
                                            secp256k1_fe_storage_cmov(&r,&s,k); pr(r.n); }
        else { fprintf(stderr,"unknown op '%s'\n", op); return 2; }
    }
    return 0;
}

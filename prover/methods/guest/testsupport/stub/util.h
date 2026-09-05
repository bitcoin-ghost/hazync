#ifndef HZ_TESTSTUB_UTIL_H
#define HZ_TESTSTUB_UTIL_H
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define SECP256K1_INLINE inline
#define SECP256K1_RESTRICT restrict
#define VERIFY_CHECK(c) do { if(!(c)){ fprintf(stderr,"VERIFY_CHECK failed: %s\n", #c); abort(); } } while(0)
#endif

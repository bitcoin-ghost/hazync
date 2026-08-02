/* Freestanding libc shims for the guest.
 *
 * The guest has no OS. These satisfy references pulled in by libstdc++ and newlib when Bitcoin Core
 * is compiled to RISC-V. NONE of them is reachable from the consensus path — verified against the
 * linked guest ELF, see test-cshims.sh and the call-site table below — but they are compiled into
 * the image, so a defect here changes what the proof attests. Treat them as consensus code.
 *
 * Call sites in the linked guest (resolved with objdump, hazync#55):
 *   strcmp   51x  libstdc++ internals
 *   strtoul   4x  __strftime, _tzset_unlocked_r, std::random_device::_M_init_pretr1, static init
 *   strchr    1x  libstdc++ static init
 *   getenv    1x  libstdc++ static init
 *   _sbrk     1x  _sbrk_r  <- _malloc_r AND _malloc_trim_r (the latter passes a NEGATIVE incr)
 *
 * Differentially tested against glibc by test-cshims.sh, which also carries positive controls
 * proving each assertion fails against the previous implementation.
 */
#include <stddef.h>
#include <limits.h>

int strcmp(const char*a,const char*b){while(*a&&*a==*b){a++;b++;}return (unsigned char)*a-(unsigned char)*b;}
char* strchr(const char*s,int c){while(*s){if(*s==(char)c)return (char*)s;s++;}return c?(char*)0:(char*)s;}

/* Deterministic by construction: the guest must not read an environment it does not have. Returning
 * NULL is the answer, not a stub — it is also why _tzset_unlocked_r never gets a string to parse. */
char* getenv(const char*n){(void)n;return (char*)0;}

/* strtoul, standard semantics.
 *
 * The previous implementation ignored `base` entirely and always parsed decimal, with no leading
 * whitespace, sign or `0x` handling and no overflow detection — so a base-16 caller received a
 * silently wrong value. That was inert (no consensus caller, and the one env-driven caller is fed
 * NULL by getenv above) but it is the worst failure shape there is, so it is fixed rather than
 * documented.
 *
 * DELIBERATE DEVIATION: errno is not set on overflow (ERANGE) or an invalid base (EINVAL). The
 * return values match C99 in both cases; only the errno side effect is absent. No caller in the
 * guest reads errno, and adding the dependency buys nothing. test-cshims.sh asserts the return
 * values and endptr against glibc and skips errno for this reason.
 */
unsigned long strtoul(const char* s, char** e, int b) {
    const char* p = s;
    unsigned long r = 0, cutoff;
    int neg = 0, any = 0, cutlim, d;

    while (*p == ' ' || (*p >= '\t' && *p <= '\r')) p++;
    if (*p == '+' || *p == '-') { neg = (*p == '-'); p++; }

    /* 0x prefix only counts when a hex digit actually follows; otherwise "0x" parses as "0" with
     * endptr after the zero, which is what C99 requires and what glibc does. */
    if ((b == 0 || b == 16) && p[0] == '0' && (p[1] == 'x' || p[1] == 'X')
        && (((unsigned)(p[2] - '0') < 10u) || ((unsigned)((p[2] | 32) - 'a') < 6u))) {
        p += 2; b = 16;
    } else if (b == 0) {
        b = (p[0] == '0') ? 8 : 10;
    }

    if (b < 2 || b > 36) { if (e) *e = (char*)s; return 0; }

    cutoff = ULONG_MAX / (unsigned long)b;
    cutlim = (int)(ULONG_MAX % (unsigned long)b);
    for (;;) {
        unsigned char c = (unsigned char)*p;
        if ((unsigned)(c - '0') < 10u)              d = c - '0';
        else if ((unsigned)((c | 32) - 'a') < 26u)  d = (c | 32) - 'a' + 10;
        else break;
        if (d >= b) break;
        if (any < 0 || r > cutoff || (r == cutoff && d > cutlim)) any = -1;     /* saturate */
        else { any = 1; r = r * (unsigned long)b + (unsigned long)d; }
        p++;
    }
    if (any < 0)   r = ULONG_MAX;
    else if (neg)  r = 0UL - r;
    if (e) *e = (char*)(any ? p : s);
    return r;
}

/* Static heap for newlib malloc's _sbrk (the guest has no OS).
 *
 * 1 MiB, and exhaustion is FAIL-CLOSED: this returns -1, malloc returns NULL, operator new throws
 * bad_alloc, nothing in the guest catches it, so std::terminate aborts and NO receipt is produced.
 * A block crafted to exhaust the heap yields no proof, never a wrong one. */
static char _heap[1 << 20];
static size_t _used = 0;

/* Both bounds are checked, and the arithmetic is done in integer space so that an out-of-range
 * pointer is never formed (forming one is UB even without dereferencing it).
 *
 * The lower bound is not theoretical: _malloc_trim_r calls this with a NEGATIVE incr to release
 * top-of-heap memory. The previous version checked only the upper bound, so a negative incr moved
 * the break unchecked and could in principle walk it below _heap, after which allocations would be
 * handed addresses outside the array. newlib never released more than it took, so this stayed
 * latent — but that was a property of the caller, not of this function. */
void* _sbrk(int incr) {
    char* old = _heap + _used;
    if (incr < 0) {
        size_t dec = (size_t)0 - (size_t)incr;   /* magnitude; well-defined even for INT_MIN */
        if (dec > _used) return (void*)-1;
        _used -= dec;
    } else {
        size_t inc = (size_t)incr;
        if (inc > sizeof(_heap) - _used) return (void*)-1;   /* cannot overflow: _used <= sizeof */
        _used += inc;
    }
    return old;
}

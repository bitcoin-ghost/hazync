#!/usr/bin/env bash
# Test the guest's libc shims (cshims.c) natively, against glibc.
#
#   ./prover/methods/guest/test-cshims.sh
#
# These nine-odd lines are compiled into the guest, so a defect in them changes what a proof
# attests. They are also the one part of the guest that needs no zkVM to exercise: the semantics are
# plain C, so they can be compiled for the host and differentially tested against glibc's real
# implementations. That is what this does.
#
# WHAT THIS DOES NOT COVER: the guest is RV32 (32-bit `unsigned long`), the host is LP64. The
# differential run therefore validates PARSING (bases, prefixes, signs, endptr, saturation
# behaviour) at the host's width, not the exact 32-bit saturation boundary. The logic is written in
# terms of ULONG_MAX so it is width-independent, but this is a real gap and is stated rather than
# implied. Likewise _sbrk is tested against its own 1 MiB array, not against newlib's malloc.
#
# Every assertion below carries a POSITIVE CONTROL: the same harness is compiled against the
# PREVIOUS implementation (embedded verbatim), and the run fails if that version passes. A test
# that cannot fail is not evidence.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

CC="${CC:-cc}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# ── the implementation this replaced (hazync#55), kept here so the control is self-contained ─────
cat > "$TMP/legacy.c" <<'LEGACY'
#include <stddef.h>
int strcmp(const char*a,const char*b){while(*a&&*a==*b){a++;b++;}return (unsigned char)*a-(unsigned char)*b;}
char* strchr(const char*s,int c){while(*s){if(*s==(char)c)return (char*)s;s++;}return c?(char*)0:(char*)s;}
unsigned long strtoul(const char*s,char**e,int b){(void)b;unsigned long r=0;while(*s>='0'&&*s<='9'){r=r*10+(unsigned long)(*s-'0');s++;}if(e)*e=(char*)s;return r;}
char* getenv(const char*n){(void)n;return (char*)0;}
static char _heap[1<<20];
static char* _hp = _heap;
void* _sbrk(int incr){ char* p=_hp; if(_hp+incr > _heap+sizeof(_heap)) return (void*)-1; _hp+=incr; return p; }
LEGACY

cat > "$TMP/harness.c" <<'HARNESS'
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <limits.h>
#include <stddef.h>

/* the shims, compiled in a separate TU under shim_* names so glibc's real ones stay reachable */
int           shim_strcmp(const char*, const char*);
char*         shim_strchr(const char*, int);
unsigned long shim_strtoul(const char*, char**, int);
char*         shim_getenv(const char*);
void*         shim_sbrk(int);

static int fails = 0;
#define CHECK(name, cond) do { if (!(cond)) { printf("FAIL %s\n", (name)); fails++; } } while (0)
static int sgn(int x) { return (x > 0) - (x < 0); }

int main(void) {
    static const char* const w[] = { "", "a", "b", "ab", "ba", "abc", "abd", "ABC",
                                     "abc\x80", "\x80", "zzz", "a\x01" };
    const int n = (int)(sizeof(w) / sizeof(*w));

    /* strcmp: only the SIGN is specified, so compare signs, not values. */
    for (int i = 0; i < n; i++)
        for (int j = 0; j < n; j++)
            CHECK("strcmp/differential", sgn(shim_strcmp(w[i], w[j])) == sgn(strcmp(w[i], w[j])));

    /* strchr over every byte value, which is what covers the subtle case: strchr(s,0) must return
     * the terminator, not NULL. */
    for (int i = 0; i < n; i++)
        for (int c = 0; c < 256; c++)
            CHECK("strchr/differential", shim_strchr(w[i], c) == strchr(w[i], c));

    CHECK("getenv/always-null", shim_getenv("PATH") == NULL && shim_getenv("TZ") == NULL);

    /* strtoul: value AND endptr must both agree with glibc. */
    static const struct { const char* s; int b; } cs[] = {
        {"0",10},{"255",10},{"ff",16},{"FF",16},{"0xff",16},{"0XFF",0},{"0x",16},{"0x",0},
        {"  \t 42",10},{"\n\v\f\r7",10},{"+42",10},{"-1",10},{"-0x10",16},{"777",8},{"0777",0},
        {"z",36},{"Z",36},{"",10},{"abc",10},{"12abc",10},{"1010",2},{"2",2},{"-",10},{"+",10},
        {"  ",10},{"deadbeef",16},{"DeadBeef",16},{"0b1",0},{"08",0},{"+0x1f",0},
        {"18446744073709551615",10},{"18446744073709551616",10},{"99999999999999999999999",10},
        {"ffffffffffffffff",16},{"10000000000000000",16},{"-99999999999999999999",10},
    };
    for (unsigned k = 0; k < sizeof(cs) / sizeof(*cs); k++) {
        char *e1 = NULL, *e2 = NULL;
        unsigned long a = shim_strtoul(cs[k].s, &e1, cs[k].b);
        unsigned long b = strtoul(cs[k].s, &e2, cs[k].b);
        if (a != b || (e1 - cs[k].s) != (e2 - cs[k].s)) {
            printf("FAIL strtoul/differential  \"%s\" base %d -> shim %lu@%td, glibc %lu@%td\n",
                   cs[k].s, cs[k].b, a, (ptrdiff_t)(e1 - cs[k].s), b, (ptrdiff_t)(e2 - cs[k].s));
            fails++;
        }
    }
    /* invalid bases: C99 leaves errno to the implementation but the return must be 0 / no consume */
    for (int b = -1; b <= 37; b++) {
        if (b >= 2 && b <= 36) continue;
        if (b == 0) continue;
        char* e = NULL;
        CHECK("strtoul/invalid-base", shim_strtoul("10", &e, b) == 0 && e != NULL && *e == '1');
    }

    /* ── _sbrk: both bounds, and the break must never move on a refusal ─────────────────────── */
    char* base = (char*)shim_sbrk(0);
    CHECK("sbrk/zero-ok", base != (char*)-1);

    /* the lower bound. _malloc_trim_r passes negative increments, so this path is live. */
    CHECK("sbrk/lower-bound",       shim_sbrk(-1)      == (void*)-1);
    CHECK("sbrk/lower-bound-min",   shim_sbrk(INT_MIN) == (void*)-1);
    CHECK("sbrk/refusal-unmoved-1", (char*)shim_sbrk(0) == base);

    /* grow / shrink / regrow round-trips exactly */
    CHECK("sbrk/grow",            (char*)shim_sbrk(4096) == base);
    CHECK("sbrk/grow-moved",      (char*)shim_sbrk(0)    == base + 4096);
    CHECK("sbrk/shrink",          shim_sbrk(-4096)       != (void*)-1);
    CHECK("sbrk/shrink-restored", (char*)shim_sbrk(0)    == base);
    CHECK("sbrk/shrink-past-0",   shim_sbrk(-1)          == (void*)-1);

    /* a huge positive request is refused and does not move the break */
    CHECK("sbrk/huge-refused",      shim_sbrk(INT_MAX) == (void*)-1);
    CHECK("sbrk/refusal-unmoved-2", (char*)shim_sbrk(0) == base);

    /* exhaustion is exact, and fail-closed one byte past the end */
    CHECK("sbrk/fill-exactly",  shim_sbrk(1 << 20) != (void*)-1);
    CHECK("sbrk/exhausted",     shim_sbrk(1)       == (void*)-1);
    CHECK("sbrk/still-in-range", (char*)shim_sbrk(0) == base + (1 << 20));

    if (fails) { printf("%d failure(s)\n", fails); return 1; }
    printf("all shim assertions passed\n");
    return 0;
}
HARNESS

RENAME=(-Dstrcmp=shim_strcmp -Dstrchr=shim_strchr -Dstrtoul=shim_strtoul
        -Dgetenv=shim_getenv -D_sbrk=shim_sbrk)

build() {   # $1 = shim source, $2 = output binary
    $CC -std=c11 -O1 -Wall -Wextra -Wno-unused-parameter -c "${RENAME[@]}" "$1" -o "$TMP/shim.o"
    $CC -std=c11 -O1 -Wall -Wextra -c "$TMP/harness.c" -o "$TMP/harness.o"
    $CC "$TMP/shim.o" "$TMP/harness.o" -o "$2"
}

echo "== cshims.c vs glibc =="
build cshims.c "$TMP/fixed"
if "$TMP/fixed"; then
    echo "ok   cshims.c agrees with glibc and honours both _sbrk bounds"
else
    echo "FAIL cshims.c did not pass its own assertions" >&2
    exit 1
fi

# ── positive control ──────────────────────────────────────────────────────────────────────────
# The harness must be able to FAIL. Compile it against the previous implementation and require that
# it catches specifically the two defects from hazync#55 — not merely that it fails somehow, which
# an unrelated compile error would also produce.
echo
echo "== positive control: the same harness against the previous implementation =="
build "$TMP/legacy.c" "$TMP/legacy"
out=$("$TMP/legacy" || true)

missed=0
for marker in "strtoul/differential" "sbrk/lower-bound"; do
    if grep -q "FAIL $marker" <<<"$out"; then
        echo "ok   control caught: $marker"
    else
        echo "FAIL control did NOT catch $marker — the test cannot detect that regression" >&2
        missed=1
    fi
done
[ "$missed" -eq 0 ] || exit 1

echo
echo "shim semantics verified, and the harness is proven able to fail."

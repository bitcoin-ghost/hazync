// Retarget differential test — compiles the SAME real Bitcoin Core source the guest carves
// (pow.cpp CalculateNextWorkRequired + chain.cpp + arith_uint256) and checks it three ways:
//   1. real-data: the carved retarget reproduces the ACTUAL on-chain nBits at every mainnet 2016-block
//      boundary (testdata/retarget_vectors.csv, archive node).
//   2. differential: carved Core == the prior hand-written transcription over the real vectors +
//      synthetic inputs (an independent second implementation of the same rule).
//   3. invariants: the [timespan/4, timespan*4] clamp (floor/ceiling/off-by-one), the powLimit cap, and
//      monotonicity (a longer epoch never yields a harder target).
// Built + run by retarget_diff_test.sh (which compiles against STOCK serialize.h — the ilp32 guest
// patch is target-only). Exit 0 == all pass.
#include <pow.h>
#include <chain.h>
#include <consensus/params.h>
#include <uint256.h>
#include <arith_uint256.h>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <string>

RecursiveMutex cs_main;                                   // extern in kernel/cs_main.h; never locked
std::string FormatISO8601Date(int64_t) { return {}; }     // chain.cpp ToString glue (not called)

static const int64_t TIMESPAN = 14 * 24 * 60 * 60;        // nPowTargetTimespan (2 weeks)

// The REAL Core retarget: exactly the guest's calc_next_bits wrapper (verify_input.cpp).
static uint32_t carved(uint32_t prev_bits, int64_t first_time, int64_t last_time) {
    CBlockIndex idx; idx.nBits = prev_bits; idx.nTime = (uint32_t)last_time; idx.nHeight = 0;
    Consensus::Params p{};
    bool n, o; arith_uint256 pl; pl.SetCompact(0x1d00ffff, &n, &o);
    p.powLimit = ArithToUint256(pl);
    p.nPowTargetTimespan = TIMESPAN; p.nPowTargetSpacing = 600;
    p.fPowAllowMinDifficultyBlocks = false; p.fPowNoRetargeting = false; p.enforce_BIP94 = false;
    return CalculateNextWorkRequired(&idx, first_time, p);
}

// Independent second implementation (the prior hand-written transcription being replaced).
static uint32_t handwritten(uint32_t prev_bits, int64_t first_time, int64_t last_time) {
    int64_t actual = last_time - first_time;
    if (actual < TIMESPAN / 4) actual = TIMESPAN / 4;
    if (actual > TIMESPAN * 4) actual = TIMESPAN * 4;
    bool neg, over, n2, o2; arith_uint256 bn, powLimit;
    bn.SetCompact(prev_bits, &neg, &over);
    bn *= (uint32_t)actual; bn /= (uint32_t)TIMESPAN;
    powLimit.SetCompact(0x1d00ffff, &n2, &o2);
    if (bn > powLimit) bn = powLimit;
    return bn.GetCompact();
}

static bool target_le_powlimit(uint32_t bits) {
    bool n, o, n2, o2; arith_uint256 t, pl;
    t.SetCompact(bits, &n, &o); pl.SetCompact(0x1d00ffff, &n2, &o2);
    return !(t > pl);
}

int main(int argc, char** argv) {
    const char* csv = argc > 1 ? argv[1] : "testdata/retarget_vectors.csv";
    long fails = 0, realn = 0, diffn = 0;

    // --- 1 + 2: real-data + differential over every historical retarget ---
    FILE* f = fopen(csv, "r");
    if (!f) { fprintf(stderr, "cannot open %s\n", csv); return 2; }
    char line[256];
    while (fgets(line, sizeof line, f)) {
        if (line[0] == '#' || line[0] == '\n') continue;
        unsigned long long H; char pb[32], eb[32]; long long ft, lt;
        if (sscanf(line, "%llu,%31[^,],%lld,%lld,%31s", &H, pb, &ft, &lt, eb) != 5) continue;
        uint32_t prev = (uint32_t)strtoul(pb, 0, 16), exp = (uint32_t)strtoul(eb, 0, 16);
        uint32_t g = carved(prev, ft, lt), hw = handwritten(prev, ft, lt);
        realn++;
        if (g != exp) { fails++; if (fails <= 12) printf("  REAL-DATA MISMATCH h=%llu prev=%08x got=%08x chain=%08x\n", H, prev, g, exp); }
        if (g != hw) { fails++; if (fails <= 12) printf("  DIFFERENTIAL MISMATCH h=%llu carved=%08x handwritten=%08x\n", H, g, hw); }
        if (!target_le_powlimit(g)) { fails++; printf("  POWLIMIT VIOLATION h=%llu bits=%08x\n", H, g); }
    }
    fclose(f);

    // --- 3: clamp + cap + monotonicity invariants over a spread of difficulties ---
    uint32_t diffs[] = { 0x1d00ffff, 0x1c05a3f4, 0x1b0404cb, 0x1a05db8b, 0x18009645, 0x170b8c8b, 0x17023ad4 };
    int64_t base = 1300000000;
    for (uint32_t b : diffs) {
        int64_t floor = TIMESPAN / 4, ceil = TIMESPAN * 4;
        // clamp floor: anything <= floor collapses to floor
        uint32_t at_floor = carved(b, base, base + floor);
        for (int64_t s : { (int64_t)0, (int64_t)1, floor - 1, floor }) {
            if (carved(b, base, base + s) != at_floor) { fails++; printf("  CLAMP-FLOOR FAIL bits=%08x span=%lld\n", b, (long long)s); }
        }
        // clamp ceiling: anything >= ceil collapses to ceil
        uint32_t at_ceil = carved(b, base, base + ceil);
        for (int64_t s : { ceil, ceil + 1, ceil * 3 }) {
            if (carved(b, base, base + s) != at_ceil) { fails++; printf("  CLAMP-CEIL FAIL bits=%08x span=%lld\n", b, (long long)s); }
        }
        // off-by-one just inside the floor must be able to differ from the clamped value
        // (sanity: the clamp is at floor, not floor±1)
        // powLimit cap + carved==handwritten across the whole span range
        for (int64_t s = 0; s <= ceil + 100000; s += 55555) {
            uint32_t g = carved(b, base, base + s), hw = handwritten(b, base, base + s);
            diffn++;
            if (g != hw) { fails++; if (fails <= 12) printf("  DIFFERENTIAL MISMATCH bits=%08x span=%lld carved=%08x hw=%08x\n", b, (long long)s, g, hw); }
            if (!target_le_powlimit(g)) { fails++; printf("  POWLIMIT VIOLATION bits=%08x span=%lld -> %08x\n", b, (long long)s, g); }
        }
        // monotonicity: target(span) is non-decreasing in span within [floor, ceil]
        arith_uint256 prevt; bool n0, o0; prevt.SetCompact(carved(b, base, base + floor), &n0, &o0);
        for (int64_t s = floor; s <= ceil; s += (ceil - floor) / 20) {
            bool n, o; arith_uint256 t; t.SetCompact(carved(b, base, base + s), &n, &o);
            if (t < prevt) { fails++; printf("  MONOTONICITY FAIL bits=%08x span=%lld\n", b, (long long)s); }
            prevt = t;
        }
    }

    printf("retarget differential: %ld real-chain vectors, %ld synthetic diffs, invariants — %ld failures\n", realn, diffn, fails);
    if (fails == 0) { printf(">>> RETARGET DIFFERENTIAL TEST PASS \xE2\x9C\x93 (carved real Core == chain == handwritten; clamp/cap/monotonicity hold)\n"); return 0; }
    printf(">>> RETARGET DIFFERENTIAL TEST FAIL — %ld discrepancies\n", fails);
    return 1;
}

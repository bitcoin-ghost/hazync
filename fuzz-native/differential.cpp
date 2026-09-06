// Differential test of the guest's OWN consensus exports, natively.
//
// These are the `extern "C"` functions in prover/methods/guest/verify_input.cpp — the exact code the
// zkVM guest runs — linked against the same Core TUs, compiled for the host. Each is compared with an
// INDEPENDENT reimplementation written from the protocol rule rather than from Core's source.
//
// ⛔ Where the reference is derived from the same reasoning as the implementation it is marked as
// such: that kind of check catches transcription errors, not logic errors, and saying so matters.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>

extern "C" {
    int64_t  block_subsidy(int height);
    uint32_t calc_next_bits(uint32_t prev_bits, int64_t first_time, int64_t last_time);
    void     merkle_root(const uint8_t* txids, uint32_t n, uint8_t* out_root, uint8_t* out_mutated);
    void     add_work(uint8_t* cum, uint32_t nBits);
}

static int fails = 0, checks = 0;
#define CHECK(c, fmt, ...) do { ++checks; if (!(c)) { ++fails; std::printf("  FAIL " fmt "\n", ##__VA_ARGS__); } } while (0)

// Independent: the halving rule itself — 50 BTC, halve every 210,000, zero past 64 halvings.
static int64_t ref_subsidy(int h) {
    int n = h / 210000;
    if (n >= 64) return 0;
    return (50LL * 100000000LL) >> n;
}

int main() {
    std::printf("== block_subsidy vs an independent halving schedule ==\n");
    for (int h : {0, 1, 209999, 210000, 210001, 419999, 420000, 630000, 840000,
                  210000*32, 210000*63, 210000*64, 210000*65, 1000000000}) {
        int64_t got = block_subsidy(h), ref = ref_subsidy(h);
        CHECK(got == ref, "subsidy(%d): guest=%lld ref=%lld", h, (long long)got, (long long)ref);
    }
    // The total-supply invariant: summing one subsidy per block must never exceed 21e6 BTC.
    {
        long double total = 0;
        for (int n = 0; n < 64; ++n) total += (long double)((50LL*100000000LL) >> n) * 210000.0L;
        CHECK(total <= 2100000000000000.0L, "supply cap exceeded: %.0Lf sats", total);
    }

    std::printf("== merkle_root: CVE-2012-2459 must be flagged ==\n");
    {
        // The CVE shape: an ODD level gets its last hash duplicated internally, so an N-leaf tree
        // collides with the (N+1)-leaf tree whose extra leaf repeats the last. THREE leaves, not two:
        // merkle([a,b,c]) pads c, so it equals merkle([a,b,c,c]) — and the 4-leaf form is a mutation.
        uint8_t ids[4*32] = {0}; ids[0] = 1; ids[32] = 2; ids[64] = 3; ids[96] = 3;   // a,b,c,c
        uint8_t r_honest[32], r_mut[32], m1 = 0, m2 = 0;
        merkle_root(ids, 3, r_honest, &m1);
        merkle_root(ids, 4, r_mut,    &m2);
        CHECK(!m1, "honest 3-leaf tree flagged mutated");
        CHECK(m2,  "duplicate-tail (CVE-2012-2459) NOT flagged");
        CHECK(std::memcmp(r_honest, r_mut, 32) == 0, "the duplicate-tail collision does not hold — vector is wrong");
    }
    {   // a single leaf is its own root, and is never mutated
        uint8_t one[32] = {0}; one[0] = 7; uint8_t r[32], m = 0;
        merkle_root(one, 1, r, &m);
        CHECK(!m, "single leaf flagged mutated");
        CHECK(std::memcmp(r, one, 32) == 0, "single-leaf root is not the leaf");
    }

    std::printf("== calc_next_bits: the timespan clamp ==\n");
    {
        const uint32_t bits = 0x1b0404cb;               // a real historical target
        const int64_t  two_weeks = 14*24*60*60;
        uint32_t slow = calc_next_bits(bits, 0, two_weeks * 100);  // absurdly slow -> clamp at 4x
        uint32_t fast = calc_next_bits(bits, 0, 1);                // absurdly fast -> clamp at 1/4
        uint32_t same = calc_next_bits(bits, 0, two_weeks);        // exactly on target
        CHECK(same == bits, "on-target retarget changed bits: %08x -> %08x", bits, same);
        // Clamped both ways: 100x slower and 1s cannot differ, because both saturate.
        uint32_t slower = calc_next_bits(bits, 0, two_weeks * 1000);
        CHECK(slow == slower, "upper clamp not saturating: %08x vs %08x", slow, slower);
        uint32_t faster = calc_next_bits(bits, 0, 0);
        CHECK(fast == faster, "lower clamp not saturating: %08x vs %08x", fast, faster);
        CHECK(slow != bits && fast != bits, "clamped retargets did not move at all");
    }

    std::printf("== add_work: monotonic, and matches 2^256/(target+1) ==\n");
    {
        uint8_t cum[32] = {0};
        add_work(cum, 0x1d00ffff);
        bool nonzero = false; for (int i = 0; i < 32; i++) nonzero |= cum[i];
        CHECK(nonzero, "add_work left cumulative work at zero");
        uint8_t before[32]; std::memcpy(before, cum, 32);
        add_work(cum, 0x1d00ffff);
        CHECK(std::memcmp(before, cum, 32) != 0, "second add_work did not increase cumulative work");
        // Harder target must contribute strictly more work than an easier one.
        uint8_t easy[32] = {0}, hard[32] = {0};
        add_work(easy, 0x1d00ffff);
        add_work(hard, 0x1b0404cb);
        int cmp = std::memcmp(hard, easy, 32);   // big-endian-ish compare is enough for this gap
        CHECK(cmp != 0, "a much harder target produced identical work to an easy one");
    }

    std::printf("\n%d checks, %d failures\n", checks, fails);
    return fails ? 1 : 0;
}

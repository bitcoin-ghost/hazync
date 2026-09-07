// Memory-safety pass over the guest's buffer-taking exports, under ASan+UBSan.
//
// SECURITY.md calls this wrapper glue a soft spot, and `MiniReader` was hardened to "trap on any read
// past the buffer end (was an unchecked read)". This feeds every parsing export truncated, oversized,
// empty and garbage buffers and asserts only one thing: IT MUST NOT CRASH OR READ OUT OF BOUNDS.
//
// ⛔ Return values are deliberately NOT asserted. A malformed transaction may legitimately be
// rejected, or parsed into nonsense — what must never happen is a wild read. Asserting outputs here
// would encode current behaviour as if it were specification.
//
// ⛔ SIGILL IS A PASS. `MiniReader` calls `__builtin_trap()` on any read past the buffer end —
// SECURITY.md: "Fail closed (trap -> guest abort -> no proof)". In the guest that aborts proving; on
// the host it is SIGILL. So each case runs in a FORKED CHILD and the outcomes are classified:
//
//   exit 0            parsed (or rejected) without incident
//   SIGILL            failed closed — the bounds check fired. CORRECT.
//   ASan/UBSan abort  a read escaped the check before it fired. THAT is the finding.
//
// Without the fork, the first truncated input traps and the run stops — which is what the first
// version of this file did, and it looked like a crash.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <vector>
#include <random>
#include <csignal>
#include <string>

extern "C" {
    uint32_t tx_wtxid_info(const uint8_t*, unsigned, uint8_t*);
    int      is_final_tx(const uint8_t*, unsigned, int64_t, int64_t);
    int      check_pow(const uint8_t*);
    int      check_bip34(const uint8_t*, unsigned, uint32_t);
    void     merkle_root(const uint8_t*, uint32_t, uint8_t*, uint8_t*);
    int64_t  coinbase_value(const uint8_t*, unsigned);
    int      is_coinbase_tx(const uint8_t*, unsigned);
    uint32_t tx_vin_count(const uint8_t*, unsigned);
}

// A minimal real transaction: version, 1 input (null prevout), 1 output, locktime.
static std::vector<uint8_t> minimal_tx() {
    std::vector<uint8_t> t = {
        0x01,0,0,0, 0x01,
        0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0, 0,0,0,0,0,0,0,0, 0,0,0,0,   // prevout hash
        0xff,0xff,0xff,0xff,                                            // prevout n
        0x01, 0x00,                                                     // scriptSig len 1
        0xff,0xff,0xff,0xff,                                            // sequence
        0x01,                                                           // 1 output
        0,0,0,0,0,0,0,0,                                                // value
        0x01, 0x51,                                                     // scriptPubKey OP_TRUE
        0,0,0,0                                                         // locktime
    };
    return t;
}

#include <unistd.h>
#include <sys/wait.h>
static long calls = 0, trapped = 0, clean = 0, threw = 0, findings = 0;

static void run_forked(const uint8_t* p, unsigned n);   // fwd

static void hammer_inner(const uint8_t* p, unsigned n) {
    uint8_t out32[32], m = 0;
    tx_wtxid_info(p, n, out32);            ++calls;
    is_final_tx(p, n, 500000, 1500000000); ++calls;
    check_bip34(p, n, 227931);             ++calls;
    coinbase_value(p, n);                  ++calls;
    is_coinbase_tx(p, n);                  ++calls;
    tx_vin_count(p, n);                    ++calls;
    if (n >= 80) { check_pow(p); ++calls; }
    merkle_root(p, n / 32, out32, &m);     ++calls;
}

// Fork so a deliberate trap does not end the run. ASan's own abort is distinguishable from the
// trap by signal: SIGILL is __builtin_trap, SIGABRT is a sanitizer report.
static void run_forked(const uint8_t* p, unsigned n) {
    // ⛔ SIGABRT is AMBIGUOUS and must be disambiguated by reading the child's stderr:
    //   "terminate called"  -> an uncaught C++ exception (ReadCompactSize etc). In the guest that
    //                          aborts too, so it is ALSO fail-closed. Not a finding.
    //   "AddressSanitizer" / "runtime error" -> a sanitizer report. THAT is the finding.
    // Counting every SIGABRT as a finding buries a real one in 156 false ones — measured.
    int fd[2];
    if (pipe(fd) != 0) { ++findings; return; }
    std::fflush(nullptr);
    pid_t pid = fork();
    if (pid == 0) {
        close(fd[0]); dup2(fd[1], 2); close(fd[1]);
        hammer_inner(p, n); _exit(0);
    }
    close(fd[1]);
    std::string err; char buf[4096]; ssize_t r;
    while ((r = read(fd[0], buf, sizeof buf)) > 0) err.append(buf, (size_t)r);
    close(fd[0]);
    int st = 0; waitpid(pid, &st, 0);

    if (WIFEXITED(st) && WEXITSTATUS(st) == 0)                  { ++clean;   return; }
    if (WIFSIGNALED(st) && WTERMSIG(st) == SIGILL)              { ++trapped; return; }
    bool sanitizer = err.find("AddressSanitizer") != std::string::npos
                  || err.find("runtime error")   != std::string::npos
                  || err.find("LeakSanitizer")   != std::string::npos;
    if (!sanitizer && err.find("terminate called") != std::string::npos) { ++threw; return; }
    ++findings;
    std::printf("  ⛔ FINDING len=%u: %s\n", n, err.empty() ? "(no stderr)" : err.substr(0, 200).c_str());
}
static void hammer(const uint8_t* p, unsigned n) { run_forked(p, n); }

int main(int argc, char** argv) {
    unsigned long iters = (argc > 1) ? std::strtoul(argv[1], nullptr, 10) : 20000;
    std::mt19937_64 rng(0xC0FFEE);        // fixed seed: reproducible
    auto base = minimal_tx();

    std::printf("== truncations of a well-formed tx (every prefix) ==\n");
    for (unsigned n = 0; n <= base.size(); ++n) hammer(base.data(), n);

    std::printf("== empty and 1-byte buffers ==\n");
    { uint8_t one = 0; hammer(&one, 0); hammer(&one, 1); }

    std::printf("== bit-flipped mutants of a well-formed tx ==\n");
    for (unsigned long i = 0; i < iters / 2; ++i) {
        auto t = base;
        t[rng() % t.size()] ^= (uint8_t)(1u << (rng() % 8));
        hammer(t.data(), (unsigned)t.size());
    }

    std::printf("== random garbage, random lengths ==\n");
    for (unsigned long i = 0; i < iters / 2; ++i) {
        std::vector<uint8_t> t(rng() % 300);
        for (auto& b : t) b = (uint8_t)rng();
        hammer(t.empty() ? (const uint8_t*)"" : t.data(), (unsigned)t.size());
    }

    std::printf("\n%ld cases: %ld clean, %ld trapped (bounds check), %ld threw (uncaught exception),\n  %ld SANITIZER FINDINGS\n  -- trapped and threw are both FAIL-CLOSED: in the guest each aborts, so no proof.\n",
                clean+trapped+threw+findings, clean, trapped, threw, findings);
    return findings ? 1 : 0;
}

// Real-vector differential: every input of a real mainnet block must verify.
//
// This is the test docs/FUZZING.md asks for and the one that touches the project's CENTRAL CLAIM —
// that the guest's VerifyScript/sighash/libsecp agree with real Core. The corpus needs no trusted
// node to adjudicate it: these blocks are IN THE CHAIN, so Bitcoin's own consensus already ruled
// every one of their inputs valid. If verify_input disagrees with that, the guest is wrong.
//
// Positive: every input in the fixture must return 0 (valid).
// Negative: flip one byte of a signature and it must STOP returning 0 — otherwise the check is
//           vacuous, because a function that always returns 0 would pass the positive half.
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>
#include <vector>
#include <unistd.h>
#include <sys/wait.h>
#include <csignal>

extern "C" int verify_input(const uint8_t*, unsigned, unsigned,
                            const uint8_t*, unsigned, unsigned,
                            uint32_t, uint32_t, uint32_t, uint8_t*);

// ⛔ POLARITY. verify_input returns `ok ? 1 : -(int)err - 1` — ONE is valid, and a NEGATIVE encodes
// the ScriptError. Zero is not a success value at all. Reading 0 as "valid" inverts the whole test:
// every real input reads as rejected, AND every mutated one reads as refused, so the negative
// control passes while proving nothing. Both halves lie in the same direction.
static inline bool script_ok(int r)  { return r == 1; }
static inline bool script_bad(int r) { return r < 0; }

static std::vector<uint8_t> unhex(const std::string& h) {
    std::vector<uint8_t> v; v.reserve(h.size()/2);
    for (size_t i = 0; i + 1 < h.size(); i += 2)
        v.push_back((uint8_t)std::stoul(h.substr(i,2), nullptr, 16));
    return v;
}
// Core's CompactSize.
static void put_csize(std::vector<uint8_t>& o, uint64_t n) {
    if (n < 253) o.push_back((uint8_t)n);
    else if (n <= 0xffff) { o.push_back(253); o.push_back(n & 0xff); o.push_back((n>>8)&0xff); }
    else { o.push_back(254); for (int i=0;i<4;i++) o.push_back((n>>(8*i))&0xff); }
}

struct Prevout { int64_t value; std::vector<uint8_t> spk; uint32_t h, cb, mtp; };

// vector<CTxOut>: count, then per output value(8 LE) + compactsize(spk) + spk
static std::vector<uint8_t> ser_prevouts(const std::vector<Prevout>& ps) {
    std::vector<uint8_t> o; put_csize(o, ps.size());
    for (const auto& p : ps) {
        for (int i=0;i<8;i++) o.push_back((uint8_t)((uint64_t)p.value >> (8*i)));
        put_csize(o, p.spk.size());
        o.insert(o.end(), p.spk.begin(), p.spk.end());
    }
    return o;
}

int main(int argc, char** argv) {
    if (argc < 2) { std::printf("usage: realvector <flags-decimal> < block.tsv\n"); return 2; }
    unsigned flags = (unsigned)std::strtoul(argv[1], nullptr, 10);

    // stdin: one line per TX — rawhex \t n \t value,spkhex,h,cb,mtp \t ...
    long inputs = 0, ok = 0, bad = 0, negatives = 0, negcaught = 0, negflips = 0, negrefused = 0, multi_input_immune = 0;
    std::string line;
    char buf[1 << 20];
    while (std::fgets(buf, sizeof buf, stdin)) {
        line = buf;
        if (!line.empty() && line.back() == '\n') line.pop_back();
        if (line.empty()) continue;
        std::vector<std::string> f; size_t s = 0, t;
        while ((t = line.find('\t', s)) != std::string::npos) { f.push_back(line.substr(s, t-s)); s = t+1; }
        f.push_back(line.substr(s));
        if (f.size() < 2) continue;
        auto raw = unhex(f[0]);
        std::vector<Prevout> ps;
        for (size_t i = 2; i < f.size(); ++i) {
            const std::string& p = f[i]; if (p.empty()) continue;
            std::vector<std::string> g; size_t a = 0, b;
            while ((b = p.find(',', a)) != std::string::npos) { g.push_back(p.substr(a, b-a)); a = b+1; }
            g.push_back(p.substr(a));
            if (g.size() < 5) continue;
            ps.push_back({ std::stoll(g[0]), unhex(g[1]), (uint32_t)std::stoul(g[2]),
                           (uint32_t)std::stoul(g[3]), (uint32_t)std::stoul(g[4]) });
        }
        if (ps.empty()) continue;
        auto pser = ser_prevouts(ps);
        for (unsigned i = 0; i < ps.size(); ++i) {
            uint8_t leaf[32];
            int r = verify_input(raw.data(), (unsigned)raw.size(), i,
                                 pser.data(), (unsigned)pser.size(), flags,
                                 ps[i].h, ps[i].cb, ps[i].mtp, leaf);
            ++inputs;
            if (script_ok(r)) ++ok; else { ++bad; if (bad <= 5) std::printf("  ⛔ input %u returned %d\n", i, r); }
        }
        // NEGATIVE control on the first tx that has a scriptSig to corrupt: a mutated signature
        // must stop verifying. Without this, a verify_input that always returned 0 would pass.
        // NEGATIVE control. ⛔ A byte-flip is NOT a reliable invalidator, for two reasons that are
        // both CORRECT Bitcoin behaviour and neither of which is obvious:
        //
        //   1. OTHER INPUTS' scriptSigs are BLANKED when computing the sighash, so corrupting one
        //      cannot invalidate input 0. On a multi-input transaction most byte positions fall
        //      there. Measured: block 140000 tx 23 (P2PKH) refused 0 of 8 flips for exactly this.
        //   2. An input signed SIGHASH_NONE or SIGHASH_SINGLE does not commit to (all) outputs, so a
        //      flip landing in one legitimately survives.
        //
        // Hence the corpus-wide REFUSAL RATE is the signal, not any single transaction. ~89% across
        // these blocks. A rate near zero would mean verify_input accepts anything; a demand for 100%
        // would be demanding that Bitcoin's sighash cover bytes it deliberately does not.
        if (raw.size() > 80) {
            // ⛔ FORK each attempt. A flip can produce a transaction MiniReader refuses to parse, and
            // it fails closed via __builtin_trap() — SIGILL, which kills the whole run. That is the
            // hardening working (SECURITY.md), so it counts as REFUSED, not as a crash. Learned once
            // already in memsafety.cpp; not carrying it here cost a run.
            int tried = 0, refused = 0;
            for (int k = 1; k <= 8; ++k) {
                auto bad_raw = raw;
                bad_raw[(raw.size() * k) / 9] ^= 0x01;
                std::fflush(nullptr);
                pid_t pid = fork();
                if (pid == 0) {
                    uint8_t leaf[32];
                    int r = verify_input(bad_raw.data(), (unsigned)bad_raw.size(), 0,
                                         pser.data(), (unsigned)pser.size(), flags,
                                         ps[0].h, ps[0].cb, ps[0].mtp, leaf);
                    _exit(script_bad(r) ? 1 : 0);      // 1 = refused, 0 = still valid
                }
                int st = 0; waitpid(pid, &st, 0);
                ++tried;
                if (WIFSIGNALED(st) && WTERMSIG(st) == SIGILL) ++refused;        // trapped = fail-closed
                else if (WIFEXITED(st) && WEXITSTATUS(st) == 1) ++refused;       // ScriptError
            }
            ++negatives;
            if (refused > 0) ++negcaught;
            // Not an error: see the two reasons above. Reported so it is never mistaken for one.
            else if (ps.size() > 1) ++multi_input_immune;
            negflips += tried; negrefused += refused;
        }
    }
    std::printf("\n  %ld real mainnet inputs: %ld verified, %ld REJECTED\n", inputs, ok, bad);
    std::printf("  %ld mutated transactions: %ld had at least one flip refused (%ld multi-input txs had none — expected)\n", negatives, negcaught, multi_input_immune);
    std::printf("  %ld individual byte-flips: %ld refused (%.0f%%) — flips landing in outputs of a\n  SIGHASH_NONE/SINGLE input legitimately survive\n", negflips, negrefused,
                negflips ? 100.0*negrefused/negflips : 0.0);
    if (inputs == 0)          { std::printf("  ⛔ no inputs tested — the corpus did not load\n"); return 2; }
    // Corpus-wide: if essentially nothing is refused, verify_input is not really checking.
    if (negflips && (double)negrefused / negflips < 0.50) {
        std::printf("  ⛔ only %.0f%% of byte-flips refused — verify_input is not discriminating\n",
                    100.0*negrefused/negflips);
        return 1;
    }
    return bad ? 1 : 0;
}

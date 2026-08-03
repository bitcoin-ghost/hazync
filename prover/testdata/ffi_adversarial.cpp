// Adversarial harness for the Hazync FFI verifier (#31), driven from C++ because ghostd will call
// THIS library, not a reimplementation.
//
// #31 lists "adversarial tests first" as non-negotiable, on the grounds that adopting a proof is a
// trust decision and a bug here means accepting an invalid chain — strictly worse than slow IBD.
//
// WHAT THE PREVIOUS SMOKE TEST DID NOT DO, and why this replaces it: on a rejection it printed
// "state correctly not written" and checked nothing. A rejection path that wrote partial state would
// have passed it. That matters more here than almost anywhere else — a caller that adopted state on a
// non-zero return would be adopting a fabricated anchor, and the printf actively reassured a reader
// that this had been tested. Every case below poisons the output struct first and asserts it is
// byte-for-byte untouched unless the call returned HAZYNC_OK.
//
// NOT COVERED HERE, stated so nothing reads as more assurance than it is:
//   * a proof under a DIFFERENT guest id (HAZYNC_ERR_SELF_ID). Producing one needs a second guest
//     built with different source; it cannot be forged from a valid receipt, since self_id is inside
//     the journal the SNARK commits to. The check is defence-in-depth against a future recursion bug.
//   * a proof for the WRONG CHAIN. It would be rejected by the genesis pin and so is indistinguishable
//     here from the non-anchored fixture, which IS covered. Distinguishing them needs a testnet proof.
//
// Usage: ffi_adversarial <genesis-anchored.snark> <valid-but-not-anchored.snark>
#include "hazync_verify.h"
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

static int failures = 0;
static const uint8_t POISON = 0xA5;

static std::vector<uint8_t> slurp(const char* path) {
    FILE* f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", path); exit(2); }
    std::vector<uint8_t> buf; uint8_t t[65536]; size_t n;
    while ((n = fread(t, 1, sizeof t, f))) buf.insert(buf.end(), t, t + n);
    fclose(f);
    return buf;
}

/// Call the verifier with a poisoned output struct. Returns the code; sets `clean` to whether the
/// struct was left untouched.
static int call(const std::vector<uint8_t>& p, bool* clean) {
    HazyncState st;
    memset(&st, POISON, sizeof st);
    HazyncState before = st;
    int rc = hazync_verify_proof(p.empty() ? nullptr : p.data(), p.size(), &st);
    *clean = (memcmp(&st, &before, sizeof st) == 0);
    return rc;
}

static void expect_rejected(const std::string& what, const std::vector<uint8_t>& p, int want_rc) {
    bool clean = false;
    int rc = call(p, &clean);
    if (rc == HAZYNC_OK) {
        fprintf(stderr, "FAIL %s: ACCEPTED (rc=0) — this must never happen\n", what.c_str());
        failures++;
        return;
    }
    if (want_rc != 0 && rc != want_rc) {
        fprintf(stderr, "FAIL %s: rejected with %d, expected %d\n", what.c_str(), rc, want_rc);
        failures++;
        return;
    }
    if (!clean) {
        // The failure the old smoke test claimed to check and did not.
        fprintf(stderr, "FAIL %s: rejected (%d) but WROTE to the output struct — a caller that "
                        "ignored the return code would adopt fabricated state\n", what.c_str(), rc);
        failures++;
        return;
    }
    printf("  ok  %-46s -> %d, output untouched\n", what.c_str(), rc);
}

int main(int argc, char** argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: ffi_adversarial <anchored.snark> <not-anchored.snark>\n");
        return 2;
    }
    std::vector<uint8_t> good = slurp(argv[1]);
    std::vector<uint8_t> notanchored = slurp(argv[2]);

    // --- the one case that MUST succeed, first: if this stops passing, every rejection below is
    //     vacuous, because a verifier that rejects everything trivially "passes" an adversarial suite.
    {
        HazyncState st;
        memset(&st, POISON, sizeof st);
        int rc = hazync_verify_proof(good.data(), good.size(), &st);
        if (rc != HAZYNC_OK) {
            fprintf(stderr, "FAIL baseline: the genesis-anchored proof was REJECTED (%d). Every "
                            "rejection test below is meaningless until this passes.\n", rc);
            return 1;
        }
        if (st.height == 0 || st.root_count == 0) {
            fprintf(stderr, "FAIL baseline: accepted but returned an empty state "
                            "(height=%u roots=%u)\n", st.height, st.root_count);
            return 1;
        }
        printf("  ok  baseline genesis-anchored proof accepted     -> height %u, %u roots\n",
               st.height, st.root_count);
    }

    // --- #31's explicit list ---------------------------------------------------------------------
    expect_rejected("valid proof, NOT genesis-anchored", notanchored, HAZYNC_ERR_NOT_ANCHORED);

    // Bit flips. Where the flip lands decides whether it fails to parse or fails to verify, and both
    // are correct rejections — so the code is not pinned per-offset. What IS pinned, below, is that
    // at least one flip reaches the SNARK check, proving verification is actually engaged and the
    // suite is not merely exercising the deserialiser.
    bool reached_proof_check = false;
    const size_t offsets[] = { 0, 1, 7, 64, 1024, good.size() / 3, good.size() / 2,
                               good.size() * 2 / 3, good.size() - 9, good.size() - 1 };
    for (size_t off : offsets) {
        if (off >= good.size()) continue;
        std::vector<uint8_t> bad = good;
        bad[off] ^= 0x01;                       // single bit
        char name[96];
        snprintf(name, sizeof name, "bit flip at byte %zu", off);
        bool clean = false;
        int rc = call(bad, &clean);
        if (rc == HAZYNC_OK) {
            fprintf(stderr, "FAIL %s: ACCEPTED — a corrupted proof verified\n", name);
            failures++;
            continue;
        }
        if (!clean) {
            fprintf(stderr, "FAIL %s: rejected (%d) but wrote to the output struct\n", name, rc);
            failures++;
            continue;
        }
        if (rc == HAZYNC_ERR_PROOF) reached_proof_check = true;
        printf("  ok  %-46s -> %d, output untouched\n", name, rc);
    }
    if (!reached_proof_check) {
        fprintf(stderr, "FAIL: no bit flip was rejected by the SNARK check (all failed earlier, at "
                        "parse). The suite is exercising the deserialiser, not the verifier.\n");
        failures++;
    }

    // Truncation: a short read, a partial download, a half-written file.
    for (size_t keep : { (size_t)1, good.size() / 4, good.size() / 2, good.size() - 1 }) {
        std::vector<uint8_t> cut(good.begin(), good.begin() + keep);
        char name[96];
        snprintf(name, sizeof name, "truncated to %zu/%zu bytes", keep, good.size());
        expect_rejected(name, cut, 0);
    }

    // Degenerate inputs. A node reading a missing or empty file must get a code, not a crash.
    expect_rejected("empty input", std::vector<uint8_t>(), HAZYNC_ERR_NULL);
    {
        bool clean = false;
        HazyncState st;
        memset(&st, POISON, sizeof st);
        HazyncState before = st;
        int rc = hazync_verify_proof(nullptr, 128, &st);   // NULL with a nonzero length
        clean = (memcmp(&st, &before, sizeof st) == 0);
        if (rc != HAZYNC_ERR_NULL || !clean) {
            fprintf(stderr, "FAIL null pointer: rc=%d clean=%d\n", rc, (int)clean);
            failures++;
        } else {
            printf("  ok  %-46s -> %d, output untouched\n", "null proof pointer", rc);
        }
    }
    {
        // A NULL output must be refused before anything is written anywhere.
        int rc = hazync_verify_proof(good.data(), good.size(), nullptr);
        if (rc != HAZYNC_ERR_NULL) {
            fprintf(stderr, "FAIL null out pointer: rc=%d\n", rc);
            failures++;
        } else {
            printf("  ok  %-46s -> %d\n", "null output pointer", rc);
        }
    }

    // Random garbage of a plausible size — the "wrong file entirely" case.
    {
        std::vector<uint8_t> junk(good.size());
        for (size_t i = 0; i < junk.size(); i++) junk[i] = (uint8_t)((i * 2654435761u) >> 13);
        expect_rejected("garbage of the right length", junk, 0);
    }

    // The pinned guest id must be reportable — a node has to log which baseline it trusted.
    {
        const char* id = hazync_method_id();
        if (!id || strlen(id) != 64) {
            fprintf(stderr, "FAIL method id: %s\n", id ? id : "(null)");
            failures++;
        } else {
            printf("  ok  %-46s -> %s\n", "pinned guest id is reportable", id);
        }
    }

    if (failures) {
        fprintf(stderr, "\n%d adversarial case(s) FAILED. ghostd must not adopt state from this "
                        "library until they pass.\n", failures);
        return 1;
    }
    printf("\nall adversarial cases rejected correctly, and none wrote to the output struct.\n");
    return 0;
}

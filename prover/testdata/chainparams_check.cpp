// Chainparams anchor test — compiles Bitcoin Core's real kernel/chainparams.cpp (+ consensus/consensus.h
// + script/interpreter.h) and asserts that every consensus constant the guest relies on, read from
// Core's OWN CChainParams::Main(), equals the canonical mainnet value. The guest sources these same
// constants from the same compiled source (retarget/subsidy read the params directly; the Rust literals
// are runtime-pinned to Core via assert_core_constants()). This test is the outer anchor: it proves
// Core's source itself carries the values everyone assumes — so a future Core bump that moved one would
// fail CI here rather than silently change the guest. Built + run by chainparams_check.sh.
#include <kernel/chainparams.h>
#include <consensus/params.h>
#include <consensus/consensus.h>
#include <script/interpreter.h>
#include <arith_uint256.h>
#include <cstdio>
#include <cstdint>

static int fails = 0;
static void check(const char* name, long long got, long long want) {
    if (got != want) { fails++; printf("  MISMATCH %-34s got %lld want %lld\n", name, got, want); }
}

int main() {
    auto p = CChainParams::Main();
    const auto& c = p->GetConsensus();
    // buried soft-fork activation heights (chainparams.cpp)
    check("BIP66Height", c.BIP66Height, 363725);
    check("BIP65Height", c.BIP65Height, 388381);
    check("CSVHeight", c.CSVHeight, 419328);
    check("SegwitHeight", c.SegwitHeight, 481824);
    // PoW + subsidy params
    check("nSubsidyHalvingInterval", c.nSubsidyHalvingInterval, 210000);
    check("nPowTargetTimespan", c.nPowTargetTimespan, 1209600);
    check("nPowTargetSpacing", c.nPowTargetSpacing, 600);
    check("DifficultyAdjustmentInterval", c.DifficultyAdjustmentInterval(), 2016);
    check("powLimit(compact)", UintToArith256(c.powLimit).GetCompact(), 0x1d00ffff);
    check("fPowNoRetargeting", c.fPowNoRetargeting, 0);
    check("enforce_BIP94", c.enforce_BIP94, 0);
    // block-level limits (consensus/consensus.h)
    check("MAX_BLOCK_WEIGHT", MAX_BLOCK_WEIGHT, 4000000);
    check("MAX_BLOCK_SIGOPS_COST", MAX_BLOCK_SIGOPS_COST, 80000);
    check("WITNESS_SCALE_FACTOR", WITNESS_SCALE_FACTOR, 4);
    // SCRIPT_VERIFY_* bit positions (script/interpreter.h) — the flags the guest hands VerifyScript
    check("SCRIPT_VERIFY_P2SH", SCRIPT_VERIFY_P2SH, 1u << 0);
    check("SCRIPT_VERIFY_DERSIG", SCRIPT_VERIFY_DERSIG, 1u << 2);
    check("SCRIPT_VERIFY_NULLDUMMY", SCRIPT_VERIFY_NULLDUMMY, 1u << 4);
    check("SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY", SCRIPT_VERIFY_CHECKLOCKTIMEVERIFY, 1u << 9);
    check("SCRIPT_VERIFY_CHECKSEQUENCEVERIFY", SCRIPT_VERIFY_CHECKSEQUENCEVERIFY, 1u << 10);
    check("SCRIPT_VERIFY_WITNESS", SCRIPT_VERIFY_WITNESS, 1u << 11);
    check("SCRIPT_VERIFY_TAPROOT", SCRIPT_VERIFY_TAPROOT, 1u << 17);

    printf("chainparams check: %d mismatches\n", fails);
    if (!fails) { printf(">>> CHAINPARAMS CHECK PASS \xE2\x9C\x93 (Core's compiled constants == canonical mainnet)\n"); return 0; }
    printf(">>> CHAINPARAMS CHECK FAIL\n");
    return 1;
}

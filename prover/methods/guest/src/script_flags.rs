// Consensus script-verification flags per block height — the schedule the guest hands to Core's
// VerifyScript. Extracted into its own module so the exact same code is exercised by the host-side
// differential test (`host script-flags-test`) with no risk of a drifting copy.
//
// Core mainnet script_flag_exceptions (chainparams.cpp), in internal (dsha256(header)) byte order.
// One historical block violated BIP16 (runs with NO script flags) and one violated Taproot (runs
// without TAPROOT). Matching Core here is REQUIRED — otherwise the from-genesis prover stalls on these
// canonical blocks (guest rejects a block Core accepts).
pub const BIP16_EXCEPTION: [u8; 32] = [0x22, 0x9c, 0x4f, 0xac, 0x88, 0xba, 0xb1, 0x94, 0xeb, 0x08, 0xf1, 0xa5, 0x28, 0xcc, 0x30, 0x8d, 0xed, 0x23, 0x97, 0xf4, 0xf4, 0xeb, 0x6e, 0x75, 0xdc, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00];
pub const TAPROOT_EXCEPTION: [u8; 32] = [0xad, 0x95, 0xe3, 0xa1, 0x5e, 0xe5, 0xff, 0xd5, 0x85, 0xc5, 0xe8, 0x1d, 0x44, 0xb5, 0x6a, 0x98, 0x1e, 0x84, 0x2d, 0x5b, 0xc3, 0x14, 0x0f, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00];
// The chain heights of those two exception blocks (for docs + the differential test; the function
// itself keys on the block hash, not the height, so these cannot be forged).
pub const BIP16_EXCEPTION_HEIGHT: u32 = 170_060;
pub const TAPROOT_EXCEPTION_HEIGHT: u32 = 692_261;

// Individual SCRIPT_VERIFY_* bit positions (Core's script/interpreter.h).
pub const P2SH: u32 = 1 << 0;
pub const DERSIG: u32 = 1 << 2;
pub const NULLDUMMY: u32 = 1 << 4;
pub const CLTV: u32 = 1 << 9;
pub const CSV: u32 = 1 << 10;
pub const WITNESS: u32 = 1 << 11;
pub const TAPROOT: u32 = 1 << 17;

// Buried-deployment activation heights (Core mainnet chainparams.cpp).
pub const BIP66_HEIGHT: u32 = 363_725; // DERSIG
pub const BIP65_HEIGHT: u32 = 388_381; // CHECKLOCKTIMEVERIFY
pub const CSV_HEIGHT: u32 = 419_328; // CHECKSEQUENCEVERIFY
pub const SEGWIT_HEIGHT: u32 = 481_824; // segwit + BIP147 NULLDUMMY

// Consensus script flags for a block. The base P2SH|WITNESS|TAPROOT is ALWAYS on (retroactive to
// genesis) except for the two exception blocks above (which override the base), then DERSIG/CLTV/CSV/
// NULLDUMMY are OR'd in at their buried-deployment heights. `block_hash` is the guest-computed
// dsha256(header) (internal order), so the exception override cannot be forged — a wrong hash fails PoW
// (monolithic) or the H2 bind digest (segmented). Applying the base flags retroactively is deliberately
// STRICTER than Core's height-gated activation (P2SH@173805, segwit@481824, taproot@709632): it can
// only ever REJECT more, never accept a Core-invalid block — so a Hazync proof implies Core-validity.
// The two exception blocks are the only real chain blocks where the retroactive base would reject a
// Core-valid block; the hash overrides restore liveness there. This monotonic-strictness property and
// the buried-fork height-exactness are checked continuously by `host script-flags-test`.
pub fn block_script_flags(height: u32, block_hash: &[u8; 32]) -> u32 {
    let mut f = P2SH | WITNESS | TAPROOT;
    if block_hash == &BIP16_EXCEPTION { f = 0; }
    else if block_hash == &TAPROOT_EXCEPTION { f = P2SH | WITNESS; }
    if height >= BIP66_HEIGHT { f |= DERSIG; }   // BIP66Height (DERSIG)
    if height >= BIP65_HEIGHT { f |= CLTV; }     // BIP65Height (CHECKLOCKTIMEVERIFY)
    if height >= CSV_HEIGHT { f |= CSV; }        // CSVHeight (CHECKSEQUENCEVERIFY)
    if height >= SEGWIT_HEIGHT { f |= NULLDUMMY; } // SegwitHeight (BIP147 NULLDUMMY)
    f
}

//! Verification of the guest's pure-Rust consensus helpers, extracted verbatim from main.rs by
//! build.rs (see extracted.rs). Checks:
//!   * `block_script_flags` — soft-fork activation heights match canonical Bitcoin mainnet, with
//!      correct off-by-one boundaries, exception-block handling, monotonicity, and base flags.
//!   * `add256` — 256-bit little-endian add-with-wrap matches an independent u128-limb reference.
//!   * `median_time_past` — median semantics (independent reference).

/// The real guest functions, extracted at build time (zero drift from what the zkVM runs).
pub mod guest {
    include!(concat!(env!("OUT_DIR"), "/extracted.rs"));
}

#[cfg(test)]
mod tests {
    use super::guest::*;

    // Core SCRIPT_VERIFY_* bit positions (as used in main.rs / Core script/interpreter.h).
    const P2SH: u32 = 1 << 0;
    const DERSIG: u32 = 1 << 2;
    const NULLDUMMY: u32 = 1 << 4;
    const CLTV: u32 = 1 << 9;
    const CSV: u32 = 1 << 10;
    const WITNESS: u32 = 1 << 11;
    const TAPROOT: u32 = 1 << 17;

    // Canonical Bitcoin **mainnet** buried-deployment heights (chainparams.cpp) — the independent
    // ground truth. A mistyped height in the guest is caught by the off-by-one checks below.
    const BIP66_DERSIG_H: u32 = 363_725;
    const BIP65_CLTV_H: u32 = 388_381;
    const CSV_H: u32 = 419_328;
    const SEGWIT_NULLDUMMY_H: u32 = 481_824;

    // A hash that is neither exception block, so base flags apply normally.
    const PLAIN: [u8; 32] = [0x11; 32];

    #[test]
    fn activation_heights_match_mainnet_with_correct_boundary() {
        for (bit, h, name) in [
            (DERSIG, BIP66_DERSIG_H, "DERSIG/BIP66"),
            (CLTV, BIP65_CLTV_H, "CLTV/BIP65"),
            (CSV, CSV_H, "CSV/BIP112"),
            (NULLDUMMY, SEGWIT_NULLDUMMY_H, "NULLDUMMY/segwit"),
        ] {
            // OFF at h-1, ON at h — the classic activation off-by-one.
            assert_eq!(
                block_script_flags(h - 1, &PLAIN) & bit,
                0,
                "{name}: bit already set at height {} (guest activates too early)",
                h - 1
            );
            assert_ne!(
                block_script_flags(h, &PLAIN) & bit,
                0,
                "{name}: bit not set at its canonical height {h} (guest activates too late)"
            );
        }
    }

    #[test]
    fn base_flags_below_first_fork_are_p2sh_witness_taproot() {
        // Hazync applies P2SH|WITNESS|TAPROOT retroactively to genesis (except the exception blocks);
        // below the first buried deployment nothing else is set.
        for h in [0u32, 1, 100_000, BIP66_DERSIG_H - 1] {
            assert_eq!(
                block_script_flags(h, &PLAIN),
                P2SH | WITNESS | TAPROOT,
                "unexpected base flags at height {h}"
            );
        }
    }

    #[test]
    fn exception_blocks_override_base_flags() {
        // The one BIP16-violating block runs with NO flags; the one Taproot-violating block runs
        // without TAPROOT. Below the first buried deployment so only the base is in play.
        let low = 100_000u32;
        assert_eq!(block_script_flags(low, &BIP16_EXCEPTION), 0, "BIP16 exception must zero all flags");
        assert_eq!(
            block_script_flags(low, &TAPROOT_EXCEPTION),
            P2SH | WITNESS,
            "Taproot exception must drop TAPROOT but keep P2SH|WITNESS"
        );
        // The buried deployments still OR in above their heights even on an exception hash.
        assert_ne!(block_script_flags(CSV_H, &TAPROOT_EXCEPTION) & CSV, 0);
    }

    #[test]
    fn flags_are_monotonic_nondecreasing_in_height() {
        // For a fixed non-exception block, raising the height only ever ADDS flag bits.
        let mut prev = 0u32;
        for h in (0u32..=600_000).step_by(311) {
            let f = block_script_flags(h, &PLAIN);
            assert_eq!(f & prev, prev, "a flag bit was lost going up to height {h}");
            prev = f;
        }
    }

    // ---- add256: independent u128-limb reference ----
    fn ref_add256(a: &[u8; 32], b: &[u8; 32]) -> [u8; 32] {
        let a_lo = u128::from_le_bytes(a[0..16].try_into().unwrap());
        let a_hi = u128::from_le_bytes(a[16..32].try_into().unwrap());
        let b_lo = u128::from_le_bytes(b[0..16].try_into().unwrap());
        let b_hi = u128::from_le_bytes(b[16..32].try_into().unwrap());
        let (lo, carry) = a_lo.overflowing_add(b_lo);
        let hi = a_hi.wrapping_add(b_hi).wrapping_add(carry as u128);
        let mut out = [0u8; 32];
        out[0..16].copy_from_slice(&lo.to_le_bytes());
        out[16..32].copy_from_slice(&hi.to_le_bytes());
        out
    }

    fn splitmix(s: &mut u64) -> u64 {
        *s = s.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = *s;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    #[test]
    fn add256_matches_independent_reference() {
        // Edge vectors: zero, max (all-ones → wrap), single carry chains.
        let zero = [0u8; 32];
        let ones = [0xFFu8; 32];
        let mut one = [0u8; 32];
        one[0] = 1;
        for (a, b) in [(zero, zero), (ones, one), (ones, ones), (one, ones)] {
            let mut got = a;
            add256(&mut got, &b);
            assert_eq!(got, ref_add256(&a, &b), "add256 mismatch on edge vector");
        }
        // Randomised.
        let mut s = 0xDEAD_BEEF_0BAD_F00Du64;
        for _ in 0..500_000 {
            let mut a = [0u8; 32];
            let mut b = [0u8; 32];
            for chunk in a.chunks_mut(8) {
                chunk.copy_from_slice(&splitmix(&mut s).to_le_bytes());
            }
            for chunk in b.chunks_mut(8) {
                chunk.copy_from_slice(&splitmix(&mut s).to_le_bytes());
            }
            let mut got = a;
            add256(&mut got, &b);
            assert_eq!(got, ref_add256(&a, &b), "add256 mismatch: a={a:?} b={b:?}");
        }
    }

    // ---- median_time_past ----
    #[test]
    fn median_time_past_semantics() {
        assert_eq!(median_time_past(&[]), 0, "empty MTP must be 0");
        let mut s = 0x1234_5678_9ABC_DEF0u64;
        for _ in 0..200_000 {
            let n = 1 + (splitmix(&mut s) % 11) as usize; // Core uses ≤11 timestamps
            let times: Vec<u32> = (0..n).map(|_| (splitmix(&mut s) % 4_000_000_000) as u32).collect();
            let m = median_time_past(&times);
            // The result must be the middle element of the sorted window, and a member of the set.
            let mut sorted = times.clone();
            sorted.sort_unstable();
            assert_eq!(m, sorted[sorted.len() / 2], "not the sorted middle");
            assert!(times.contains(&m), "MTP result not a member of the input");
            // Order independence: shuffling the input can't change the median.
            let mut rev = times.clone();
            rev.reverse();
            assert_eq!(median_time_past(&rev), m, "MTP depends on input order");
        }
    }
}

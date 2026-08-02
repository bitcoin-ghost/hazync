//! The `RangeState` journal type and the chain constants a verifier needs.
//!
//! This is the struct the guest commits to the journal. Every consumer decodes it positionally, so
//! **field order is load-bearing**: a reordering does not fail, it silently misinterprets a valid
//! proof. A verifier that read `out_leaves` where `in_leaves` belongs would report confident nonsense.
//!
//! It existed in three hand-maintained copies (guest, host, verifier) before this crate, and a fourth
//! was about to land in ghostd. New consumers should depend on this; the existing mirrors are held in
//! step by `scripts/check-rangestate.sh`.

use serde::{Deserialize, Serialize};

/// Domain tag (H8). A receipt that is not a `RangeState` must not be mistaken for one.
pub const KIND_RANGE: u32 = 0xC4A1_0006;

/// Genesis block hash, DISPLAY order (as `bitcoin-cli getblockhash 0` prints it).
/// The journal stores tips in internal order, so this needs reversing before comparison.
pub const GENESIS_HASH: &str = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f";
pub const GENESIS_TIME: u32 = 1_231_006_505;
pub const GENESIS_BITS: u32 = 0x1d00_ffff;
/// `GetBlockProof(0x1d00ffff)` — cumulative work through block 0.
pub const GENESIS_WORK: u128 = 4_295_032_833;

/// Mirror of the guest's `RangeState`. **Field order must match the guest exactly.**
#[derive(Serialize, Deserialize, Clone, Debug)]
pub struct RangeState {
    pub kind: u32,
    pub lo: u32,
    pub hi: u32,
    pub in_tip_hash: [u8; 32],
    pub in_roots: Vec<Option<[u8; 32]>>,
    pub in_leaves: u64,
    pub in_nbits: u32,
    pub in_time: u32,
    pub in_epoch_start: u32,
    pub in_recent: Vec<u32>,
    /// Coinbase-SMT root at the IN boundary (hazync#54). Committed beside the UTXO roots because it
    /// is the same kind of thing: a state the next range must inherit unchanged. Without it in the
    /// journal a fold could join two ranges whose BIP30 state disagrees, which is the seam being
    /// checked for the UTXO set but not for this one.
    pub in_smt_root: [u8; 32],
    pub out_tip_hash: [u8; 32],
    pub out_roots: Vec<Option<[u8; 32]>>,
    pub out_leaves: u64,
    pub out_nbits: u32,
    pub out_time: u32,
    pub out_epoch_start: u32,
    pub out_recent: Vec<u32>,
    /// Coinbase-SMT root at the OUT boundary (hazync#54).
    pub out_smt_root: [u8; 32],
    pub range_work: [u8; 32],
    pub self_id: [u32; 8],
}

impl RangeState {
    /// Is the in-boundary genesis itself? This is the assertion the whole artifact rests on — without
    /// it a valid proof of an arbitrary mid-chain range passes, and the claim collapses to "someone
    /// proved a thousand blocks somewhere".
    pub fn is_genesis_anchored(&self) -> Result<(), &'static str> {
        if self.lo != 1 {
            return Err("range does not start at block 1");
        }
        let genesis_le: Vec<u8> = (0..GENESIS_HASH.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&GENESIS_HASH[i..i + 2], 16).unwrap_or(0))
            .rev()
            .collect();
        if self.in_tip_hash.as_slice() != genesis_le.as_slice() {
            return Err("in-boundary tip is not the genesis block hash");
        }
        if self.in_leaves != 0 {
            return Err("in-boundary UTXO set is not empty");
        }
        if !normalize_roots(self.in_roots.clone()).is_empty() {
            return Err("in-boundary UTXO roots are not empty");
        }
        // #54 — the coinbase-SMT must start EMPTY, for the same reason the UTXO set must. Without this
        // a prover could anchor at genesis while starting from a tree in which the coinbase it intends
        // to duplicate is already recorded as fully spent, and the BIP30 check would verify happily
        // against a history that never happened. The other in-boundary fields are pinned above; this
        // one is a consensus input exactly like them.
        if self.in_smt_root != hazync_coinbase_smt::empty_root() {
            return Err("in-boundary coinbase-SMT (BIP30) root is not the empty tree");
        }
        if self.in_nbits != GENESIS_BITS {
            return Err("in-boundary nBits != genesis");
        }
        if self.in_epoch_start != GENESIS_TIME {
            return Err("in-boundary epoch start != genesis time");
        }
        if self.in_time != GENESIS_TIME {
            return Err("in-boundary prev-time != genesis time");
        }
        if self.in_recent != vec![GENESIS_TIME] {
            return Err("in-boundary recent-times != [genesis time]");
        }
        Ok(())
    }

    /// Cumulative work through `hi`, including genesis.
    pub fn total_work(&self) -> u128 {
        GENESIS_WORK + work_u128(&self.range_work)
    }
}

/// Trailing empty root slots are not significant — an accumulator with 0 leaves may serialise with or
/// without them, so compare in normalised form.
pub fn normalize_roots(mut v: Vec<Option<[u8; 32]>>) -> Vec<Option<[u8; 32]>> {
    while v.last() == Some(&None) {
        v.pop();
    }
    v
}

/// `range_work` is a 256-bit little-endian counter; real chain work fits comfortably in the low 128.
pub fn work_u128(b: &[u8; 32]) -> u128 {
    let mut acc: u128 = 0;
    for i in (0..16).rev() {
        acc = (acc << 8) | b[i] as u128;
    }
    acc
}

#[cfg(test)]
mod genesis_pin {
    use super::*;

    /// A `RangeState` that IS genesis-anchored, so each test can break exactly one field.
    fn anchored() -> RangeState {
        let genesis_le: Vec<u8> = (0..GENESIS_HASH.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&GENESIS_HASH[i..i + 2], 16).unwrap_or(0))
            .rev()
            .collect();
        let mut tip = [0u8; 32];
        tip.copy_from_slice(&genesis_le);
        RangeState {
            kind: 0, lo: 1, hi: 1,
            in_tip_hash: tip, in_roots: Vec::new(), in_leaves: 0,
            in_nbits: GENESIS_BITS, in_time: GENESIS_TIME, in_epoch_start: GENESIS_TIME,
            in_recent: vec![GENESIS_TIME],
            in_smt_root: hazync_coinbase_smt::empty_root(),
            out_tip_hash: [0u8; 32], out_roots: Vec::new(), out_leaves: 0,
            out_nbits: GENESIS_BITS, out_time: GENESIS_TIME, out_epoch_start: GENESIS_TIME,
            out_recent: vec![GENESIS_TIME],
            out_smt_root: hazync_coinbase_smt::empty_root(),
            range_work: [0u8; 32], self_id: [0u32; 8],
        }
    }

    #[test]
    fn the_baseline_is_actually_anchored() {
        // Without this the tests below are vacuous — every one of them would "pass" by failing for
        // some unrelated reason.
        assert_eq!(anchored().is_genesis_anchored(), Ok(()),
                   "the baseline must be genesis-anchored or the negative tests prove nothing");
    }

    #[test]
    fn a_nonempty_coinbase_smt_is_not_genesis_anchored() {
        // The attack: anchor at genesis while starting from a tree in which the coinbase you intend to
        // duplicate is already recorded as fully spent. Every other in-boundary field looks perfect.
        let mut rs = anchored();
        rs.in_smt_root = [0u8; 32];   // any root that is not the empty tree
        assert!(rs.is_genesis_anchored().is_err(),
                "a range starting from a fabricated coinbase-SMT was accepted as genesis-anchored");

        let mut rs = anchored();
        rs.in_smt_root[31] ^= 1;      // one bit off the real empty root
        assert!(rs.is_genesis_anchored().is_err(),
                "a one-bit-wrong SMT root was accepted");
    }

    #[test]
    fn the_empty_root_is_not_all_zeros() {
        // A 256-deep tree of empty hashes does not fold to zero, so the zero default that an
        // Option-shaped or serde-defaulted field would produce is NOT accidentally the right answer.
        assert_ne!(hazync_coinbase_smt::empty_root(), [0u8; 32]);
    }
}

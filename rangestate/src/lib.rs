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
    pub out_tip_hash: [u8; 32],
    pub out_roots: Vec<Option<[u8; 32]>>,
    pub out_leaves: u64,
    pub out_nbits: u32,
    pub out_time: u32,
    pub out_epoch_start: u32,
    pub out_recent: Vec<u32>,
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

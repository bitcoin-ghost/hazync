//! C ABI over the Hazync verifier, for linking into ghostd (#31).
//!
//! This exists so a node calls **the same verification code** the standalone verifier and CI already
//! exercise, rather than a second implementation in C++. Verifying a risc0 receipt means Groth16 over
//! BN254 plus risc0's receipt and claim format; reimplementing that in C++ would be a large rewrite of
//! consensus-critical code that already exists and is tested, and the two would drift.
//!
//! Deliberately narrow: verify a proof, hand back the chain state it commits to. No proving, no chain
//! access, no allocation the caller must free — the caller owns the output struct.
//!
//! SAFETY CONTRACT: a non-zero return means `out` was not written and must not be read. Zero means
//! every field is valid AND the proof is genesis-anchored. There is deliberately no "verified but not
//! anchored" success case: a caller that forgot to check a separate flag would adopt a fabricated
//! anchor, so the anchoring check is not optional.

use hazync_rangestate::{normalize_roots, RangeState, KIND_RANGE};

/// Guest image id this build trusts. Pinned, and checked against `reproduce/METHOD_ID` by
/// `scripts/check-versions.sh` — a re-baseline that forgets it ships a verifier that rejects
/// every current proof.
const METHOD_ID_HEX: &str = "dfc9eeda7a5cc19f5091a642c1d88cde6fb153259d94be7e317ee20efb41206f";

pub const HAZYNC_OK: i32 = 0;
pub const HAZYNC_ERR_NULL: i32 = -1;
pub const HAZYNC_ERR_PARSE: i32 = -2;
pub const HAZYNC_ERR_PROOF: i32 = -3;
pub const HAZYNC_ERR_JOURNAL: i32 = -4;
pub const HAZYNC_ERR_NOT_ANCHORED: i32 = -5;
pub const HAZYNC_ERR_SELF_ID: i32 = -6;
pub const HAZYNC_ERR_KIND: i32 = -7;
pub const HAZYNC_ERR_TOO_MANY_ROOTS: i32 = -8;

/// Utreexo roots are popcount(leaves), so 32 covers any mainnet-scale accumulator with room to spare
/// (~200M leaves needs at most 28 slots, and typically ~14).
pub const HAZYNC_MAX_ROOTS: usize = 32;

/// Chain state a node can adopt. `repr(C)` — mirrored in `include/hazync_verify.h`.
#[repr(C)]
pub struct HazyncState {
    pub height: u32,
    /// DISPLAY order, i.e. what `bitcoin-cli getblockhash` prints — not the journal's internal order.
    pub tip_hash: [u8; 32],
    /// Cumulative work through `height`, split because C has no portable u128.
    pub cumulative_work_lo: u64,
    pub cumulative_work_hi: u64,
    pub utxo_leaves: u64,
    pub next_bits: u32,
    pub epoch_start_time: u32,
    pub prev_time: u32,
    pub root_count: u32,
    pub utxo_roots: [[u8; 32]; HAZYNC_MAX_ROOTS],
}

fn method_id_words() -> Option<[u32; 8]> {
    let b: Vec<u8> = (0..METHOD_ID_HEX.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&METHOD_ID_HEX[i..i + 2], 16).ok())
        .collect::<Option<Vec<u8>>>()?;
    let mut w = [0u32; 8];
    for i in 0..8 {
        w[i] = u32::from_le_bytes(b[i * 4..i * 4 + 4].try_into().ok()?);
    }
    Some(w)
}

/// Verify a genesis-anchored Hazync range proof and return the state it commits to.
///
/// # Safety
/// `proof` must point to `len` readable bytes; `out` must point to a writable `HazyncState`.
#[no_mangle]
pub unsafe extern "C" fn hazync_verify_proof(
    proof: *const u8,
    len: usize,
    out: *mut HazyncState,
) -> i32 {
    if proof.is_null() || out.is_null() || len == 0 {
        return HAZYNC_ERR_NULL;
    }
    let bytes = std::slice::from_raw_parts(proof, len);

    let receipt: risc0_zkvm::Receipt = match bincode::deserialize(bytes) {
        Ok(r) => r,
        Err(_) => return HAZYNC_ERR_PARSE,
    };
    let words = match method_id_words() {
        Some(w) => w,
        None => return HAZYNC_ERR_PARSE,
    };
    if receipt.verify(words).is_err() {
        return HAZYNC_ERR_PROOF;
    }
    let rs: RangeState = match receipt.journal.decode() {
        Ok(v) => v,
        Err(_) => return HAZYNC_ERR_JOURNAL,
    };
    if rs.self_id != words {
        return HAZYNC_ERR_SELF_ID;      // S1: recursion pinned to this guest
    }
    if rs.kind != KIND_RANGE {
        return HAZYNC_ERR_KIND;         // H8: not a RangeState
    }
    if rs.is_genesis_anchored().is_err() {
        return HAZYNC_ERR_NOT_ANCHORED;
    }
    let roots = normalize_roots(rs.out_roots.clone());
    let present: Vec<[u8; 32]> = roots.into_iter().flatten().collect();
    if present.len() > HAZYNC_MAX_ROOTS {
        return HAZYNC_ERR_TOO_MANY_ROOTS;
    }

    let total = rs.total_work();
    let mut st = HazyncState {
        height: rs.hi,
        tip_hash: [0; 32],
        cumulative_work_lo: total as u64,
        cumulative_work_hi: (total >> 64) as u64,
        utxo_leaves: rs.out_leaves,
        next_bits: rs.out_nbits,
        epoch_start_time: rs.out_epoch_start,
        prev_time: rs.out_time,
        root_count: present.len() as u32,
        utxo_roots: [[0u8; 32]; HAZYNC_MAX_ROOTS],
    };
    for (i, b) in rs.out_tip_hash.iter().rev().enumerate() {
        st.tip_hash[i] = *b;            // internal -> display order
    }
    for (i, r) in present.iter().enumerate() {
        st.utxo_roots[i] = *r;
    }
    *out = st;
    HAZYNC_OK
}

/// The guest image id this library trusts, as a NUL-terminated hex string. Lets a caller log or
/// cross-check which baseline it is verifying against.
#[no_mangle]
pub extern "C" fn hazync_method_id() -> *const std::os::raw::c_char {
    concat!(
        "dfc9eeda7a5cc19f5091a642c1d88cde6fb153259d94be7e317ee20efb41206f",
        "\0"
    )
    .as_ptr() as *const std::os::raw::c_char
}

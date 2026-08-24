//! C ABI over the Hazync verifier, for linking into ghostd (#31).
//!
//! This exists so a node calls **the same verification code** the standalone verifier and CI already
//! exercise, rather than a second implementation in C++. Verifying a risc0 receipt means Groth16 over
//! BN254 plus risc0's receipt and claim format; reimplementing that in C++ would be a large rewrite of
//! consensus-critical code that already exists and is tested, and the two would drift.
//!
//! Deliberately narrow: verify a proof and hand back the chain state it commits to, and check a
//! bridge UTXO dump against those committed accumulator roots (the step that makes assumeutxo PROVEN
//! rather than trusted). No proving, no chain access, no allocation the caller must free — the caller
//! owns the output struct.
//!
//! SAFETY CONTRACT: a non-zero return means `out` was not written and must not be read. Zero means
//! every field is valid AND the proof is genesis-anchored. There is deliberately no "verified but not
//! anchored" success case: a caller that forgot to check a separate flag would adopt a fabricated
//! anchor, so the anchoring check is not optional.

use hazync_rangestate::{normalize_roots, RangeState, KIND_RANGE};

/// Guest image id this build trusts. Pinned, and checked against `reproduce/METHOD_ID` by
/// `scripts/check-versions.sh` — a re-baseline that forgets it ships a verifier that rejects
/// every current proof.
const METHOD_ID_HEX: &str = "1d6c3792e5aefec398bfb03e176934f6876f423ec6f54c3d3d8f0c79ce5000c5";

pub const HAZYNC_OK: i32 = 0;
pub const HAZYNC_ERR_NULL: i32 = -1;
pub const HAZYNC_ERR_PARSE: i32 = -2;
pub const HAZYNC_ERR_PROOF: i32 = -3;
pub const HAZYNC_ERR_JOURNAL: i32 = -4;
pub const HAZYNC_ERR_NOT_ANCHORED: i32 = -5;
pub const HAZYNC_ERR_SELF_ID: i32 = -6;
pub const HAZYNC_ERR_KIND: i32 = -7;
pub const HAZYNC_ERR_TOO_MANY_ROOTS: i32 = -8;
/// UTXO-dump checking (`hazync_check_utxo_dump`).
pub const HAZYNC_ERR_DUMP_MAGIC: i32 = -9;
pub const HAZYNC_ERR_DUMP_VERSION: i32 = -10;
pub const HAZYNC_ERR_DUMP_HEIGHT: i32 = -11;
pub const HAZYNC_ERR_DUMP_COUNT: i32 = -12;
pub const HAZYNC_ERR_DUMP_TRUNC: i32 = -13;
pub const HAZYNC_ERR_DUMP_POS: i32 = -14;
pub const HAZYNC_ERR_DUMP_ROOTS: i32 = -15;

/// Utreexo roots are popcount(leaves), so 32 covers any mainnet-scale accumulator with room to spare
/// (~200M leaves needs at most 28 slots, and typically ~14).
pub const HAZYNC_MAX_ROOTS: usize = 32;

/// Chain state a node can adopt. `repr(C)` — mirrored in `include/hazync_verify.h`.
#[repr(C)]
#[derive(Clone, Copy)]
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

/// Check that a bridge UTXO dump is exactly the set a verified proof commits to.
///
/// This is what makes assumeutxo PROVEN rather than trusted. Core's `loadtxoutset` checks a snapshot
/// against a developer-chosen hash compiled into the binary; this checks it against the accumulator
/// roots a zk proof attests to, so the trust moves from "the developers picked this hash" to "these
/// blocks were valid under real consensus".
///
/// `proven` must come from a SUCCESSFUL `hazync_verify_proof`. Passing an unverified struct checks
/// the dump against nothing.
///
/// The dump carries each coin's accumulator position because it cannot be derived: the forest deletes
/// by swap-and-shrink, so its layout is a function of the whole add/delete history rather than of the
/// surviving set. Positions are therefore verified to be a permutation of `0..n-1` — a dump that
/// reused or skipped a slot could otherwise present a forest that is not the proven one.
///
/// # Memory, at mainnet scale
///
/// This rebuilds the whole forest in RAM and holds three things at once: the slot vector, the leaf
/// vector, and the forest internals — on the order of **15–20 GB at a real mainnet height** (~140M
/// coins). That is not a soundness concern (the coin count is bounded by a *verified* proof, not by
/// the untrusted file), but it is an integration constraint a caller must plan for rather than
/// discover: Core's own `loadtxoutset` is lighter only because it streams into LevelDB instead of
/// materialising the set.
///
/// So ghostd should check-then-load on a machine sized for it, and treat a streaming variant as the
/// fix if that becomes impractical — not a smaller check. Raised as F-2 by external audit #6.
///
/// # Safety
/// `dump` must point to `len` readable bytes; `proven` must be a valid `HazyncState`.
#[no_mangle]
pub unsafe extern "C" fn hazync_check_utxo_dump(
    dump: *const u8,
    len: usize,
    proven: *const HazyncState,
) -> i32 {
    if dump.is_null() || proven.is_null() || len == 0 {
        return HAZYNC_ERR_NULL;
    }
    let buf = std::slice::from_raw_parts(dump, len);
    let pv = &*proven;

    // ---- header: magic ‖ version ‖ height ‖ count ----
    const HDR: usize = 8 + 4 + 4 + 8;
    if buf.len() < HDR || &buf[..8] != b"HZUTXO\0\0" {
        return HAZYNC_ERR_DUMP_MAGIC;
    }
    let rd32 = |o: usize| u32::from_le_bytes(buf[o..o + 4].try_into().unwrap());
    let rd64 = |o: usize| u64::from_le_bytes(buf[o..o + 8].try_into().unwrap());
    if rd32(8) != 1 {
        return HAZYNC_ERR_DUMP_VERSION;
    }
    if rd32(12) != pv.height {
        return HAZYNC_ERR_DUMP_HEIGHT;
    }
    let count = rd64(16);
    // The proof states how many leaves the accumulator holds. A dump with a different number of coins
    // cannot be the proven set, and checking it here turns a confusing root mismatch into a clear one.
    if count != pv.utxo_leaves {
        return HAZYNC_ERR_DUMP_COUNT;
    }
    let n = match usize::try_from(count) {
        Ok(v) => v,
        Err(_) => return HAZYNC_ERR_DUMP_COUNT,
    };

    // `None` marks an unfilled slot, so a duplicate or missing position is caught rather than
    // silently leaving a zero leaf that would hash to something plausible.
    let mut slots: Vec<Option<[u8; 32]>> = vec![None; n];
    let mut o = HDR;
    for _ in 0..n {
        // txid 32, vout 4, value 8, height 4, coinbase 1, mtp 4, pos 4, spk_len 4
        if o + 61 > buf.len() {
            return HAZYNC_ERR_DUMP_TRUNC;
        }
        let txid: [u8; 32] = buf[o..o + 32].try_into().unwrap();
        let vout = rd32(o + 32);
        let value = rd64(o + 36);
        let height = rd32(o + 44);
        let is_cb = buf[o + 48] != 0;
        let mtp = rd32(o + 49);
        let pos = rd32(o + 53);
        let spk_len = rd32(o + 57) as usize;
        o += 61;
        if o + spk_len > buf.len() {
            return HAZYNC_ERR_DUMP_TRUNC;
        }
        let spk = &buf[o..o + spk_len];
        o += spk_len;

        let p = pos as usize;
        if p >= n || slots[p].is_some() {
            return HAZYNC_ERR_DUMP_POS;
        }
        slots[p] = Some(hazync_utreexo::coin_leaf(&txid, vout, value, spk, height, is_cb, mtp));
    }
    if o != buf.len() {
        return HAZYNC_ERR_DUMP_TRUNC; // trailing bytes: not the file it claims to be
    }

    // Every slot filled + no duplicates (checked above) == positions are a permutation of 0..n-1.
    let mut leaves = Vec::with_capacity(n);
    for s in slots {
        match s {
            Some(l) => leaves.push(l),
            None => return HAZYNC_ERR_DUMP_POS,
        }
    }

    let rebuilt: Vec<Option<[u8; 32]>> = hazync_utreexo::Forest::from_leaves(leaves).roots();
    let present: Vec<[u8; 32]> = rebuilt.into_iter().flatten().collect();
    if present.len() != pv.root_count as usize {
        return HAZYNC_ERR_DUMP_ROOTS;
    }
    for (i, r) in present.iter().enumerate() {
        if *r != pv.utxo_roots[i] {
            return HAZYNC_ERR_DUMP_ROOTS;
        }
    }
    HAZYNC_OK
}

/// The guest image id this library trusts, as a NUL-terminated hex string. Lets a caller log or
/// cross-check which baseline it is verifying against.
#[no_mangle]
pub extern "C" fn hazync_method_id() -> *const std::os::raw::c_char {
    concat!(
        "1d6c3792e5aefec398bfb03e176934f6876f423ec6f54c3d3d8f0c79ce5000c5",
        "\0"
    )
    .as_ptr() as *const std::os::raw::c_char
}

#[cfg(test)]
mod dump_tests {
    use super::*;

    /// Build a dump + the HazyncState a proof of that set would carry. `n` coins, trivial scripts.
    fn fixture(n: u32) -> (Vec<u8>, HazyncState) {
        let mut leaves = Vec::new();
        let mut body = Vec::new();
        for i in 0..n {
            let mut txid = [0u8; 32];
            txid[..4].copy_from_slice(&i.to_le_bytes());
            let spk = vec![0x51u8, i as u8]; // OP_TRUE, i — distinct per coin
            let (vout, value, height, cb, mtp) = (0u32, 1000u64 + i as u64, 100u32 + i, i % 2 == 0, 500u32 + i);
            leaves.push(hazync_utreexo::coin_leaf(&txid, vout, value, &spk, height, cb, mtp));
            body.extend_from_slice(&txid);
            body.extend_from_slice(&vout.to_le_bytes());
            body.extend_from_slice(&value.to_le_bytes());
            body.extend_from_slice(&height.to_le_bytes());
            body.push(cb as u8);
            body.extend_from_slice(&mtp.to_le_bytes());
            body.extend_from_slice(&i.to_le_bytes()); // position == insertion order
            body.extend_from_slice(&(spk.len() as u32).to_le_bytes());
            body.extend_from_slice(&spk);
        }
        let roots: Vec<[u8; 32]> = hazync_utreexo::Forest::from_leaves(leaves.clone())
            .roots().into_iter().flatten().collect();

        let mut st = HazyncState { height: 7, utxo_leaves: n as u64, root_count: roots.len() as u32,
            ..unsafe { std::mem::zeroed() } };
        for (i, r) in roots.iter().enumerate() { st.utxo_roots[i] = *r; }

        let mut out = b"HZUTXO\0\0".to_vec();
        out.extend_from_slice(&1u32.to_le_bytes());
        out.extend_from_slice(&7u32.to_le_bytes());
        out.extend_from_slice(&(n as u64).to_le_bytes());
        out.extend_from_slice(&body);
        (out, st)
    }

    fn check(d: &[u8], s: &HazyncState) -> i32 {
        unsafe { hazync_check_utxo_dump(d.as_ptr(), d.len(), s as *const _) }
    }

    #[test]
    fn accepts_the_proven_set() {
        for n in [1u32, 2, 3, 5, 8, 13] {
            let (d, s) = fixture(n);
            assert_eq!(check(&d, &s), HAZYNC_OK, "n={n}");
        }
    }

    // Each mutation must be REJECTED. A checker that accepts these accepts an unproven UTXO set,
    // which is the entire failure this function exists to prevent.
    #[test]
    fn rejects_a_tampered_value() {
        let (mut d, s) = fixture(5);
        d[28 + 36] ^= 0x01; // first coin's value -> different leaf -> different roots
        assert_eq!(check(&d, &s), HAZYNC_ERR_DUMP_ROOTS);
    }

    #[test]
    fn rejects_a_duplicate_position() {
        let (mut d, s) = fixture(5);
        let second = 24 + 61 + 2; // start of coin[1]
        d[second + 53..second + 57].copy_from_slice(&0u32.to_le_bytes()); // reuse slot 0
        assert_eq!(check(&d, &s), HAZYNC_ERR_DUMP_POS);
    }

    #[test]
    fn rejects_an_out_of_range_position() {
        let (mut d, s) = fixture(3);
        d[24 + 53..24 + 57].copy_from_slice(&99u32.to_le_bytes());
        assert_eq!(check(&d, &s), HAZYNC_ERR_DUMP_POS);
    }

    #[test]
    fn rejects_wrong_count_height_magic_and_trailing_bytes() {
        let (d, s) = fixture(4);
        let mut s2 = s.clone(); s2.utxo_leaves = 5;
        assert_eq!(check(&d, &s2), HAZYNC_ERR_DUMP_COUNT);
        let mut s3 = s.clone(); s3.height = 9;
        assert_eq!(check(&d, &s3), HAZYNC_ERR_DUMP_HEIGHT);
        let mut bad = d.clone(); bad[0] = b'X';
        assert_eq!(check(&bad, &s), HAZYNC_ERR_DUMP_MAGIC);
        let mut extra = d.clone(); extra.push(0);
        assert_eq!(check(&extra, &s), HAZYNC_ERR_DUMP_TRUNC);
        assert_eq!(check(&d[..d.len() - 1], &s), HAZYNC_ERR_DUMP_TRUNC);
    }

    // A different set with the same COUNT must fail: the roots, not the size, are what bind it.
    #[test]
    fn rejects_a_different_set_of_the_same_size() {
        let (d, _) = fixture(5);
        let (_, s_other) = fixture(5);
        let mut s = s_other.clone();
        s.utxo_roots[0][0] ^= 0xff;
        assert_eq!(check(&d, &s), HAZYNC_ERR_DUMP_ROOTS);
    }
}

//! The Hazync trust check, as a library.
//!
//! This exists so that the CLI, the WASM build and anything else that verifies a proof run **the same
//! code**. The anchoring rules are the entire product claim; a second implementation of them is a
//! second place for them to be subtly wrong, and the wrong one would fail *open* — accepting a proof
//! that is sound but unanchored, which is exactly the failure this tool exists to prevent.
//!
//! That is the same argument `verifier-ffi` already makes for ghostd. This extends it to the browser.
//!
//! Five assertions, in order (see `main.rs` for the long-form rationale):
//!
//!   1. the STARK/SNARK verifies against the canonical guest image id
//!   2. the journal's `self_id` equals that same image id      (S1: recursion pinned to this guest)
//!   3. the domain tag is KIND_RANGE                            (H8: not some other receipt shape)
//!   4. the range starts at block 1                             (genesis-anchored)
//!   5. the in-boundary IS genesis — hash, empty UTXO set, nBits, epoch start, recent-times, prev-time
//!
//! The split between `Invalid` and `NotAnchored` is deliberate and is preserved from the CLI's exit
//! codes: a mid-chain segment proof is *cryptographically perfect*, and reporting it as forged makes
//! the proof-party board look broken. It is still a refusal — there is no verified-but-unanchored
//! success value in this API, only two shapes of failure.

use hazync_rangestate::{
    normalize_roots, work_u128, RangeState, GENESIS_BITS, GENESIS_HASH, GENESIS_TIME, GENESIS_WORK,
    KIND_RANGE,
};

/// Canonical guest image id. Embedded rather than imported from the `methods` crate, which would drag
/// in the guest build. `scripts/check-versions.sh` fails the build if this drifts from
/// `reproduce/METHOD_ID`, which is the source of truth.
pub const METHOD_ID_HEX: &str = "85dc0b56af8739acac76e560fbd44141d48d753044523604dd5e2a9619224c39";

/// Everything the proof commits to — i.e. the state a node may ADOPT once verification passes.
///
/// A node that verifies the proof can resume at `height + 1` without downloading or validating
/// anything below it: it needs the UTXO commitment to check spends, and the difficulty / median-time
/// context to check the next header.
pub struct Verified {
    pub height: u32,
    /// DISPLAY order (byte-reversed), matching `bitcoin-cli getblockhash`, so callers compare directly.
    pub tip_hash: String,
    /// Internal byte order, as committed. The CLI's human output prints this one.
    pub tip_hash_internal: String,
    pub cumulative_work: u128,
    pub range_work: u128,
    pub utxo_leaves: u64,
    pub utxo_roots: Vec<String>,
    pub next_bits: u32,
    pub epoch_start_time: u32,
    pub recent_times: Vec<u32>,
    pub proof_bytes: usize,
}

pub enum VerifyError {
    /// The proof is not valid, or is malformed: forged, tampered, corrupt, or a different guest.
    /// CLI exit 1.
    Invalid(String),
    /// The SNARK verified, but the range is not anchored at genesis. CLI exit 2.
    NotAnchored { lo: u32, hi: u32, detail: String },
}

fn hex_to_bytes(s: &str) -> Result<Vec<u8>, VerifyError> {
    (0..s.len())
        .step_by(2)
        .map(|i| {
            u8::from_str_radix(&s[i..i + 2], 16)
                .map_err(|_| VerifyError::Invalid("bad hex constant".into()))
        })
        .collect()
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// Verify a serialised receipt. This is the whole trust check and nothing else — it never proves,
/// never touches the chain, needs no peers and no node.
pub fn verify(bytes: &[u8]) -> Result<Verified, VerifyError> {
    let receipt: risc0_zkvm::Receipt = bincode::deserialize(bytes)
        .map_err(|e| VerifyError::Invalid(format!("not a receipt: {e}")))?;

    let image_id = risc0_zkvm::sha::Digest::try_from(hex_to_bytes(METHOD_ID_HEX)?.as_slice())
        .map_err(|_| VerifyError::Invalid("bad embedded METHOD_ID".into()))?;

    // 1. the proof itself
    receipt.verify(image_id).map_err(|e| {
        VerifyError::Invalid(format!(
            "the proof is not valid for guest {} — forged, tampered, corrupt, or produced by a \
             different guest build.\n  underlying: {e}",
            &METHOD_ID_HEX[..8]
        ))
    })?;

    let rs: RangeState = receipt
        .journal
        .decode()
        .map_err(|e| VerifyError::Invalid(format!("journal is not a RangeState: {e}")))?;

    // 2. recursion pinned to this guest (S1)
    let claimed = hex(&rs.self_id.iter().flat_map(|w| w.to_le_bytes()).collect::<Vec<u8>>());
    if claimed != METHOD_ID_HEX {
        return Err(VerifyError::Invalid(format!(
            "journal self_id {} != guest image id {}",
            &claimed[..16],
            &METHOD_ID_HEX[..16]
        )));
    }
    // 3. domain tag (H8)
    if rs.kind != KIND_RANGE {
        return Err(VerifyError::Invalid(
            "receipt is not a RangeState (wrong domain tag)".into(),
        ));
    }
    // 4 + 5. genesis anchoring — the assertion the whole artifact is built around.
    //
    // Note which failures land in which bucket, and that this is NOT arbitrary: a range that simply
    // starts elsewhere is a legitimate segment proof (NotAnchored). A range that claims lo == 1 but
    // whose in-boundary is not actually genesis is malformed — it is asserting something false about
    // itself — and is reported as Invalid.
    if rs.lo != 1 {
        return Err(VerifyError::NotAnchored {
            lo: rs.lo,
            hi: rs.hi,
            detail: "Its range starts above block 1.".into(),
        });
    }
    let genesis_le: Vec<u8> = hex_to_bytes(GENESIS_HASH)?.into_iter().rev().collect();
    if rs.in_tip_hash.as_slice() != genesis_le.as_slice() {
        return Err(VerifyError::NotAnchored {
            lo: rs.lo,
            hi: rs.hi,
            detail: "Its in-boundary tip is not the genesis block hash.".into(),
        });
    }
    if rs.in_leaves != 0 {
        return Err(VerifyError::Invalid(
            "in-boundary UTXO set is not empty — the range does not start from nothing".into(),
        ));
    }
    if !normalize_roots(rs.in_roots.clone()).is_empty() {
        return Err(VerifyError::Invalid("in-boundary UTXO roots are not empty".into()));
    }
    if rs.in_nbits != GENESIS_BITS {
        return Err(VerifyError::Invalid("in-boundary nBits != genesis".into()));
    }
    if rs.in_epoch_start != GENESIS_TIME {
        return Err(VerifyError::Invalid("in-boundary epoch start != genesis time".into()));
    }
    if rs.in_time != GENESIS_TIME {
        return Err(VerifyError::Invalid("in-boundary prev-time != genesis time".into()));
    }
    if rs.in_recent != vec![GENESIS_TIME] {
        return Err(VerifyError::Invalid("in-boundary recent-times != [genesis time]".into()));
    }

    let range_work = work_u128(&rs.range_work);
    Ok(Verified {
        height: rs.hi,
        tip_hash: hex(&rs.out_tip_hash.iter().rev().copied().collect::<Vec<u8>>()),
        tip_hash_internal: hex(&rs.out_tip_hash),
        cumulative_work: GENESIS_WORK + range_work,
        range_work,
        utxo_leaves: rs.out_leaves,
        utxo_roots: rs.out_roots.iter().filter_map(|r| r.as_ref().map(|h| hex(h))).collect(),
        next_bits: rs.out_nbits,
        epoch_start_time: rs.out_epoch_start,
        recent_times: rs.out_recent.clone(),
        proof_bytes: bytes.len(),
    })
}

impl Verified {
    /// The state a node can adopt, as JSON. Shared by the CLI's `--json` and the WASM build so the
    /// two cannot describe the same proof differently.
    pub fn to_json(&self) -> String {
        let roots = self
            .utxo_roots
            .iter()
            .map(|r| format!("\"{r}\""))
            .collect::<Vec<_>>()
            .join(", ");
        let times = self
            .recent_times
            .iter()
            .map(|t| t.to_string())
            .collect::<Vec<_>>()
            .join(", ");
        format!(
            "{{\n  \"verified\": true,\n  \"guest_image_id\": \"{METHOD_ID_HEX}\",\n  \
             \"genesis_anchored\": true,\n  \"height\": {},\n  \"tip_hash\": \"{}\",\n  \
             \"cumulative_work\": {},\n  \"utxo_leaves\": {},\n  \"utxo_roots\": [{roots}],\n  \
             \"next_bits\": {},\n  \"epoch_start_time\": {},\n  \"recent_times\": [{times}],\n  \
             \"proof_bytes\": {},\n  \"blocks_not_validated\": {}\n}}",
            self.height,
            self.tip_hash,
            self.cumulative_work,
            self.utxo_leaves,
            self.next_bits,
            self.epoch_start_time,
            self.proof_bytes,
            self.height,
        )
    }
}

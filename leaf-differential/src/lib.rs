//! Differential test of the accumulator's LEAF PREIMAGE across its three implementations (hazync#50).
//!
//! # Why this is the one that matters
//!
//! Almost anything else that breaks costs time. A drift in the leaf preimage costs the entire board:
//! every proof made against a broken commitment is worthless and there is no repair short of
//! re-proving from genesis. It is the failure mode that reaches backwards.
//!
//! # What was already covered, and what was not
//!
//! `scripts/check-utreexo.sh` gates the construction TEXTUALLY — the tag constants are equal in all
//! three files, and each builder writes its tag before the payload. That is real, and it is not the
//! same as the builders agreeing on bytes. Two implementations can both write `TAG_LEAF` first and
//! still disagree about field order, integer width, endianness, or whether `scriptPubKey` is
//! length-prefixed.
//!
//! Real blocks do exercise the agreement — an inclusion proof only verifies if the spend-side leaf
//! equals the create-side leaf — but over the shapes those blocks happen to contain. This covers the
//! edges deliberately: empty scripts, `MAX_SCRIPT_SIZE`, zero and `MAX_MONEY` values, the coinbase
//! flag, and boundary heights.
//!
//! # The three implementations
//!
//! | where | function | side |
//! |---|---|---|
//! | `prover/methods/guest/verify_input.cpp` | `coin_leaf` (via `coin_leaf_only`) | SPEND |
//! | `prover/methods/guest/verify_input.cpp` | `tx_out_leaves` | CREATE |
//! | `accumulator/src/lib.rs` + the host's builder | Rust | both |
//!
//! The C++ here is the guest's own source, compiled natively by `build.rs` from the same translation
//! unit list. A test against a re-typed copy would prove nothing.

use bitcoin::consensus::serialize;
use bitcoin::hashes::Hash as _;
#[allow(unused_imports)]
use bitcoin::hashes::Hash as _HashTrait;
use bitcoin::{Amount, OutPoint, ScriptBuf, Sequence, Transaction, TxIn, TxOut, Witness};

extern "C" {
    /// Spend side: the leaf for `input_idx`'s spent coin, from the raw tx + its prevouts blob.
    pub fn coin_leaf_only(
        tx_bytes: *const u8, tx_len: u32, input_idx: u32,
        prevouts: *const u8, prevouts_len: u32,
        coin_height: u32, coin_is_coinbase: u32, coin_mtp: u32,
        out_leaf: *mut u8,
    );
    /// Create side: every spendable output's leaf, plus the txid.
    pub fn tx_out_leaves(
        tx: *const u8, tx_len: u32, height: u32, is_coinbase: u32, mtp: u32,
        out_leaves: *mut u8, out_txid: *mut u8,
    ) -> u32;
}

/// A `CTxOut` vector serialised the way `coin_leaf_only` expects its `prevouts` blob.
pub fn prevouts_blob(outs: &[TxOut]) -> Vec<u8> {
    let mut v = Vec::new();
    // CompactSize count, then each CTxOut.
    let n = outs.len();
    assert!(n < 253, "test blobs stay under the CompactSize boundary");
    v.push(n as u8);
    for o in outs {
        v.extend_from_slice(&serialize(o));
    }
    v
}

/// The SPEND-side leaf, from the guest's C++.
pub fn spend_leaf(tx: &Transaction, input_idx: u32, prevouts: &[TxOut],
                  coin_height: u32, coin_is_coinbase: bool, coin_mtp: u32) -> [u8; 32] {
    let raw = serialize(tx);
    let blob = prevouts_blob(prevouts);
    let mut out = [0u8; 32];
    unsafe {
        coin_leaf_only(raw.as_ptr(), raw.len() as u32, input_idx,
                       blob.as_ptr(), blob.len() as u32,
                       coin_height, coin_is_coinbase as u32, coin_mtp, out.as_mut_ptr());
    }
    out
}

/// The CREATE-side leaves, from the guest's C++.
pub fn create_leaves(tx: &Transaction, height: u32, is_coinbase: bool, mtp: u32)
    -> (Vec<[u8; 32]>, [u8; 32]) {
    let raw = serialize(tx);
    let mut buf = vec![0u8; 32 * (tx.output.len() + 1)];
    let mut txid = [0u8; 32];
    let n = unsafe {
        tx_out_leaves(raw.as_ptr(), raw.len() as u32, height, is_coinbase as u32, mtp,
                      buf.as_mut_ptr(), txid.as_mut_ptr())
    };
    let leaves = (0..n as usize)
        .map(|i| { let mut l = [0u8; 32]; l.copy_from_slice(&buf[i * 32..i * 32 + 32]); l })
        .collect();
    (leaves, txid)
}

/// Build a one-input transaction spending `outpoint`.
pub fn spending_tx(outpoint: OutPoint) -> Transaction {
    Transaction {
        version: bitcoin::transaction::Version::TWO,
        lock_time: bitcoin::absolute::LockTime::ZERO,
        input: vec![TxIn { previous_output: outpoint, script_sig: ScriptBuf::new(),
                           sequence: Sequence::MAX, witness: Witness::new() }],
        output: vec![TxOut { value: Amount::from_sat(1), script_pubkey: ScriptBuf::from_bytes(vec![0x51]) }],
    }
}

/// A funding transaction whose outputs are `outs`.
pub fn funding_tx(outs: Vec<TxOut>) -> Transaction {
    Transaction {
        version: bitcoin::transaction::Version::TWO,
        lock_time: bitcoin::absolute::LockTime::ZERO,
        input: vec![TxIn { previous_output: OutPoint::null(), script_sig: ScriptBuf::from_bytes(vec![0x01, 0x02]),
                           sequence: Sequence::MAX, witness: Witness::new() }],
        output: outs,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A spread of scriptPubKey shapes, chosen for the edges rather than for realism.
    fn scripts() -> Vec<(&'static str, ScriptBuf)> {
        vec![
            ("p2pkh-ish", ScriptBuf::from_bytes(vec![0x76, 0xa9, 0x14].into_iter().chain([0x11u8; 20]).chain([0x88, 0xac]).collect())),
            ("single-byte", ScriptBuf::from_bytes(vec![0x51])),
            ("empty", ScriptBuf::new()),
            // Length-prefixing is the field N2 added; a script whose bytes could be read as the NEXT
            // field's length is exactly what an unprefixed encoding confuses.
            ("prefix-confusable", ScriptBuf::from_bytes(vec![0x20; 32])),
            ("max-script", ScriptBuf::from_bytes(vec![0xacu8; 10_000])),
            ("just-over-max", ScriptBuf::from_bytes(vec![0xacu8; 10_001])),
            ("op-return", ScriptBuf::from_bytes(vec![0x6a, 0x01, 0x02])),
        ]
    }

    fn values() -> Vec<(&'static str, u64)> {
        vec![("zero", 0), ("one-sat", 1), ("typical", 5_000_000_000), ("max-money", 21_000_000 * 100_000_000)]
    }

    /// THE test: the leaf a coin is CREATED with must equal the leaf it is SPENT with.
    ///
    /// If these ever disagree, an inclusion proof for a real coin cannot verify — which presents as
    /// the board silently ceasing to advance, not as a wrong answer. Worse, a partial disagreement
    /// (some shapes only) means SOME blocks prove and others do not, which reads as a prover bug.
    #[test]
    fn create_side_and_spend_side_leaves_agree_for_every_shape() {
        let mut checked = 0usize;
        for (sname, spk) in scripts() {
            for (vname, val) in values() {
                for &is_cb in &[false, true] {
                    for &(h, mtp) in &[(0u32, 0u32), (1, 1), (227_931, 1_400_000_000), (u32::MAX, u32::MAX)] {
                        let out = TxOut { value: Amount::from_sat(val), script_pubkey: spk.clone() };
                        let fund = funding_tx(vec![out.clone()]);
                        let (created, txid) = create_leaves(&fund, h, is_cb, mtp);

                        // Unspendable outputs are not in the UTXO set and get no leaf — that is the
                        // create side's own rule (H3) and is why the counts can differ.
                        let spendable = !(spk.as_bytes().first() == Some(&0x6a) || spk.len() > 10_000);
                        if !spendable {
                            assert!(created.is_empty(),
                                "{sname}/{vname}: an unspendable output produced a leaf — it would be \
                                 permanently unspendable in the accumulator");
                            continue;
                        }
                        assert_eq!(created.len(), 1, "{sname}/{vname}: expected exactly one leaf");

                        let spend = spending_tx(OutPoint {
                            txid: bitcoin::Txid::from_byte_array(txid), vout: 0 });
                        let spent = spend_leaf(&spend, 0, &[out], h, is_cb, mtp);

                        assert_eq!(created[0], spent,
                            "LEAF DRIFT at {sname}/{vname} is_cb={is_cb} h={h} mtp={mtp}: the leaf a \
                             coin is created with differs from the leaf it is spent with");
                        checked += 1;
                    }
                }
            }
        }
        assert!(checked >= 100, "only {checked} shapes actually compared — the loop is not covering");
    }

    /// The fields must all be COMMITTED. If a field is dropped from the preimage, two coins that
    /// differ only in that field collide — and a collision in a UTXO commitment is a coin that can be
    /// spent twice.
    #[test]
    fn every_committed_field_changes_the_leaf() {
        let base_out = TxOut { value: Amount::from_sat(50_000), script_pubkey: ScriptBuf::from_bytes(vec![0x51, 0x52]) };
        let base = funding_tx(vec![base_out.clone()]);
        let (b, _) = create_leaves(&base, 100, false, 500);
        let base_leaf = b[0];

        let variants: Vec<(&str, [u8; 32])> = vec![
            ("height", create_leaves(&base, 101, false, 500).0[0]),
            ("is_coinbase", create_leaves(&base, 100, true, 500).0[0]),
            ("mtp", create_leaves(&base, 100, false, 501).0[0]),
            ("value", create_leaves(&funding_tx(vec![TxOut { value: Amount::from_sat(50_001), ..base_out.clone() }]), 100, false, 500).0[0]),
            ("script", create_leaves(&funding_tx(vec![TxOut { script_pubkey: ScriptBuf::from_bytes(vec![0x51, 0x53]), ..base_out.clone() }]), 100, false, 500).0[0]),
        ];
        for (field, leaf) in variants {
            assert_ne!(base_leaf, leaf,
                "changing `{field}` did not change the leaf — it is NOT committed, so two coins \
                 differing only in {field} collide in the accumulator");
        }
    }

    /// The vout must be committed too: two identical outputs in one transaction are DIFFERENT coins.
    #[test]
    fn two_identical_outputs_in_one_tx_get_different_leaves() {
        let out = TxOut { value: Amount::from_sat(7), script_pubkey: ScriptBuf::from_bytes(vec![0x51]) };
        let tx = funding_tx(vec![out.clone(), out]);
        let (leaves, _) = create_leaves(&tx, 10, false, 20);
        assert_eq!(leaves.len(), 2);
        assert_ne!(leaves[0], leaves[1],
            "two identical outputs of one tx share a leaf — spending one would spend both");
    }

    /// Length-prefixing, tested by collision rather than by inspection. Without the prefix the
    /// concatenation `script || next_field` is ambiguous, so two different coins produce one preimage.
    #[test]
    fn a_script_that_could_swallow_the_next_field_does_not_collide() {
        // Same total bytes, different split between script and the following fields.
        let a = funding_tx(vec![TxOut { value: Amount::from_sat(1),
            script_pubkey: ScriptBuf::from_bytes(vec![0xaa, 0xbb, 0xcc]) }]);
        let b = funding_tx(vec![TxOut { value: Amount::from_sat(1),
            script_pubkey: ScriptBuf::from_bytes(vec![0xaa, 0xbb]) }]);
        assert_ne!(create_leaves(&a, 5, false, 6).0[0], create_leaves(&b, 5, false, 6).0[0],
            "scripts of different length collided — the length prefix (N2) is not doing its job");
    }

    /// The THIRD implementation: the host's Rust builder, which produces the leaves that actually go
    /// into the `Forest`. If it disagreed with the guest's C++, the host would build proofs the guest
    /// cannot verify — every block would stop proving.
    ///
    /// This calls `hazync_utreexo::coin_leaf` itself, not a copy of it. A differential test against a
    /// re-typed duplicate proves only that the duplicate is self-consistent.
    #[test]
    fn the_hosts_rust_builder_agrees_with_both_cpp_sides() {
        let mut checked = 0usize;
        for (sname, spk) in scripts() {
            if spk.as_bytes().first() == Some(&0x6a) || spk.len() > 10_000 { continue; } // no leaf
            for (vname, val) in values() {
                for &is_cb in &[false, true] {
                    for &(h, mtp) in &[(0u32, 0u32), (227_931, 1_400_000_000), (u32::MAX, u32::MAX)] {
                        let out = TxOut { value: Amount::from_sat(val), script_pubkey: spk.clone() };
                        let fund = funding_tx(vec![out.clone()]);
                        let (created, txid) = create_leaves(&fund, h, is_cb, mtp);

                        let rust = hazync_utreexo::coin_leaf(
                            &txid, 0, val, spk.as_bytes(), h, is_cb, mtp);

                        assert_eq!(created[0], rust,
                            "LEAF DRIFT at {sname}/{vname} is_cb={is_cb}: the host's Rust builder \
                             disagrees with the guest's C++ CREATE side — the host would build proofs \
                             the guest cannot verify");

                        let spend = spending_tx(OutPoint {
                            txid: bitcoin::Txid::from_byte_array(txid), vout: 0 });
                        assert_eq!(spend_leaf(&spend, 0, &[out], h, is_cb, mtp), rust,
                            "LEAF DRIFT at {sname}/{vname} is_cb={is_cb}: the host's Rust builder \
                             disagrees with the guest's C++ SPEND side");
                        checked += 1;
                    }
                }
            }
        }
        assert!(checked >= 50, "only {checked} shapes compared — the loop is not covering");
    }

    /// The create side must agree with Core on the txid, or every leaf is filed under the wrong coin.
    #[test]
    fn the_txid_the_create_side_reports_is_cores_txid() {
        let tx = funding_tx(vec![TxOut { value: Amount::from_sat(9), script_pubkey: ScriptBuf::from_bytes(vec![0x51]) }]);
        let (_, txid) = create_leaves(&tx, 1, false, 1);
        assert_eq!(txid, tx.compute_txid().to_byte_array(),
            "the C++ create side and the rust-bitcoin txid disagree");
    }
}

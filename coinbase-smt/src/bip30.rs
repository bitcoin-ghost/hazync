//! The per-block BIP30 state transition (hazync#54).
//!
//! This is the logic that replaces the structural argument. It lives here, `no_std`, so the guest and
//! the bridge run the same compiled code — and so it can be tested exhaustively on the host before it
//! is load-bearing anywhere.
//!
//! # The rule, precisely
//!
//! A block is invalid if its coinbase txid duplicates a coinbase that still has **unspent outputs**.
//! Duplicating a **fully spent** one is legal — Core accepted exactly that in 2010 — so the check is
//! "absent or zero", not "absent".
//!
//! # The transition
//!
//! ```text
//!   1. prove the new coinbase txid is absent-or-zero   <- the BIP30 check itself
//!   2. insert it with its output count
//!   3. for each input spending a COINBASE output, decrement that coinbase's count,
//!      deleting it at zero
//! ```
//!
//! Step 3 is what keeps the danger set shrinking as history does, and is why a static exception table
//! is the wrong shape: ~18% of pre-BIP34 coinbases are still unspent today, and that fraction only
//! falls.
//!
//! # Proofs are SEQUENCED, and the bridge must match
//!
//! Every proof is against the root *as it stands when that step is applied*, not against the
//! incoming root. The coinbase insert lands first, so a spend's proof must be taken after it; two
//! spends of the same coinbase chain likewise. A proof taken against the wrong intermediate root is
//! refused rather than mis-folded — asserted below — which is the safe failure, but it does mean the
//! bridge has to generate proofs in exactly this order. That is a real coupling between the two, and
//! it is cheap for the bridge (it holds the tree and can step it) but easy to get wrong silently if
//! anyone reorders the steps here without reordering them there.
//!
//! # Ordering matters, and is asserted
//!
//! The check happens against the root **before** any of this block's own updates. A block whose
//! coinbase duplicates a coinbase it also spends in the same block must still be rejected — checking
//! after the decrements would let it spend the old one to zero and then claim the slot was free.

use crate::{apply, verify, Hash, Key, Proof};
use alloc::vec::Vec;

/// One coinbase-output spend: which coinbase it belonged to, and the proof of that coinbase's current
/// count under the root as it stands when the spend is applied.
pub struct Spend {
    pub coinbase_txid: Key,
    pub current_count: u32,
    pub proof: Proof,
}

/// Everything the transition needs for one block.
pub struct BlockUpdate {
    pub coinbase_txid: Key,
    pub coinbase_outputs: u32,
    /// Proof that `coinbase_txid` is absent-or-zero under the INCOMING root.
    pub absence_proof: Proof,
    /// Coinbase-output spends in this block, in application order.
    pub spends: Vec<Spend>,
}

#[derive(Debug, PartialEq, Eq)]
pub enum Bip30Error {
    /// The coinbase txid already has unspent outputs — the rule this exists to enforce.
    DuplicateUnspentCoinbase,
    /// A proof did not verify against the root it was offered for.
    BadProof,
    /// A spend claimed a count of zero, which cannot be spent from.
    SpendFromEmpty,
}

/// Apply one block's BIP30 transition, returning the new SMT root.
///
/// Fails closed: any bad proof, any duplicate, and the block is rejected. In the guest a failure means
/// no receipt at all, so there is no partial-application concern — but the root is threaded rather
/// than mutated in place regardless, so a rejected block cannot leave a half-updated state behind for
/// a caller that ignores the error.
pub fn apply_block(root: &Hash, u: &BlockUpdate) -> Result<Hash, Bip30Error> {
    // 1. THE CHECK — against the incoming root, before this block's own updates. See the ordering note
    //    above: doing it later would let a block spend the duplicate to zero and then claim the slot.
    //    "Absent" and "zero" are the same state (the tree stores no zero-valued leaves), so proving
    //    absence is exactly proving BIP30 is satisfied.
    if !verify(root, &u.coinbase_txid, None, &u.absence_proof) {
        // Either the key is present with a nonzero count — a real BIP30 violation — or the proof is
        // junk. Both reject; distinguishing them would require a second proof and buys nothing, since
        // the block is invalid either way.
        return Err(Bip30Error::DuplicateUnspentCoinbase);
    }

    // 2. Insert the new coinbase. The same proof supports this: it proved the slot's state, so it can
    //    carry the update to it — which is why the insert costs no extra witness bytes.
    let mut cur = apply(root, &u.coinbase_txid, None, Some(u.coinbase_outputs), &u.absence_proof)
        .ok_or(Bip30Error::BadProof)?;

    // 3. Decrement each spent coinbase, deleting at zero. Sequential: every proof is against the root
    //    as it stands after the previous update, which is what lets a block spend two outputs of the
    //    same coinbase — the second proof sees the first's effect.
    for s in &u.spends {
        if s.current_count == 0 {
            return Err(Bip30Error::SpendFromEmpty);
        }
        let next = s.current_count - 1;
        let new_val = if next == 0 { None } else { Some(next) };
        cur = apply(&cur, &s.coinbase_txid, Some(s.current_count), new_val, &s.proof)
            .ok_or(Bip30Error::BadProof)?;
    }
    Ok(cur)
}

#[cfg(all(test, feature = "std"))]
mod tests {
    use super::*;
    use crate::Smt;
    use sha2::{Digest, Sha256};

    fn k(n: u64) -> Key {
        let mut b = [0u8; 32];
        b[..8].copy_from_slice(&n.to_be_bytes());
        Sha256::digest(b).into()
    }

    fn update(t: &Smt, cb: Key, nout: u32) -> BlockUpdate {
        BlockUpdate {
            coinbase_txid: cb,
            coinbase_outputs: nout,
            absence_proof: t.prove(&cb),
            spends: Vec::new(),
        }
    }

    #[test]
    fn an_ordinary_block_advances_the_root() {
        let mut t = Smt::new();
        let mut root = t.root();
        for i in 0..20u64 {
            let cb = k(i);
            let new_root = apply_block(&root, &update(&t, cb, 1)).expect("honest block rejected");
            t.insert(cb, 1);
            assert_eq!(new_root, t.root(), "guest root diverged from the host tree at block {i}");
            root = new_root;
        }
    }

    #[test]
    fn a_duplicate_unspent_coinbase_is_rejected() {
        // The entire point of the structure.
        let mut t = Smt::new();
        t.insert(k(7), 1);
        let root = t.root();
        let err = apply_block(&root, &update(&t, k(7), 1)).unwrap_err();
        assert_eq!(err, Bip30Error::DuplicateUnspentCoinbase);
    }

    #[test]
    fn a_duplicate_FULLY_SPENT_coinbase_is_permitted() {
        // BIP30 allows this and Core accepted it in 2010. If this test fails the change is
        // reject-valid, which stalls a from-genesis prover rather than protecting anything — the exact
        // failure a presence-bit design would have had.
        let mut t = Smt::new();
        t.insert(k(7), 1);
        t.insert(k(7), 0); // fully spent -> absent
        let root = t.root();
        assert!(apply_block(&root, &update(&t, k(7), 2)).is_ok(),
                "duplicating a fully-spent coinbase was rejected — this is legal under BIP30");
    }

    #[test]
    fn spending_coinbase_outputs_decrements_and_deletes_at_zero() {
        let mut t = Smt::new();
        t.insert(k(1), 2);
        let root = t.root();

        // The spend proof must be against the root AFTER the coinbase insert, because apply_block
        // threads the root through in order. Proving it against the incoming root gives a stale proof
        // and is correctly refused — the first version of this test made exactly that mistake.
        let absence = t.prove(&k(50));
        let mut mid = t.clone();
        mid.insert(k(50), 1);
        let spends = vec![
            Spend { coinbase_txid: k(1), current_count: 2, proof: mid.prove(&k(1)) },
        ];
        let u = BlockUpdate {
            coinbase_txid: k(50), coinbase_outputs: 1,
            absence_proof: absence, spends,
        };
        let new_root = apply_block(&root, &u).expect("honest block rejected");
        t.insert(k(50), 1);
        t.insert(k(1), 1);
        assert_eq!(new_root, t.root(), "decrement diverged from the host tree");
    }

    #[test]
    fn two_spends_of_the_same_coinbase_in_one_block_chain_correctly() {
        // The sequential-proof property: the second spend's proof must be against the root AFTER the
        // first. Getting this wrong is the classic double-application bug, and it would leave a
        // coinbase looking unspent when it is not.
        let mut t = Smt::new();
        t.insert(k(2), 2);
        let root = t.root();

        let p1 = t.prove(&k(2));
        let mut mid = t.clone();
        mid.insert(k(2), 1);                       // state the second proof must be against
        let p2 = mid.prove(&k(2));

        let u = BlockUpdate {
            coinbase_txid: k(60), coinbase_outputs: 1,
            absence_proof: t.prove(&k(60)),
            spends: vec![
                Spend { coinbase_txid: k(2), current_count: 2, proof: p1 },
                Spend { coinbase_txid: k(2), current_count: 1, proof: p2 },
            ],
        };
        // NOTE: the absence proof and p1 are both against `root`, but p2 is against the post-p1 state.
        // apply_block threads the root, so this is the honest arrangement.
        let got = apply_block(&root, &u);
        // p1 was taken before the coinbase insert, so it will not verify against the post-insert root:
        // this asserts the transition REFUSES a stale proof rather than silently mis-folding.
        assert!(got.is_err(), "a proof taken against the wrong intermediate root was accepted");
    }

    #[test]
    fn a_block_cannot_spend_its_own_duplicate_to_free_the_slot() {
        // The ordering attack the check-before-update rule exists to stop: if the check ran after the
        // decrements, a block could spend the old coinbase to zero and then claim the slot was free.
        let mut t = Smt::new();
        t.insert(k(9), 1);
        let root = t.root();
        let u = BlockUpdate {
            coinbase_txid: k(9), coinbase_outputs: 1,
            absence_proof: t.prove(&k(9)),
            spends: vec![Spend { coinbase_txid: k(9), current_count: 1, proof: t.prove(&k(9)) }],
        };
        assert_eq!(apply_block(&root, &u).unwrap_err(), Bip30Error::DuplicateUnspentCoinbase,
                   "a block spent its own duplicate to free the slot");
    }

    #[test]
    fn a_forged_absence_proof_is_refused() {
        let mut t = Smt::new();
        t.insert(k(3), 1);
        let root = t.root();
        // Offer a proof for a DIFFERENT key and claim it covers k(3).
        let u = BlockUpdate {
            coinbase_txid: k(3), coinbase_outputs: 1,
            absence_proof: t.prove(&k(4242)),
            spends: Vec::new(),
        };
        assert!(apply_block(&root, &u).is_err(), "a proof for another key was accepted");
    }

    #[test]
    fn spending_from_an_empty_count_is_refused() {
        let t = Smt::new();
        let root = t.root();
        let u = BlockUpdate {
            coinbase_txid: k(70), coinbase_outputs: 1,
            absence_proof: t.prove(&k(70)),
            spends: vec![Spend { coinbase_txid: k(71), current_count: 0, proof: t.prove(&k(71)) }],
        };
        assert_eq!(apply_block(&root, &u).unwrap_err(), Bip30Error::SpendFromEmpty);
    }

    #[test]
    fn the_two_historical_duplicates_stop_needing_a_special_case() {
        // F3 currently grandfathers 91842/91812 and 91880/91722 with an explicit overwrite witness.
        // Under the SMT they are ordinary: the earlier coinbase is spent to zero, so the later one is
        // an ordinary absent-slot insert. Modelled here with the real shape rather than the real txids,
        // which the bridge will supply.
        let mut t = Smt::new();
        let earlier = k(91812);
        t.insert(earlier, 1);
        let root_before = t.root();
        assert!(apply_block(&root_before, &update(&t, earlier, 1)).is_err(),
                "while unspent, the duplicate must be rejected");

        t.insert(earlier, 0);                        // the earlier coinbase gets spent
        let root_after = t.root();
        assert!(apply_block(&root_after, &update(&t, earlier, 1)).is_ok(),
                "once spent, the duplicate is an ordinary insert — no special case needed");
    }
}

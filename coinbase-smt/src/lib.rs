//! Coinbase-only sparse Merkle tree — the structure that closes BIP30 permanently (hazync#54).
//!
//! # What this is for
//!
//! BIP30 forbids creating an outpoint that duplicates an **existing unspent** coin. Hazync cannot
//! check that today: a utreexo `Stump` proves membership, never non-membership. So the rule is carried
//! by a structural argument — BIP34 makes post-2013 coinbase txids mutually distinct, the two
//! historical duplicates are grandfathered, and a duplicate *non-coinbase* txid would require
//! repeating its inputs, which is a double-spend the accumulator already rejects.
//!
//! That argument is sound but **bounded at height ~1,983,702** (≈2046), where a BIP34 height push
//! could encode to bytes matching a pre-BIP34 coinbase and the first leg stops holding.
//!
//! # Why coinbase-only is sufficient rather than a shortcut
//!
//! A non-coinbase transaction can only repeat a txid by repeating its inputs byte-for-byte — spending
//! outpoints already consumed, which the accumulator refuses. Only a coinbase, having no inputs to
//! constrain it, can be duplicated while its predecessor is unspent. That takes the set needing
//! non-membership proofs from ~180M UTXOs to ~1M coinbases.
//!
//! # Why the value is a COUNT and not a presence bit
//!
//! This is the part most likely to be got wrong. BIP30 permits duplicating a **fully spent** txid —
//! that is exactly what Core accepted in 2010. A presence-only set would reject those, turning today's
//! accept-invalid risk into a **reject-valid** one at the same height, stalling a from-genesis prover
//! instead of fixing anything. So the value is "how many outputs of this coinbase are still unspent",
//! and absence and zero mean the same thing: duplication permitted.
//!
//! Measured 2026-08-02: ~18% of pre-BIP34 coinbases still have an unspent output (~41,000 of them,
//! 95% CI 27k–55k). The danger set is not small, and it SHRINKS as those coins are spent — which is
//! why a static exception table is the wrong shape and this has to be dynamic.
//!
//! # Two halves, deliberately separated — the same split as utreexo
//!
//!   * [`Smt`]   — the full tree. Host/bridge side, holds every node, generates proofs. Never in the zkVM.
//!   * [`verify`]/[`apply`] — roots-only. What the guest runs: check a proof against a root, and compute
//!     the new root after an update, without ever holding the tree.

//! # One crate, not two copies
//!
//! The guest depends on THIS crate rather than carrying a ported copy, which is the main structural
//! improvement over how utreexo is arranged. There, `prover/methods/guest/src/utreexo.rs` is a
//! verbatim port of `accumulator/`, and keeping them identical costs a CI gate plus a hand-written
//! test — because "ported verbatim" is an assertion, not a property. Here the guest and the host run
//! the same compiled code, so drift is not something to detect; it cannot occur.
//!
//! That works because the roots-only half needs no `std`: no allocation beyond `alloc::vec::Vec`, no
//! collections. Only [`Smt`] — the full tree, host-side, never in the zkVM — needs `HashMap`, so it
//! sits behind the default-on `std` feature and the guest builds with `default-features = false`.

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;
use alloc::vec::Vec;
use sha2::{Digest, Sha256};

#[cfg(feature = "std")]
use std::collections::HashMap;

pub type Hash = [u8; 32];
pub type Key = [u8; 32];

/// Domain-separation tags. Distinct from the accumulator's on purpose: an SMT node and a utreexo node
/// must never be interchangeable, or a value valid in one tree could be replayed into the other. Same
/// reasoning as `TAG_LEAF`/`TAG_NODE` there, applied across structures rather than within one.
pub const TAG_SMT_LEAF: u8 = 0x10;
pub const TAG_SMT_NODE: u8 = 0x11;

/// Tree depth. 256 = one level per bit of the key, so every key has a unique leaf slot and absence is
/// unambiguous: the slot holds the empty hash. A compacted tree would be smaller but makes
/// non-membership a case analysis ("empty, OR a different key sits here"), and case analysis in a
/// non-membership proof is where soundness bugs live.
pub const DEPTH: usize = 256;

pub fn hash_leaf(key: &Key, value: u32) -> Hash {
    let mut h = Sha256::new();
    h.update([TAG_SMT_LEAF]);
    h.update(key);
    h.update(value.to_le_bytes());
    h.finalize().into()
}

pub fn hash_node(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update([TAG_SMT_NODE]);
    h.update(left);
    h.update(right);
    h.finalize().into()
}

/// `EMPTY[d]` is the root of an all-empty subtree of height `d`. Precomputing these is what makes a
/// 256-deep sparse tree tractable: an untouched subtree costs one constant, not 2^d hashes.
pub fn empty_hashes() -> [Hash; DEPTH + 1] {
    let mut e = [[0u8; 32]; DEPTH + 1];
    for d in 1..=DEPTH {
        e[d] = hash_node(&e[d - 1], &e[d - 1]);
    }
    e
}

/// Bit `i` of the key, MSB first — the direction taken at depth `i` walking down from the root.
#[inline]
fn bit(key: &Key, i: usize) -> bool {
    (key[i / 8] >> (7 - (i % 8))) & 1 == 1
}

/// An inclusion **or** non-inclusion proof.
///
/// `siblings` carries ONLY the non-default ones, ordered leaf-to-root, with `bitmap` marking which
/// depths they belong to. A 256-deep path is otherwise 8 KB of mostly-constant hashes; in a tree of
/// ~1M entries roughly 20 are non-default, so this is ~640 bytes. That compression is not cosmetic —
/// it is the difference between ~1 GB and ~8 GB of extra bundle data across the chain.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Proof {
    pub bitmap: [u8; 32],
    pub siblings: Vec<Hash>,
}

impl Proof {
    #[inline]
    fn has(&self, depth: usize) -> bool {
        (self.bitmap[depth / 8] >> (7 - (depth % 8))) & 1 == 1
    }

    /// Serialised size, for measuring the witness cost this design lives or dies by.
    pub fn wire_len(&self) -> usize {
        32 + self.siblings.len() * 32
    }
}

/// Fold a leaf value up to a root using `proof`. `value = None` means "this slot is empty", which is
/// how non-membership is expressed: the caller asserts absence, and the fold either reproduces the
/// committed root or it does not.
///
/// Roots-only — no tree. This is the guest-side primitive.
pub fn compute_root(key: &Key, value: Option<u32>, proof: &Proof) -> Option<Hash> {
    let empty = empty_hashes();
    let mut node = match value {
        Some(v) => hash_leaf(key, v),
        None => empty[0],
    };
    let mut idx = proof.siblings.len();
    for d in (0..DEPTH).rev() {
        let sib = if proof.has(d) {
            if idx == 0 {
                return None; // bitmap claims more siblings than supplied
            }
            idx -= 1;
            proof.siblings[idx]
        } else {
            empty[DEPTH - 1 - d]
        };
        node = if bit(key, d) { hash_node(&sib, &node) } else { hash_node(&node, &sib) };
    }
    if idx != 0 {
        return None; // siblings supplied that the bitmap never claimed
    }
    Some(node)
}

/// Verify that `key` maps to `value` (or is absent, when `None`) under `root`.
pub fn verify(root: &Hash, key: &Key, value: Option<u32>, proof: &Proof) -> bool {
    compute_root(key, value, proof).map_or(false, |r| r == *root)
}

/// The new root after changing `key` from `old` to `new`, given a proof of `old`.
///
/// Returns `None` if the proof does not support `old` under `root` — so a caller cannot update from a
/// state that was never there. Every mutation is gated on proving the prior value first.
pub fn apply(root: &Hash, key: &Key, old: Option<u32>, new: Option<u32>, proof: &Proof) -> Option<Hash> {
    if !verify(root, key, old, proof) {
        return None;
    }
    compute_root(key, new, proof)
}

/// The full tree. Host/bridge side only — never compiled into the guest.
#[cfg(feature = "std")]
#[derive(Clone, Debug, Default)]
pub struct Smt {
    leaves: HashMap<Key, u32>,
}

#[cfg(feature = "std")]
impl Smt {
    pub fn new() -> Self {
        Smt { leaves: HashMap::new() }
    }

    pub fn len(&self) -> usize {
        self.leaves.len()
    }
    pub fn is_empty(&self) -> bool {
        self.leaves.is_empty()
    }
    pub fn get(&self, key: &Key) -> Option<u32> {
        self.leaves.get(key).copied()
    }

    pub fn insert(&mut self, key: Key, value: u32) {
        // Zero means "no unspent outputs", which is indistinguishable from absent as far as BIP30 is
        // concerned. Storing it would make two encodings of one state and give the root a way to
        // disagree with itself.
        if value == 0 {
            self.leaves.remove(&key);
        } else {
            self.leaves.insert(key, value);
        }
    }

    pub fn remove(&mut self, key: &Key) {
        self.leaves.remove(key);
    }

    /// Recompute the root from scratch. O(n log n); the incremental version is a later optimisation
    /// and must be differentially tested against this, exactly as `Forest`'s internals cache is.
    pub fn root(&self) -> Hash {
        let empty = empty_hashes();
        self.subtree(&self.sorted(), 0, &empty)
    }

    fn sorted(&self) -> Vec<(Key, u32)> {
        let mut v: Vec<_> = self.leaves.iter().map(|(k, val)| (*k, *val)).collect();
        v.sort_unstable_by(|a, b| a.0.cmp(&b.0));
        v
    }

    /// Root of the subtree at `depth` covering exactly the keys in `items` (which share a prefix).
    fn subtree(&self, items: &[(Key, u32)], depth: usize, empty: &[Hash; DEPTH + 1]) -> Hash {
        if items.is_empty() {
            return empty[DEPTH - depth];
        }
        if depth == DEPTH {
            return hash_leaf(&items[0].0, items[0].1);
        }
        let split = items.partition_point(|(k, _)| !bit(k, depth));
        let l = self.subtree(&items[..split], depth + 1, empty);
        let r = self.subtree(&items[split..], depth + 1, empty);
        hash_node(&l, &r)
    }

    /// Inclusion proof if `key` is present, non-inclusion proof if not — the same shape either way,
    /// which is what lets the guest treat "prove it is absent" as an ordinary check.
    pub fn prove(&self, key: &Key) -> Proof {
        let empty = empty_hashes();
        let items = self.sorted();
        let mut bitmap = [0u8; 32];
        let mut sibs: Vec<Hash> = Vec::new();
        let mut lo = 0usize;
        let mut hi = items.len();
        for d in 0..DEPTH {
            let split = lo + items[lo..hi].partition_point(|(k, _)| !bit(k, d));
            let (mine, theirs) = if bit(key, d) { ((split, hi), (lo, split)) } else { ((lo, split), (split, hi)) };
            let sib = self.subtree(&items[theirs.0..theirs.1], d + 1, &empty);
            if sib != empty[DEPTH - 1 - d] {
                bitmap[d / 8] |= 1 << (7 - (d % 8));
                sibs.push(sib);
            }
            lo = mine.0;
            hi = mine.1;
        }
        // NOT reversed. `prove` pushes root-to-leaf (d ascending); `compute_root` folds leaf-to-root
        // (d descending) and consumes from the END, so the orders already match. Reversing here was
        // the first bug in this file: it handed the deepest level the shallowest sibling, which still
        // produced a well-formed root — just not the right one. A proof that folds to a plausible
        // wrong root is exactly the failure a non-membership check must not have.
        Proof { bitmap, siblings: sibs }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn k(n: u64) -> Key {
        let mut key = [0u8; 32];
        key[..8].copy_from_slice(&n.to_be_bytes());
        // Spread the keys across the tree; sequential prefixes would only ever exercise one corner.
        let d: Hash = Sha256::digest(key).into();
        d
    }

    #[test]
    fn empty_tree_root_is_the_empty_hash() {
        assert_eq!(Smt::new().root(), empty_hashes()[DEPTH]);
    }

    #[test]
    fn absence_proves_against_the_root_for_every_size() {
        // The whole point of the structure. If non-membership ever fails to verify, BIP30 cannot be
        // checked at all — so walk sizes rather than sampling one.
        for n in 0..40u64 {
            let mut t = Smt::new();
            for i in 0..n {
                t.insert(k(i), 1);
            }
            let root = t.root();
            let absent = k(10_000 + n);
            assert!(verify(&root, &absent, None, &t.prove(&absent)), "absence failed at n={n}");
        }
    }

    #[test]
    fn presence_proves_and_absence_does_not_for_the_same_key() {
        let mut t = Smt::new();
        for i in 0..64u64 {
            t.insert(k(i), (i as u32 % 7) + 1);
        }
        let root = t.root();
        for i in 0..64u64 {
            let key = k(i);
            let v = t.get(&key).unwrap();
            let p = t.prove(&key);
            assert!(verify(&root, &key, Some(v), &p), "present key {i} failed to prove");
            assert!(!verify(&root, &key, None, &p), "present key {i} ALSO proved absent — non-membership is broken");
            assert!(!verify(&root, &key, Some(v + 1), &p), "wrong value proved for key {i}");
        }
    }

    #[test]
    fn apply_moves_the_root_exactly_as_a_rebuild_would() {
        // The guest computes the new root from a proof; the host rebuilds. They must agree, or the
        // seam between guest and bridge is broken.
        let mut t = Smt::new();
        for i in 0..32u64 {
            t.insert(k(i), 3);
        }
        for i in 0..32u64 {
            let root = t.root();
            let key = k(i);
            let p = t.prove(&key);
            let via_apply = apply(&root, &key, Some(3), Some(2), &p).expect("apply should accept a valid proof");
            t.insert(key, 2);
            assert_eq!(via_apply, t.root(), "apply disagreed with a rebuild at key {i}");
        }
    }

    #[test]
    fn insert_then_delete_returns_to_the_original_root() {
        let mut t = Smt::new();
        for i in 0..20u64 {
            t.insert(k(i), 1);
        }
        let before = t.root();
        t.insert(k(999), 5);
        assert_ne!(t.root(), before, "inserting did not change the root");
        t.remove(&k(999));
        assert_eq!(t.root(), before, "delete did not restore the root — the tree is not history-independent");
    }

    #[test]
    fn zero_is_the_same_state_as_absent() {
        // BIP30 permits duplicating a fully-spent coinbase, so "0 unspent" and "never existed" must be
        // the SAME state. Two encodings of one state would let the root disagree with itself.
        let mut a = Smt::new();
        let mut b = Smt::new();
        for i in 0..10u64 {
            a.insert(k(i), 1);
            b.insert(k(i), 1);
        }
        a.insert(k(500), 0); // decremented to nothing
        assert_eq!(a.root(), b.root(), "a zero-count entry changed the root");
        assert_eq!(a.get(&k(500)), None, "zero should read back as absent");
    }

    #[test]
    fn apply_refuses_an_update_from_a_state_that_was_never_there() {
        let mut t = Smt::new();
        for i in 0..16u64 {
            t.insert(k(i), 4);
        }
        let root = t.root();
        let key = k(3);
        let p = t.prove(&key);
        assert!(apply(&root, &key, Some(99), Some(1), &p).is_none(), "apply accepted a false prior value");
        assert!(apply(&root, &key, None, Some(1), &p).is_none(), "apply accepted 'absent' for a present key");
    }

    #[test]
    fn a_malformed_proof_is_refused_rather_than_mis_folded() {
        let mut t = Smt::new();
        for i in 0..24u64 {
            t.insert(k(i), 2);
        }
        let root = t.root();
        let key = k(5);

        let mut short = t.prove(&key);
        if !short.siblings.is_empty() {
            short.siblings.pop();
            assert!(!verify(&root, &key, Some(2), &short), "fewer siblings than the bitmap claims was accepted");
        }
        let mut long = t.prove(&key);
        long.siblings.push([9u8; 32]);
        assert!(!verify(&root, &key, Some(2), &long), "more siblings than the bitmap claims was accepted");
    }

    #[test]
    fn proofs_stay_compact_at_scale() {
        // The design lives or dies on this: 256 uncompressed siblings is 8 KB per block, which across
        // the chain is ~7.7 GB of extra bundle data on a bridge already capped for disk.
        let mut t = Smt::new();
        for i in 0..4096u64 {
            t.insert(k(i), 1);
        }
        let p = t.prove(&k(1));
        assert!(p.siblings.len() <= 24, "proof carried {} siblings at n=4096", p.siblings.len());
        assert!(p.wire_len() < 1024, "proof is {} bytes on the wire", p.wire_len());
    }
}

#[cfg(test)]
mod oracle {
    //! Differential tests against a deliberately naive tree.
    //!
    //! Audit #2's warning about the reference/guest accumulator pair applies doubly here: this is a
    //! SECOND structure whose agreement matters. The optimised `Smt` prunes empty subtrees to
    //! constants and proves by descending sorted slices — both are exactly the kind of cleverness that
    //! produces a self-consistent wrong answer. So it is checked against a tree that does none of it.
    //!
    //! The naive version is intentionally stupid: it materialises the path bit by bit and hashes
    //! everything, with no sparse shortcut at all. It would be far too slow for real use, which is the
    //! point — it shares no code and therefore no bugs.

    use super::*;

    fn k(n: u64) -> Key {
        let mut key = [0u8; 32];
        key[..8].copy_from_slice(&n.to_be_bytes());
        Sha256::digest(key).into()
    }

    /// Root by brute force: walk every key's full 256-bit path into a map of (depth, path) -> hash,
    /// then fold upward level by level. No empty-subtree shortcut, no sorting, no slices.
    fn naive_root(leaves: &[(Key, u32)]) -> Hash {
        let empty = empty_hashes();
        // level[d] maps the path prefix (as a bit vector) to the node hash at that depth.
        let mut level: HashMap<Vec<bool>, Hash> = HashMap::new();
        for (key, v) in leaves {
            if *v == 0 {
                continue;
            }
            let path: Vec<bool> = (0..DEPTH).map(|i| bit(key, i)).collect();
            level.insert(path, hash_leaf(key, *v));
        }
        for d in (0..DEPTH).rev() {
            let mut up: HashMap<Vec<bool>, Hash> = HashMap::new();
            for (path, h) in &level {
                let mut parent = path.clone();
                let last = parent.pop().unwrap();
                let e = &mut up.entry(parent.clone()).or_insert_with(|| {
                    // start from both-empty and fill in whichever side we have
                    hash_node(&empty[DEPTH - 1 - d], &empty[DEPTH - 1 - d])
                });
                let sib_path = {
                    let mut p = parent.clone();
                    p.push(!last);
                    p
                };
                let sib = level.get(&sib_path).copied().unwrap_or(empty[DEPTH - 1 - d]);
                **e = if last { hash_node(&sib, h) } else { hash_node(h, &sib) };
            }
            level = up;
        }
        level.get(&Vec::new()).copied().unwrap_or(empty[DEPTH])
    }

    #[test]
    fn optimised_root_matches_a_naive_rebuild() {
        for n in 0..24u64 {
            let mut t = Smt::new();
            let mut flat = Vec::new();
            for i in 0..n {
                let v = (i as u32 % 5) + 1;
                t.insert(k(i), v);
                flat.push((k(i), v));
            }
            assert_eq!(t.root(), naive_root(&flat), "root diverged from the naive oracle at n={n}");
        }
    }

    #[test]
    fn a_random_walk_of_inserts_deletes_and_decrements_stays_in_agreement() {
        // Sequences matter more than states: the failure mode is an update path that is right in
        // isolation and wrong after a particular history.
        let mut st = 0x243F6A8885A308D3u64;
        let mut next = || {
            st ^= st << 13;
            st ^= st >> 7;
            st ^= st << 17;
            st
        };
        let mut t = Smt::new();
        let mut model: HashMap<Key, u32> = HashMap::new();
        for step in 0..600 {
            let key = k(next() % 50);
            match next() % 3 {
                0 => {
                    let v = (next() % 4) as u32 + 1;
                    t.insert(key, v);
                    model.insert(key, v);
                }
                1 => {
                    t.remove(&key);
                    model.remove(&key);
                }
                _ => {
                    // decrement, deleting at zero — the real BIP30 lifecycle
                    let cur = model.get(&key).copied().unwrap_or(0);
                    let new = cur.saturating_sub(1);
                    t.insert(key, new);
                    if new == 0 {
                        model.remove(&key);
                    } else {
                        model.insert(key, new);
                    }
                }
            }
            let flat: Vec<(Key, u32)> = model.iter().map(|(a, b)| (*a, *b)).collect();
            assert_eq!(t.root(), naive_root(&flat), "diverged from the oracle at step {step}");

            // and every membership/non-membership claim must still verify against that root
            let root = t.root();
            let probe = k(next() % 60);
            assert!(
                verify(&root, &probe, model.get(&probe).copied(), &t.prove(&probe)),
                "proof failed at step {step}"
            );
        }
    }
}

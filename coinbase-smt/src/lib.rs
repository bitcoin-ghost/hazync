//! ⚠ GUEST-COMPILED CRATE — editing this file changes METHOD_ID and re-baselines the board.
//!
//! This crate is a path dependency of `prover/methods/guest`, so it is compiled into the guest ELF.
//! Rust embeds file and line into panic metadata, which means ANY edit here that moves line numbers
//! changes the image id — including pure comments, and including changes to code the guest never
//! calls. Every proof on the board becomes invalid.
//!
//! That is easy to miss precisely because this directory is nowhere near the guest and has its own
//! host-side test suite. It has already happened once: adding `empty_root()` below — five lines, for a
//! host-side genesis pin — moved the id from 4ea6567b… to 35cfbbed…, and adding this very notice moved it again.
//! `scripts/check-guest-inputs.sh`
//! exists to keep this notice attached to every crate in that position.
//!
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

pub mod bip30;

// The roots-only half lives in roots.rs so the GUEST can #[path]-include it — see the note there.
// Re-exported flat so `hazync_coinbase_smt::verify` etc. keep working for the host and bridge.
mod roots;
pub use roots::*;
use roots::bit;   // private helper the std-only Smt shares with the roots half

/// The full tree. Host/bridge side only — never compiled into the guest.
#[cfg(feature = "std")]
use std::collections::BTreeMap;

/// Depth above which interior nodes are cached.
///
/// Chosen from the shape of the real data, not by taste. The danger set is ~41k unspent pre-BIP34
/// coinbases; with keys as uniform as txids, a subtree at depth 20 holds 41k/2^20 ≈ 0.04 leaves, so
/// essentially every one is empty or a single leaf and the uncached tail below costs nothing. Going
/// deeper buys no speed and costs memory linearly; going much shallower starts putting real leaf
/// counts under the cutoff, and the recompute on each update grows with them.
#[cfg(feature = "std")]
const CACHE_DEPTH: usize = 20;

/// The host-side tree. Not compiled into the guest — the guest only ever verifies proofs against a
/// root, which is the whole point of the structure.
///
/// # Why this is incremental, and why that is not premature
///
/// A full recompute is inherently O(n · DEPTH) hashes: this is an uncollapsed 256-deep SMT, so even a
/// subtree holding one leaf costs ~256 hashes to fold up through its empty siblings. Measured at
/// n=50,000 in release: **seconds per root, against tens of microseconds for a maintained update — a
/// ratio around 37,000x**. The bridge needs a root for *every block*, so at rebuild cost a
/// from-genesis pass would spend *hundreds of hours* inside this function alone. Incremental is not an
/// optimisation here, it is the difference between the design working and not.
///
/// The RATIO is quoted rather than absolute timings on purpose. The first version of this note carried
/// "1.16 s" and "29 us" from one session; re-run later on a loaded machine the live numbers were ~2.7x
/// larger while the recorded ones stayed put, which is how a true measurement becomes a misleading
/// one. `a_maintained_root_is_orders_of_magnitude_cheaper_than_a_rebuild` measures both sides in the
/// same run and asserts the ratio, so the comparison cannot go stale again.
///
/// So the root is maintained, never recomputed: an update touches only the ~256 nodes on one key's
/// path. `root_naive`/`prove_naive` keep the original from-scratch implementations as the reference
/// the fast paths are differentially tested against, which is the same arrangement `Forest` uses for
/// its own internals cache.
#[cfg(feature = "std")]
#[derive(Clone, Debug)]
pub struct Smt {
    /// Ordered, not a `HashMap`: every update needs the leaves under one prefix, and a range query
    /// gives that in O(log n + k). Re-sorting the whole map per update was the other half of the cost.
    leaves: BTreeMap<Key, u32>,
    /// `(depth, prefix) -> hash` for depths 1..=CACHE_DEPTH. An absent entry means "empty subtree",
    /// so entries that fold back to empty are REMOVED rather than stored — otherwise the map grows
    /// without bound as coinbases are spent to zero, and two encodings of empty appear.
    nodes: HashMap<(u8, Key), Hash>,
    /// `empty_hashes()` is itself 256 hashes; it is invariant, so it is computed once.
    empty: [Hash; DEPTH + 1],
    root: Hash,
}

#[cfg(feature = "std")]
impl Default for Smt {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(feature = "std")]
impl Smt {
    pub fn new() -> Self {
        let empty = empty_hashes();
        let root = empty[DEPTH];
        Smt { leaves: BTreeMap::new(), nodes: HashMap::new(), empty, root }
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
        self.recompute_path(&key);
    }

    pub fn remove(&mut self, key: &Key) {
        self.leaves.remove(key);
        self.recompute_path(key);
    }

    /// The current root. O(1) — maintained by `recompute_path`, not recomputed here.
    pub fn root(&self) -> Hash {
        self.root
    }

    /// Entries in a DETERMINISTIC order, for checkpointing.
    ///
    /// The `BTreeMap` is already ordered, so this is the map's own order rather than a sort imposed on
    /// top of it — but the guarantee callers rely on is the same: a checkpoint that serialised
    /// differently on each save would be a state that disagrees with itself byte-for-byte while
    /// agreeing on the root, which makes any "did the state change?" comparison downstream meaningless.
    pub fn entries(&self) -> Vec<(Key, u32)> {
        self.leaves.iter().map(|(k, v)| (*k, *v)).collect()
    }

    /// Rebuild from `entries()`. Zero-valued pairs are dropped, so a hand-edited or corrupted
    /// checkpoint cannot introduce the second encoding of "no unspent outputs" that `insert` exists
    /// to prevent.
    ///
    /// This is O(n) incremental updates, not a bulk build — a few seconds at danger-set size, paid
    /// once when the bridge resumes. Not worth a second code path that would need its own differential
    /// test to be trustworthy.
    pub fn from_entries(entries: Vec<(Key, u32)>) -> Self {
        let mut t = Smt::new();
        for (k, v) in entries {
            t.insert(k, v);
        }
        t
    }

    // ---- incremental maintenance -------------------------------------------------------------

    /// `key` truncated to its first `depth` bits, the rest zeroed. This is the cache's node identity:
    /// every key under a node shares its prefix, so all of them map to the same entry.
    fn mask(key: &Key, depth: usize) -> Key {
        let mut out = [0u8; 32];
        let full = depth / 8;
        out[..full].copy_from_slice(&key[..full]);
        let rem = depth % 8;
        if rem != 0 {
            out[full] = key[full] & (0xffu8 << (8 - rem));
        }
        out
    }

    /// The leaves under `(prefix, depth)`, in key order.
    ///
    /// The upper bound is the prefix with every remaining bit set, so this is an inclusive range over
    /// exactly the subtree — no filtering pass, and no chance of picking up a neighbouring subtree.
    fn under(&self, prefix: &Key, depth: usize) -> Vec<(Key, u32)> {
        let lo = Self::mask(prefix, depth);
        let mut hi = lo;
        let full = depth / 8;
        let rem = depth % 8;
        if rem != 0 {
            hi[full] |= 0xffu8 >> rem;
            for b in hi.iter_mut().skip(full + 1) {
                *b = 0xff;
            }
        } else {
            for b in hi.iter_mut().skip(full) {
                *b = 0xff;
            }
        }
        self.leaves.range(lo..=hi).map(|(k, v)| (*k, *v)).collect()
    }

    /// Recompute every cached node on `key`'s path, and the root.
    ///
    /// Below CACHE_DEPTH the subtree is recomputed from scratch — cheap, because at that depth it
    /// holds at most a handful of leaves. Above it, each level is one hash against a cached sibling.
    fn recompute_path(&mut self, key: &Key) {
        let items = self.under(key, CACHE_DEPTH);
        let mut cur = self.subtree(&items, CACHE_DEPTH);
        self.put(CACHE_DEPTH, Self::mask(key, CACHE_DEPTH), cur);

        for d in (0..CACHE_DEPTH).rev() {
            let sib = self.sibling_cached(key, d);
            cur = if bit(key, d) { hash_node(&sib, &cur) } else { hash_node(&cur, &sib) };
            if d > 0 {
                self.put(d, Self::mask(key, d), cur);
            }
        }
        self.root = cur;
    }

    /// The cached sibling of `key`'s child at depth `d + 1`, or the empty value when absent.
    fn sibling_cached(&self, key: &Key, d: usize) -> Hash {
        let mut sp = Self::mask(key, d + 1);
        sp[d / 8] ^= 1 << (7 - (d % 8));
        self.nodes.get(&((d + 1) as u8, sp)).copied().unwrap_or(self.empty[DEPTH - (d + 1)])
    }

    /// Store a cached node, or DROP it when it folds back to empty. See the note on `nodes`: an absent
    /// entry is what "empty" means, so writing the empty hash would be a second encoding of it and
    /// would leak an entry per spent-to-zero coinbase for ever.
    fn put(&mut self, depth: usize, prefix: Key, h: Hash) {
        if h == self.empty[DEPTH - depth] {
            self.nodes.remove(&(depth as u8, prefix));
        } else {
            self.nodes.insert((depth as u8, prefix), h);
        }
    }

    /// Root of the subtree at `depth` covering exactly the keys in `items` (which share a prefix).
    fn subtree(&self, items: &[(Key, u32)], depth: usize) -> Hash {
        if items.is_empty() {
            return self.empty[DEPTH - depth];
        }
        if depth == DEPTH {
            return hash_leaf(&items[0].0, items[0].1);
        }
        let split = items.partition_point(|(k, _)| !bit(k, depth));
        let l = self.subtree(&items[..split], depth + 1);
        let r = self.subtree(&items[split..], depth + 1);
        hash_node(&l, &r)
    }

    /// Inclusion proof if `key` is present, non-inclusion proof if not — the same shape either way,
    /// which is what lets the guest treat "prove it is absent" as an ordinary check.
    ///
    /// Siblings above CACHE_DEPTH come from the cache. Below it they are computed, which is almost
    /// free in practice: at that depth a sibling subtree is nearly always empty, and an empty subtree
    /// costs one lookup and no hashing.
    pub fn prove(&self, key: &Key) -> Proof {
        let mut bitmap = [0u8; 32];
        let mut sibs: Vec<Hash> = Vec::new();
        for d in 0..DEPTH {
            let sib = if d + 1 <= CACHE_DEPTH {
                self.sibling_cached(key, d)
            } else {
                let mut sp = Self::mask(key, d + 1);
                sp[d / 8] ^= 1 << (7 - (d % 8));
                let items = self.under(&sp, d + 1);
                self.subtree(&items, d + 1)
            };
            if sib != self.empty[DEPTH - 1 - d] {
                bitmap[d / 8] |= 1 << (7 - (d % 8));
                sibs.push(sib);
            }
        }
        // NOT reversed. `prove` pushes root-to-leaf (d ascending); `compute_root` folds leaf-to-root
        // (d descending) and consumes from the END, so the orders already match. Reversing here was
        // the first bug in this file: it handed the deepest level the shallowest sibling, which still
        // produced a well-formed root — just not the right one. A proof that folds to a plausible
        // wrong root is exactly the failure a non-membership check must not have.
        Proof { bitmap, siblings: sibs }
    }

    // ---- the from-scratch reference the fast paths are tested against ------------------------

    /// Recompute the root from scratch. O(n · DEPTH); kept as the oracle for `root`, never called on
    /// the hot path. See `incremental_matches_the_from_scratch_reference`.
    pub fn root_naive(&self) -> Hash {
        let items: Vec<(Key, u32)> = self.leaves.iter().map(|(k, v)| (*k, *v)).collect();
        self.subtree(&items, 0)
    }

    /// From-scratch `prove`, the oracle for the cached one.
    pub fn prove_naive(&self, key: &Key) -> Proof {
        let items: Vec<(Key, u32)> = self.leaves.iter().map(|(k, v)| (*k, *v)).collect();
        let mut bitmap = [0u8; 32];
        let mut sibs: Vec<Hash> = Vec::new();
        let (mut lo, mut hi) = (0usize, items.len());
        for d in 0..DEPTH {
            let split = lo + items[lo..hi].partition_point(|(k, _)| !bit(k, d));
            let (mine, theirs) =
                if bit(key, d) { ((split, hi), (lo, split)) } else { ((lo, split), (split, hi)) };
            let sib = self.subtree(&items[theirs.0..theirs.1], d + 1);
            if sib != self.empty[DEPTH - 1 - d] {
                bitmap[d / 8] |= 1 << (7 - (d % 8));
                sibs.push(sib);
            }
            lo = mine.0;
            hi = mine.1;
        }
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

#[cfg(all(test, feature = "std"))]
mod incremental {
    use super::*;

    fn k(n: u64) -> Key {
        let mut b = [0u8; 32];
        b[..8].copy_from_slice(&n.to_le_bytes());
        hash_leaf(&b, 1) // spread like real txids; sequential prefixes exercise one corner only
    }

    /// The test that makes the incremental root trustworthy at all.
    ///
    /// The failure it exists to catch is not "wrong answer" — a mis-maintained cache produces a
    /// perfectly well-formed root that simply is not the tree's. That is the same class of bug as the
    /// reversed-siblings one recorded in `prove`, and it is invisible without an independent oracle.
    #[test]
    fn incremental_matches_the_from_scratch_reference() {
        let mut t = Smt::new();
        assert_eq!(t.root(), t.root_naive(), "empty tree");

        // Grow.
        for i in 0..200u64 {
            t.insert(k(i), (i % 7 + 1) as u32);
            assert_eq!(t.root(), t.root_naive(), "root diverged after insert {i}");
        }
        // Update in place — the case where a leaf changes value without the shape changing.
        for i in (0..200u64).step_by(3) {
            t.insert(k(i), 99);
            assert_eq!(t.root(), t.root_naive(), "root diverged after overwrite {i}");
        }
        // Shrink, including all the way back to empty: the cache must SHED nodes, not just add them.
        for i in 0..200u64 {
            t.remove(&k(i));
            assert_eq!(t.root(), t.root_naive(), "root diverged after remove {i}");
        }
        assert_eq!(t.len(), 0);
        assert_eq!(t.root(), Smt::new().root(), "a tree emptied is not the empty tree");
        assert!(t.nodes.is_empty(), "cache leaked {} nodes after emptying", t.nodes.len());
    }

    #[test]
    fn cached_proofs_match_from_scratch_proofs_present_and_absent() {
        let mut t = Smt::new();
        for i in 0..150u64 {
            t.insert(k(i), 2);
        }
        for i in 0..150u64 {
            let (fast, slow) = (t.prove(&k(i)), t.prove_naive(&k(i)));
            assert_eq!(fast.bitmap, slow.bitmap, "membership bitmap differs at {i}");
            assert_eq!(fast.siblings, slow.siblings, "membership siblings differ at {i}");
            assert!(verify(&t.root(), &k(i), Some(2), &fast), "cached membership proof failed at {i}");
        }
        for i in 500..560u64 {
            let (fast, slow) = (t.prove(&k(i)), t.prove_naive(&k(i)));
            assert_eq!(fast.bitmap, slow.bitmap, "absence bitmap differs at {i}");
            assert_eq!(fast.siblings, slow.siblings, "absence siblings differ at {i}");
            assert!(verify(&t.root(), &k(i), None, &fast), "cached absence proof failed at {i}");
        }
    }

    #[test]
    fn keys_sharing_a_long_prefix_still_agree() {
        // CACHE_DEPTH assumes subtrees below it hold ~one leaf. Deliberately break that assumption:
        // if the cutoff logic is wrong, colliding prefixes are exactly where it shows.
        let mut t = Smt::new();
        let base = k(1);
        for i in 0..32u32 {
            let mut key = base;
            key[31] = i as u8; // identical for the first 248 bits, well past CACHE_DEPTH
            t.insert(key, i + 1);
        }
        assert_eq!(t.root(), t.root_naive(), "deep-collision root diverged");
        for i in 0..32u32 {
            let mut key = base;
            key[31] = i as u8;
            assert_eq!(t.prove(&key).siblings, t.prove_naive(&key).siblings, "deep-collision proof {i}");
            assert!(verify(&t.root(), &key, Some(i + 1), &t.prove(&key)));
        }
    }

    /// Records the number the design depends on: how much cheaper a maintained root is than a
    /// from-scratch one at danger-set size.
    ///
    /// BOTH SIDES ARE MEASURED HERE, in the same run, and the assertion is on the RATIO. An earlier
    /// version printed the incremental cost next to a from-scratch figure hardcoded from a different
    /// session — which drifted, exactly like every other stale recorded value on this project: re-run
    /// on a loaded machine, the live number was ~2.7x its own recorded value while the constant it was
    /// being compared against stayed put, making the speedup look four times better than it was.
    ///
    /// Absolute timings here are machine- and load-dependent and mean little on their own. The ratio
    /// is the robust quantity and the one the design actually rests on, so it is what gets asserted.
    /// The threshold is deliberately far below the observed value — this catches a return to
    /// O(n · DEPTH), not a slow afternoon.
    #[test]
    fn a_maintained_root_is_orders_of_magnitude_cheaper_than_a_rebuild() {
        let mut t = Smt::new();
        for i in 0..50_000u64 {
            t.insert(k(i), 1);
        }
        let s = std::time::Instant::now();
        for i in 50_000..50_100u64 {
            t.insert(k(i), 1);
        }
        let update = s.elapsed() / 100;

        let s = std::time::Instant::now();
        let _ = t.prove(&k(7));
        let prove = s.elapsed();

        let s = std::time::Instant::now();
        let _ = t.root_naive();
        let rebuild = s.elapsed();

        let ratio = rebuild.as_secs_f64() / update.as_secs_f64();
        println!("COST n=50k  update={update:?}  prove={prove:?}  from-scratch rebuild={rebuild:?}");
        println!("COST n=50k  rebuild/update = {ratio:.0}x");

        // The bridge needs a root per block over ~900k blocks. At the rebuild cost that is hundreds of
        // hours inside this one function; the whole point of maintaining the root is that it is not.
        assert!(ratio > 1_000.0,
            "a maintained update is only {ratio:.0}x cheaper than a full rebuild \
             (update={update:?}, rebuild={rebuild:?}) — the incremental path is not working");
    }
}

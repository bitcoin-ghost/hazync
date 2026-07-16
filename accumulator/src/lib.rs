//! Utreexo hash-forest UTXO accumulator — the piece that makes Hazync block proofs *stateless*
//! (HAZYNC_ARCHITECTURE.md §2.3). The prover never carries the ~10 GB UTXO set; it carries a
//! tiny root set and is handed per-input **inclusion proofs** by a bridge node.
//!
//! Two halves, deliberately separated:
//!   * [`Forest`] — the full accumulator. Host-side "bridge" oracle: holds every node, generates
//!     inclusion proofs. NEVER runs in the zkVM. This is our ground-truth reference.
//!   * [`Stump`]  — roots only. The `verify`/`update` logic here is what gets ported into the
//!     guest and proven. Given the roots + proofs it decides inclusion and derives the next root,
//!     with no access to the full set.
//!
//! A forest of `n` leaves is a set of perfect binary Merkle trees, one per set bit of `n`
//! (n = 5 = 0b101 → a tree of 4 leaves and a tree of 1). Roots are indexed by tree *height*;
//! because a forest's leaf count has distinct bits, at most one tree exists per height — so a
//! proof's height uniquely identifies which root it must match.

use sha2::{Digest, Sha256};

pub type Hash = [u8; 32];

/// Interior node hash: SHA256(left || right). In the guest this same op routes through the
/// RISC0 SHA accelerator — bit-identical, so the logic developed here transfers unchanged.
pub fn parent(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update(left);
    h.update(right);
    h.finalize().into()
}

/// Leaf commitment for a UTXO. `data` is the caller's canonical serialization of the coin
/// (outpoint + height/coinbase flag + CTxOut). Opaque here; the accumulator only hashes it.
pub fn hash_leaf(data: &[u8]) -> Hash {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().into()
}

/// A leaf's path to its tree root: the leaf's index *within its tree* and the sibling hashes
/// bottom-up. `siblings.len()` == the tree's height == the root height it must match.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Proof {
    pub leaf: Hash,
    pub position: u64, // index within the containing tree (low `height` bits pick L/R per level)
    pub siblings: Vec<Hash>,
}

impl Proof {
    /// Fold the leaf up through its siblings to the tree root it claims.
    pub fn compute_root(&self) -> Hash {
        let mut node = self.leaf;
        let mut pos = self.position;
        for sib in &self.siblings {
            node = if pos & 1 == 0 { parent(&node, sib) } else { parent(sib, &node) };
            pos >>= 1;
        }
        node
    }
}

// ------------------------------------------------------------------ Stump (guest-side) --------

/// Roots-only accumulator state. `roots[h]` is the root of the height-`h` tree, or `None`.
/// This is the whole accumulator the guest holds; `num_leaves` fixes the forest shape.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct Stump {
    pub roots: Vec<Option<Hash>>, // indexed by tree height (0 = a lone leaf)
    pub num_leaves: u64,
}

impl Stump {
    pub fn new() -> Self {
        Stump { roots: Vec::new(), num_leaves: 0 }
    }

    fn set_root(&mut self, height: usize, val: Option<Hash>) {
        if height >= self.roots.len() {
            self.roots.resize(height + 1, None);
        }
        self.roots[height] = val;
    }

    fn root_at(&self, height: usize) -> Option<Hash> {
        self.roots.get(height).copied().flatten()
    }

    /// Append one leaf (binary-counter carry: merge equal-height roots upward).
    pub fn add(&mut self, leaf: Hash) {
        let mut node = leaf;
        let mut h = 0usize;
        while self.root_at(h).is_some() {
            // existing root is the LEFT child of the merged parent, the incoming node is RIGHT.
            let existing = self.root_at(h).unwrap();
            node = parent(&existing, &node);
            self.set_root(h, None);
            h += 1;
        }
        self.set_root(h, Some(node));
        self.num_leaves += 1;
    }

    /// Verify a leaf is committed: fold to its root and match the root at that height.
    pub fn verify(&self, proof: &Proof) -> bool {
        self.root_at(proof.siblings.len()) == Some(proof.compute_root())
    }

    /// The (offset, height) of the tree containing global leaf position `pos`.
    fn tree_of(&self, pos: u64) -> (u64, usize) {
        let mut offset = 0u64;
        for h in (0..u64::BITS as usize).rev() {
            if (self.num_leaves >> h) & 1 == 1 {
                let size = 1u64 << h;
                if pos >= offset && pos < offset + size {
                    return (offset, h);
                }
                offset += size;
            }
        }
        panic!("position {pos} out of range for {} leaves", self.num_leaves);
    }

    /// Fold `leaf` (at local `position`) up through `siblings` to the subtree root.
    fn fold(position: u64, leaf: Hash, siblings: &[Hash]) -> Hash {
        let mut node = leaf;
        let mut pos = position;
        for s in siblings {
            node = if pos & 1 == 0 { parent(&node, s) } else { parent(s, &node) };
            pos >>= 1;
        }
        node
    }

    /// Remove the rightmost leaf (position `num_leaves - 1`), given its inclusion proof. Because
    /// the rightmost leaf is the right child at every level, its path siblings ARE the roots of
    /// the perfect left-subtrees that survive it — so removing it just re-exposes them. Removing
    /// the smallest tree's last leaf turns `n = …1000₂` into `n-1 = …0111₂`, so heights `0..h`
    /// (previously empty) become those subtree roots and height `h` clears — no root collisions.
    fn remove_rightmost(&mut self, proof_last: &Proof) {
        let h = proof_last.siblings.len();
        debug_assert_eq!(proof_last.position, (1u64 << h) - 1, "not the rightmost leaf");
        self.set_root(h, None);
        for (j, sib) in proof_last.siblings.iter().enumerate() {
            self.set_root(j, Some(*sib));
        }
        self.num_leaves -= 1;
    }

    /// Delete the coin at global position `i` via swap-and-shrink: move the current rightmost coin
    /// into slot `i`, then drop the rightmost. Both proofs are inclusion proofs against the CURRENT
    /// roots (the bridge supplies them in the running state; for a block, spends are applied in a
    /// fixed order, each proof against the state just before it). Returns false on a bad proof.
    ///
    /// This is exactly the `Forest` operation `leaves.swap(i, last); leaves.pop()`, done with only
    /// the roots + the two paths.
    pub fn delete(&mut self, i: u64, proof_i: &Proof, proof_last: &Proof) -> bool {
        if !self.verify(proof_i) {
            return false;
        }
        let last = self.num_leaves - 1;
        if i == last {
            self.remove_rightmost(proof_i); // deleting the rightmost itself
            return true;
        }
        if !self.verify(proof_last) {
            return false;
        }
        let l_hash = proof_last.leaf;
        let (off_i, h_i) = self.tree_of(i);
        let (off_last, _) = self.tree_of(last);

        if off_i != off_last {
            // Disjoint trees. Overwrite i's slot with L (recompute i's whole tree), drop rightmost.
            let new_root = Self::fold(proof_i.position, l_hash, &proof_i.siblings);
            self.set_root(h_i, Some(new_root));
            self.remove_rightmost(proof_last);
        } else {
            // Same (smallest) tree. Shrink first — that exposes the left-subtrees as roots — then
            // place L into whichever exposed subtree slot `i` fell into.
            self.remove_rightmost(proof_last);
            let px = proof_i.position; // i's local position in the pre-shrink tree of height h_i
            // The surviving subtree holding i has height j = index of the highest 0-bit of px
            // (px < 2^{h_i}-1, so a 0-bit exists). i's path within it is the low j siblings.
            let mut j = 0usize;
            for b in (0..h_i).rev() {
                if (px >> b) & 1 == 0 {
                    j = b;
                    break;
                }
            }
            let local = px & ((1u64 << j) - 1);
            let new_root = Self::fold(local, l_hash, &proof_i.siblings[..j]);
            self.set_root(j, Some(new_root));
        }
        true
    }
}

// ------------------------------------------------------------------ Forest (bridge oracle) ----

/// The full accumulator: every leaf, in insertion order. Regenerates the exact same roots as a
/// Stump, and can produce an inclusion [`Proof`] for any leaf. Host/bridge side only.
#[derive(Clone, Debug, Default)]
pub struct Forest {
    pub leaves: Vec<Hash>,
}

impl Forest {
    pub fn new() -> Self {
        Forest { leaves: Vec::new() }
    }

    pub fn add(&mut self, leaf: Hash) {
        self.leaves.push(leaf);
    }

    /// Swap-and-shrink delete (ground-truth semantics): move the last leaf into slot `i`, drop the
    /// last. The [`Stump::delete`] above must reproduce the resulting roots from proofs alone.
    pub fn delete(&mut self, i: usize) {
        let last = self.leaves.len() - 1;
        self.leaves.swap(i, last);
        self.leaves.pop();
    }

    /// The (offset, height) span of each perfect tree, largest first — one per set bit of the
    /// leaf count, laid out left to right over `leaves`.
    fn trees(&self) -> Vec<(usize, usize)> {
        let n = self.leaves.len();
        let mut out = Vec::new();
        let mut offset = 0usize;
        for h in (0..usize::BITS as usize).rev() {
            if (n >> h) & 1 == 1 {
                out.push((offset, h));
                offset += 1 << h;
            }
        }
        out
    }

    /// Merkle root of a perfect subtree covering `leaves[offset .. offset + 2^height]`.
    fn subtree_root(&self, offset: usize, height: usize) -> Hash {
        let mut level: Vec<Hash> = self.leaves[offset..offset + (1 << height)].to_vec();
        while level.len() > 1 {
            level = level.chunks(2).map(|c| parent(&c[0], &c[1])).collect();
        }
        level[0]
    }

    /// Roots as a height-indexed vector — must equal the corresponding [`Stump::roots`].
    pub fn roots(&self) -> Vec<Option<Hash>> {
        let mut roots = vec![None; (self.leaves.len().max(1)).next_power_of_two().trailing_zeros() as usize + 1];
        for (offset, height) in self.trees() {
            if height >= roots.len() {
                roots.resize(height + 1, None);
            }
            roots[height] = Some(self.subtree_root(offset, height));
        }
        roots
    }

    /// Inclusion proof for the leaf at global index `index`.
    pub fn prove(&self, index: usize) -> Proof {
        // Find the containing tree.
        let (offset, height) = self
            .trees()
            .into_iter()
            .find(|&(off, h)| index >= off && index < off + (1 << h))
            .expect("index out of range");
        let local = index - offset;

        // Walk up the subtree collecting siblings.
        let mut level: Vec<Hash> = self.leaves[offset..offset + (1 << height)].to_vec();
        let mut pos = local;
        let mut siblings = Vec::with_capacity(height);
        while level.len() > 1 {
            let sib = if pos & 1 == 0 { level[pos + 1] } else { level[pos - 1] };
            siblings.push(sib);
            level = level.chunks(2).map(|c| parent(&c[0], &c[1])).collect();
            pos >>= 1;
        }
        Proof { leaf: self.leaves[index], position: local as u64, siblings }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leaf(i: u64) -> Hash {
        hash_leaf(&i.to_le_bytes())
    }

    // Deterministic pseudo-random walk so failures reproduce without an RNG crate.
    fn splitmix(state: &mut u64) -> u64 {
        *state = state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = *state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }

    #[test]
    fn stump_and_forest_agree_on_roots() {
        // Adding the same leaves must yield identical roots at every population count.
        let mut stump = Stump::new();
        let mut forest = Forest::new();
        for i in 0..300u64 {
            let l = leaf(i);
            stump.add(l);
            forest.add(l);
            // normalise trailing None so the vectors compare regardless of length padding
            let mut a = stump.roots.clone();
            let mut b = forest.roots();
            while a.last() == Some(&None) { a.pop(); }
            while b.last() == Some(&None) { b.pop(); }
            assert_eq!(a, b, "roots diverged at n={}", i + 1);
            assert_eq!(stump.num_leaves, forest.leaves.len() as u64);
        }
    }

    #[test]
    fn every_leaf_proves_against_the_stump() {
        // For many forest sizes, every leaf's Forest-generated proof must verify against the Stump.
        for n in 1..=64usize {
            let mut stump = Stump::new();
            let mut forest = Forest::new();
            for i in 0..n as u64 {
                let l = leaf(i);
                stump.add(l);
                forest.add(l);
            }
            for idx in 0..n {
                let p = forest.prove(idx);
                assert!(stump.verify(&p), "n={n} idx={idx} failed to verify");
                assert_eq!(p.leaf, leaf(idx as u64));
            }
        }
    }

    #[test]
    fn wrong_leaf_or_tampered_proof_is_rejected() {
        let mut stump = Stump::new();
        let mut forest = Forest::new();
        for i in 0..37u64 {
            let l = leaf(i);
            stump.add(l);
            forest.add(l);
        }
        // A genuine proof, then tamper each way.
        let good = forest.prove(20);
        assert!(stump.verify(&good));

        let mut wrong_leaf = good.clone();
        wrong_leaf.leaf = leaf(999);
        assert!(!stump.verify(&wrong_leaf), "forged leaf accepted");

        if !good.siblings.is_empty() {
            let mut bad_sib = good.clone();
            bad_sib.siblings[0][0] ^= 0xFF;
            assert!(!stump.verify(&bad_sib), "tampered sibling accepted");
        }

        let mut wrong_pos = good.clone();
        wrong_pos.position ^= 1; // flip L/R at the bottom
        assert!(!stump.verify(&wrong_pos), "wrong position accepted");
    }

    // Normalise trailing None padding so two root vectors compare by content.
    fn norm(mut v: Vec<Option<Hash>>) -> Vec<Option<Hash>> {
        while v.last() == Some(&None) { v.pop(); }
        v
    }

    #[test]
    fn exhaustive_single_delete_matches_forest() {
        // For every size and every deletable index: Stump.delete (roots+proofs only) must yield the
        // exact roots the Forest oracle produces by swap-and-shrink.
        for n in 1..=40u64 {
            for i in 0..n {
                let mut stump = Stump::new();
                let mut forest = Forest::new();
                for k in 0..n {
                    let l = leaf(k * 1000 + n); // vary so distinct sizes have distinct leaves
                    stump.add(l);
                    forest.add(l);
                }
                let proof_last = forest.prove((n - 1) as usize);
                let proof_i = forest.prove(i as usize);
                assert!(stump.delete(i, &proof_i, &proof_last), "n={n} i={i} delete rejected");
                forest.delete(i as usize);
                assert_eq!(norm(stump.roots.clone()), norm(forest.roots()), "roots mismatch n={n} i={i}");
                assert_eq!(stump.num_leaves, forest.leaves.len() as u64, "count mismatch n={n} i={i}");
                // and the accumulator is still coherent: every survivor still proves
                for idx in 0..forest.leaves.len() {
                    assert!(stump.verify(&forest.prove(idx)), "survivor idx={idx} lost after n={n} i={i}");
                }
            }
        }
    }

    #[test]
    fn double_spend_is_rejected_after_delete() {
        // Once a coin is deleted, its OLD proof must no longer verify (can't spend twice).
        let mut stump = Stump::new();
        let mut forest = Forest::new();
        for k in 0..29u64 {
            let l = leaf(k);
            stump.add(l);
            forest.add(l);
        }
        let victim = 11usize;
        let stale = forest.prove(victim); // proof captured before deletion
        assert!(stump.verify(&stale));
        let proof_last = forest.prove(28);
        let proof_i = forest.prove(victim);
        assert!(stump.delete(victim as u64, &proof_i, &proof_last));
        assert!(!stump.verify(&stale), "stale proof still verified — double-spend possible");
    }

    #[test]
    fn sequential_block_of_spends_matches_forest() {
        // Simulate a block: delete a set of coins one at a time, each proof against the running
        // state (as a bridge node would supply). Roots must track the Forest at every step.
        let mut seed = 0x5EED_1234u64;
        for _round in 0..120 {
            let n = (splitmix(&mut seed) % 200 + 5) as usize;
            let mut stump = Stump::new();
            let mut forest = Forest::new();
            for k in 0..n as u64 {
                let l = leaf(k ^ ((n as u64) << 16));
                stump.add(l);
                forest.add(l);
            }
            let spends = (splitmix(&mut seed) as usize) % n; // how many to remove this block
            for _ in 0..spends {
                let cur = forest.leaves.len();
                if cur == 0 { break; }
                let i = (splitmix(&mut seed) as usize) % cur;
                let proof_last = forest.prove(cur - 1);
                let proof_i = forest.prove(i);
                assert!(stump.delete(i as u64, &proof_i, &proof_last), "n={n} i={i} cur={cur}");
                forest.delete(i);
                assert_eq!(norm(stump.roots.clone()), norm(forest.roots()), "diverged n={n} cur={cur} i={i}");
            }
        }
    }

    #[test]
    fn add_after_delete_stays_coherent() {
        // Blocks both spend and create coins: interleave deletes and adds, track the Forest.
        let mut seed = 0xABCD_0001u64;
        let mut stump = Stump::new();
        let mut forest = Forest::new();
        let mut created = 0u64;
        for _ in 0..2000 {
            let cur = forest.leaves.len();
            let do_add = cur == 0 || splitmix(&mut seed) % 2 == 0;
            if do_add {
                let l = leaf(0xF00D_0000 + created);
                created += 1;
                stump.add(l);
                forest.add(l);
            } else {
                let i = (splitmix(&mut seed) as usize) % cur;
                let proof_last = forest.prove(cur - 1);
                let proof_i = forest.prove(i);
                assert!(stump.delete(i as u64, &proof_i, &proof_last));
                forest.delete(i);
            }
            assert_eq!(norm(stump.roots.clone()), norm(forest.roots()), "diverged at cur={cur}");
        }
    }

    #[test]
    fn fuzz_random_sizes_roots_and_proofs() {
        let mut seed = 0xCAFEF00Du64;
        for _ in 0..200 {
            let n = (splitmix(&mut seed) % 500 + 1) as usize;
            let mut stump = Stump::new();
            let mut forest = Forest::new();
            for i in 0..n as u64 {
                let l = leaf(i ^ (n as u64) << 8);
                stump.add(l);
                forest.add(l);
            }
            let mut a = stump.roots.clone();
            let mut b = forest.roots();
            while a.last() == Some(&None) { a.pop(); }
            while b.last() == Some(&None) { b.pop(); }
            assert_eq!(a, b, "roots diverged at n={n}");
            // spot-check a random leaf
            let idx = (splitmix(&mut seed) as usize) % n;
            assert!(stump.verify(&forest.prove(idx)), "n={n} idx={idx}");
        }
    }
}

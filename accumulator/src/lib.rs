//! Utreexo hash-forest UTXO accumulator — the piece that makes Hazync block proofs *stateless*
//! (HAZYNC_ARCHITECTURE.md §2.3). The prover never carries the ~10 GB UTXO set; it carries a
//! tiny root set and is handed per-input **inclusion proofs** by a bridge node.
//!
//! Two halves, deliberately separated:
//!   * [`Forest`] — the full accumulator. Host-side "bridge" oracle: holds every node, generates
//!     inclusion proofs. NEVER runs in the zkVM. This is our ground-truth reference.
//!   * [`Stump`]  — roots only. The `verify`/`update` logic here is the MODEL for the guest's
//!     `prover/methods/guest/src/utreexo.rs`, which adds the SEC-2 hardening the guest needs against a
//!     malicious prover (pinning `proof_i.position == i - offset` and that `proof_last` is the true
//!     rightmost) that this reference oracle does not — the host only ever builds honest proofs. The
//!     proven guest is the authority; treat this `Stump` as the readable spec, not a byte-for-byte copy.
//!
//! A forest of `n` leaves is a set of perfect binary Merkle trees, one per set bit of `n`
//! (n = 5 = 0b101 → a tree of 4 leaves and a tree of 1). Roots are indexed by tree *height*;
//! because a forest's leaf count has distinct bits, at most one tree exists per height — so a
//! proof's height uniquely identifies which root it must match.

use sha2::{Digest, Sha256};

pub type Hash = [u8; 32];

/// Domain-separation tags. A leaf hash and an interior hash MUST be drawn from disjoint domains, or
/// a value can be valid as both — the classic Merkle type-confusion, where an attacker presents an
/// interior node as a leaf (or vice versa) and produces a second valid path to the same root.
///
/// This was previously left implicit and documented as such in `SECURITY.md`: leaf preimages always
/// begin with a txid the prover cannot control, so the domains could not collide *in practice*. That
/// is a soundness argument resting on the shape of the data rather than on the hash construction, and
/// it is the accumulator — the one non-Core component in the system, and the piece most likely to be
/// challenged in review — that carried it. Making it explicit costs one byte per hash and turns an
/// argument into a property.
///
/// The tags MUST match `prover/methods/guest/src/utreexo.rs` byte for byte; `scripts/check-utreexo.sh`
/// fails the build if they drift.
pub const TAG_LEAF: u8 = 0x00;
pub const TAG_NODE: u8 = 0x01;

/// Interior node hash: SHA256(TAG_NODE || left || right). In the guest this same op routes through
/// the RISC0 SHA accelerator — bit-identical, so the logic developed here transfers unchanged.
pub fn parent(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update([TAG_NODE]);
    h.update(left);
    h.update(right);
    h.finalize().into()
}

/// Leaf commitment for a UTXO: SHA256(TAG_LEAF || data). `data` is the caller's canonical
/// serialization of the coin (outpoint + height/coinbase flag + CTxOut). Opaque here; the accumulator
/// only hashes it.
/// The UTXO-set leaf preimage: the bytes a coin is committed as.
///
/// THIS EXISTS IN THREE IMPLEMENTATIONS and they must agree byte for byte — this one, the guest's
/// C++ `coin_leaf` (spend side) and its `tx_out_leaves` (create side), both in
/// `prover/methods/guest/verify_input.cpp`. A drift is unrecoverable: every proof made against a
/// broken commitment is worthless and the only repair is re-proving from genesis.
///
/// It lives HERE, in the library, rather than in the host binary, so `leaf-differential` can call the
/// real function instead of a re-typed copy. A differential test against a duplicate proves only that
/// the duplicate is self-consistent.
///
/// `scripts/check-utreexo.sh` gates the construction textually; `leaf-differential` gates the BYTES.
///
/// The `scriptPubKey` length prefix (N2) is what keeps the preimage injective: without it the
/// concatenation of a variable-length script and the fixed fields after it is ambiguous, and two
/// different coins can produce one preimage — a collision in a UTXO commitment being a coin that can
/// be spent twice.
pub fn coin_leaf(txid_internal: &[u8; 32], vout: u32, value_sat: u64, spk: &[u8],
                 height: u32, is_coinbase: bool, coin_mtp: u32) -> Hash {
    let mut b = Vec::with_capacity(57 + spk.len());
    b.extend_from_slice(txid_internal);
    b.extend_from_slice(&vout.to_le_bytes());
    b.extend_from_slice(&value_sat.to_le_bytes());
    b.extend_from_slice(&(spk.len() as u32).to_le_bytes());
    b.extend_from_slice(spk);
    b.extend_from_slice(&height.to_le_bytes());
    b.push(is_coinbase as u8);
    b.extend_from_slice(&coin_mtp.to_le_bytes());
    hash_leaf(&b)
}

pub fn hash_leaf(data: &[u8]) -> Hash {
    let mut h = Sha256::new();
    h.update([TAG_LEAF]);
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

    /// The (offset, height) of the tree containing global leaf position `pos`, or `None` when `pos`
    /// is outside the forest.
    ///
    /// This USED TO PANIC, and the panic was reachable from untrusted input: `delete`'s `i` is an
    /// independent argument that `verify` does not constrain, so a perfectly well-formed inclusion
    /// proof paired with an out-of-range index aborted the process. Fail-closed in the guest, but a
    /// remote abort of the host/coordinator verification subprocess otherwise (audit 2026-08-01, L-2).
    fn tree_of(&self, pos: u64) -> Option<(u64, usize)> {
        let mut offset = 0u64;
        for h in (0..u64::BITS as usize).rev() {
            if (self.num_leaves >> h) & 1 == 1 {
                let size = 1u64 << h;
                if pos >= offset && pos < offset + size {
                    return Some((offset, h));
                }
                offset += size;
            }
        }
        None
    }

    /// Shape precondition for [`Self::remove_rightmost`], checked BEFORE any mutation.
    ///
    /// The rightmost leaf is the right child at every level, so its local position is all-ones for
    /// its height. This was only a `debug_assert!` inside `remove_rightmost` — i.e. absent in release
    /// builds, which is where it mattered. Hoisted out so a rejected delete leaves the accumulator
    /// UNTOUCHED: the disjoint-tree branch sets a root before removing the rightmost, and discovering
    /// a malformed proof at that point would leave a corrupted Stump behind a `false` return.
    fn rightmost_ok(&self, p: &Proof) -> bool {
        let h = p.siblings.len();
        h < u64::BITS as usize && self.num_leaves > 0 && p.position == (1u64 << h) - 1
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
    /// EVERY rejection path returns before mutating. `delete` returning `false` must leave the
    /// accumulator exactly as it was — a partially-applied delete would silently corrupt the roots
    /// while the caller believed nothing happened, which is worse than the panic this replaces.
    pub fn delete(&mut self, i: u64, proof_i: &Proof, proof_last: &Proof) -> bool {
        // Nothing to delete, and `num_leaves - 1` below would underflow.
        if self.num_leaves == 0 {
            return false;
        }
        // `i` is an INDEPENDENT argument: verifying proof_i constrains the leaf and its path, not the
        // index. Without this, an out-of-range `i` reached tree_of and panicked.
        if i >= self.num_leaves {
            return false;
        }
        if !self.verify(proof_i) {
            return false;
        }
        let last = self.num_leaves - 1;
        if i == last {
            if !self.rightmost_ok(proof_i) {
                return false;
            }
            self.remove_rightmost(proof_i); // deleting the rightmost itself
            return true;
        }
        if !self.verify(proof_last) {
            return false;
        }
        if !self.rightmost_ok(proof_last) {
            return false;
        }
        let (off_i, h_i) = match self.tree_of(i) {
            Some(t) => t,
            None => return false,
        };
        let (off_last, _) = match self.tree_of(last) {
            Some(t) => t,
            None => return false,
        };
        let l_hash = proof_last.leaf;

        if off_i != off_last {
            // Disjoint trees. Overwrite i's slot with L (recompute i's whole tree), drop rightmost.
            let new_root = Self::fold(proof_i.position, l_hash, &proof_i.siblings);
            self.set_root(h_i, Some(new_root));
            self.remove_rightmost(proof_last);
        } else {
            // Same (smallest) tree. Shrink first — that exposes the left-subtrees as roots — then
            // place L into whichever exposed subtree slot `i` fell into.
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
            // `j` is derived from h_i — i.e. from `i` — while the siblings come from the proof. A
            // proof that verified at a DIFFERENT height than i's tree can drive j past the end of the
            // slice, which was an out-of-range panic. Computed and checked BEFORE the shrink so this
            // rejection still mutates nothing.
            if j > proof_i.siblings.len() {
                return false;
            }
            self.remove_rightmost(proof_last);
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

    /// Cached internal nodes. `internals[k]` is tree level `k + 1`; `internals[k][i]` is the parent of
    /// `level(k)[2i]` and `level(k)[2i + 1]`. Length invariant: `internals[k].len() == level(k).len() / 2`.
    ///
    /// WHY THIS EXISTS. Storing only leaves makes `prove` and `roots` look like algorithm problems and
    /// they are not: with no internal nodes cached, producing one sibling at level `k` means hashing the
    /// `2^k` leaves under it, and summing over a proof gives `2^0 + 2^1 + ... + 2^(h-1) = 2^h - 1` — the
    /// whole subtree. That is the *information-theoretic minimum* for leaves-only storage, so the old
    /// walk was already optimal for the structure it had. The structure was the problem. At 1.6M leaves
    /// it meant ~810,000 hashes to collect ~20 siblings, twice per input, plus a full rebuild for every
    /// `roots()` call — of which the bridge makes two per block.
    ///
    /// This costs one extra hash per leaf ever added (~2n hashes total, amortised O(1) per add) and
    /// n more hashes of memory, and makes `prove` O(log n) and `roots` O(popcount).
    ///
    /// The flat pairing is safe across the forest's perfect subtrees because a subtree of height `h`
    /// always begins at an offset that is a multiple of `2^h`. For any `k <= h` the pairs at level `k`
    /// therefore never straddle a subtree boundary, so a single flat level array serves every tree.
    internals: Vec<Vec<Hash>>,

    /// leaf hash -> its positions, ascending. Replaces the bridge's `leaves.iter().position(...)`.
    ///
    /// This was measured as NOT worth having (GOALS.md §G2, "Ruled out"): an A/B on 100 real blocks
    /// gave 291 vs 285 blocks/hr, because the scan was ~4% of a 12.4 s block. That measurement was
    /// correct and the conclusion was correct *at the time*. Caching internal nodes then removed 94%
    /// of the bridge's work, and a `perf` profile of the result put 71% of the remaining time in the
    /// scan. The same optimisation went from worthless to dominant without changing — what changed was
    /// everything around it.
    ///
    /// Positions are a `Vec` rather than a single index to PRESERVE `position()`'s first-match
    /// semantics exactly, not because duplicates are expected.
    ///
    /// The BIP30 rationale this comment used to give was WRONG, and the correction is worth keeping:
    /// a Hazync leaf commits `height` (and `mtp`), so the historical duplicate-coinbase blocks
    /// (91842 duplicates 91812; 91880 duplicates 91722) produce DISTINCT leaves — `SECURITY.md` F3
    /// says so explicitly, and this comment contradicted it. Byte-identical leaves cannot arise from
    /// that case at all: identical bytes need an identical txid, which needs an identical transaction,
    /// which is in-block duplication that `bip30_ok` already forbids.
    ///
    /// The Vec stays because it is the behaviour-preserving choice — a `Forest` is an oracle whose
    /// job is to match the `Stump` exactly, and narrowing an index to "can't happen" is how an
    /// invariant becomes a silent wrong answer if it ever stops holding. Flagged by external audit #2
    /// (N-1) as a stale rationale that would mislead the next reviewer.
    index: std::collections::HashMap<Hash, Vec<usize>>,
}

impl Forest {
    pub fn new() -> Self {
        Forest { leaves: Vec::new(), internals: Vec::new(), index: Default::default() }
    }

    /// Smallest position holding `leaf`, or `None`. Exactly `leaves.iter().position(|x| *x == leaf)`,
    /// which is what the bridge called and what the duplicate-leaf (BIP30) case depends on.
    pub fn find(&self, leaf: &Hash) -> Option<usize> {
        self.index.get(leaf).and_then(|v| v.first().copied())
    }

    fn index_insert(&mut self, leaf: Hash, pos: usize) {
        let v = self.index.entry(leaf).or_default();
        let at = v.partition_point(|&p| p < pos);
        v.insert(at, pos);
    }

    fn index_remove(&mut self, leaf: &Hash, pos: usize) {
        if let Some(v) = self.index.get_mut(leaf) {
            if let Ok(at) = v.binary_search(&pos) {
                v.remove(at);
            }
            if v.is_empty() {
                self.index.remove(leaf);
            }
        }
    }

    /// Rebuild a forest from a bare leaf vector — the bridge's checkpoint-resume path.
    ///
    /// `internals` is private specifically so this cannot be bypassed: the old code resumed with the
    /// struct literal `Forest { leaves: st.leaves }`, which under the cached representation would
    /// produce a forest whose cache is empty while its leaves are not. Every proof and every root it
    /// then served would be wrong. Making the field private turns that into a compile error rather
    /// than a silent one, and this is the supported way through.
    ///
    /// Builds level by level rather than replaying `add` per leaf — same result, one pass.
    pub fn from_leaves(leaves: Vec<Hash>) -> Self {
        let mut internals: Vec<Vec<Hash>> = Vec::new();
        {
            let mut below: &[Hash] = &leaves;
            while below.len() > 1 {
                let up: Vec<Hash> =
                    below.chunks_exact(2).map(|c| parent(&c[0], &c[1])).collect();
                if up.is_empty() {
                    break;
                }
                internals.push(up);
                below = internals.last().unwrap();
            }
        }
        let mut index: std::collections::HashMap<Hash, Vec<usize>> =
            std::collections::HashMap::with_capacity(leaves.len());
        for (i, l) in leaves.iter().enumerate() {
            index.entry(*l).or_default().push(i);   // ascending by construction
        }
        Forest { leaves, internals, index }
    }

    /// Tree level `k`: level 0 is the leaves, level `k > 0` is `internals[k - 1]`.
    fn level(&self, k: usize) -> &[Hash] {
        if k == 0 { &self.leaves } else { &self.internals[k - 1] }
    }

    fn level_len(&self, k: usize) -> usize {
        if k == 0 { self.leaves.len() } else { self.internals.get(k - 1).map_or(0, |v| v.len()) }
    }

    pub fn add(&mut self, leaf: Hash) {
        // appended at the end, so its position is larger than every existing one for this key
        self.index.entry(leaf).or_default().push(self.leaves.len());
        self.leaves.push(leaf);
        // Completing a pair at level k creates exactly one parent, which may in turn complete a pair
        // one level up. Amortised O(1): a leaf whose index has t trailing ones carries t levels.
        let mut k = 0;
        while self.level_len(k) % 2 == 0 && self.level_len(k) > 0 {
            let n = self.level_len(k);
            let p = parent(&self.level(k)[n - 2], &self.level(k)[n - 1]);
            if self.internals.len() <= k {
                self.internals.push(Vec::new());
            }
            self.internals[k].push(p);
            k += 1;
        }
    }

    /// Swap-and-shrink delete (ground-truth semantics): move the last leaf into slot `i`, drop the
    /// last. The [`Stump::delete`] above must reproduce the resulting roots from proofs alone.
    pub fn delete(&mut self, i: usize) {
        let last = self.leaves.len() - 1;
        let gone = self.leaves[i];
        let moved = self.leaves.pop().expect("delete from empty forest");
        // `i == last` means `gone == moved` and the single index entry has already been accounted
        // for by this one removal — removing twice would drop another copy of a duplicated leaf.
        self.index_remove(&gone, i);
        if i != last {
            self.index_remove(&moved, last);
            self.index_insert(moved, i);
        }

        // Losing a leaf can orphan at most one parent per level, and the shrink cascades upward. Done
        // bottom-up so each level truncates against the already-corrected level below it — this is
        // also what re-shapes the forest when the tree decomposition changes (n=4, one height-2 tree,
        // becomes n=3, a height-1 tree plus a height-0 tree).
        for k in 0..self.internals.len() {
            let want = self.level_len(k) / 2;
            if self.internals[k].len() > want {
                self.internals[k].truncate(want);
            }
        }
        while self.internals.last().is_some_and(|v| v.is_empty()) {
            self.internals.pop();
        }

        if i != last {
            self.leaves[i] = moved;
            self.repair(i);
        }
    }

    /// Recompute the cached ancestors of level-0 index `i`. O(log n).
    fn repair(&mut self, i: usize) {
        let mut idx = i;
        for k in 0..self.internals.len() {
            let pair = idx & !1;
            if pair + 1 >= self.level_len(k) {
                break;                       // no completed pair here, so no parent to fix
            }
            let p = parent(&self.level(k)[pair], &self.level(k)[pair + 1]);
            let pidx = idx / 2;
            if pidx >= self.internals[k].len() {
                break;
            }
            self.internals[k][pidx] = p;
            idx = pidx;
        }
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
    ///
    /// A cache read: the subtree's root is the level-`height` node at index `offset >> height`, which
    /// exists because the tree is perfect and `offset` is a multiple of `2^height`.
    fn subtree_root(&self, offset: usize, height: usize) -> Hash {
        self.level(height)[offset >> height]
    }

    /// Roots as a height-indexed vector — must equal the corresponding [`Stump::roots`].
    ///
    /// O(popcount(n)) — one cache read per set bit. Was O(n): a full rebuild of every subtree, and the
    /// bridge calls this twice per block (`root_prev`, `root_next`).
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

    /// Inclusion proof for the leaf at global index `index`. O(log n).
    pub fn prove(&self, index: usize) -> Proof {
        // Find the containing tree.
        let (offset, height) = self
            .trees()
            .into_iter()
            .find(|&(off, h)| index >= off && index < off + (1 << h))
            .expect("index out of range");
        let local = index - offset;

        // Walk up reading cached siblings. The level-`k` node above global leaf `index` is at index
        // `index >> k`, so the sibling is `(index >> k) ^ 1` — and within a perfect subtree whose
        // offset is a multiple of its size, that sibling is guaranteed to exist for every k < height.
        let mut siblings = Vec::with_capacity(height);
        for k in 0..height {
            siblings.push(self.level(k)[(index >> k) ^ 1]);
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

    // ── L-2: adversarial proof input must not abort the process (audit 2026-08-01) ───────────────
    //
    // `Stump::delete` takes proofs from untrusted input. It had three reachable panics: `num_leaves
    // - 1` underflowing on an empty stump, `tree_of` panicking on an out-of-range `i` (which
    // `verify` does not constrain, being an independent argument), and a `siblings[..j]` slice whose
    // bound is derived from `i` while the slice comes from the proof.
    //
    // Fail-closed in the guest, but in the host/coordinator it aborts the verification subprocess.
    //
    // Asserted as a PROPERTY rather than three hand-built cases, because hand-building the
    // height-mismatch case requires constructing a proof that verifies at the wrong height — easy to
    // get subtly wrong, and a test that silently stops reaching the path proves nothing.
    //
    // TO RE-RUN THE POSITIVE CONTROL (do this if you change delete/tree_of, because these four tests
    // are worthless the moment they stop reaching the paths):
    //   1. delete the `if i >= self.num_leaves { return false; }` guard in `delete`;
    //   2. replace `None` at the end of `tree_of` with the original
    //      `panic!("position {pos} out of range for {} leaves", self.num_leaves);`.
    // All FOUR tests below must fail. Verified 2026-08-02 — and the first draft of them passed this
    // control, because fully random proofs never got past `verify()`. That is why they build genuine
    // proofs and perturb one field instead.

    /// Adversarial (i, proof_i, proof_last) triples that still get PAST `verify`.
    ///
    /// Fully random proofs are rejected by `verify` immediately, so a test built from them never
    /// reaches tree_of or the sibling slice and passes against the unfixed code — an earlier version
    /// of these tests did exactly that, and the positive control is what exposed it. So: start from
    /// GENUINE proofs and perturb one field, which keeps them plausible while making the index or the
    /// sibling count disagree with what they attest.
    fn adversarial_case(f: &Forest, n: u64, st: &mut u64) -> (u64, Proof, Proof) {
        let a = (splitmix(st) % n) as usize;
        let b = (splitmix(st) % n) as usize;
        let mut pi = f.prove(a);
        let mut pl = f.prove(b);
        match splitmix(st) % 5 {
            0 => {}                                            // genuine, wrong index only
            1 => pi.position = splitmix(st) % 64,
            2 => {
                let k = (splitmix(st) as usize) % (pi.siblings.len() + 1);
                pi.siblings.truncate(k);                        // drives j past the slice end
            }
            3 => pl.position = splitmix(st) % 64,
            _ => pl.siblings.truncate((splitmix(st) as usize) % (pl.siblings.len() + 1)),
        }
        (splitmix(st) % (n * 2 + 4), pi, pl)                    // frequently out of range
    }

    #[test]
    fn delete_never_panics_on_arbitrary_proof_input() {
        let mut st = 0x243F6A8885A308D3u64;
        for n in 1..40u64 {
            let (mut f, mut base) = (Forest::new(), Stump::new());
            for k in 0..n {
                f.add(leaf(k));
                base.add(leaf(k));
            }
            for _ in 0..300 {
                let mut s = base.clone();
                let (i, pi, pl) = adversarial_case(&f, n, &mut st);
                let _ = s.delete(i, &pi, &pl); // the assertion is that this line returns at all
            }
        }
    }

    #[test]
    fn a_rejected_delete_mutates_nothing() {
        // Worse than the panic would be a delete that half-applies and then reports false: the
        // caller believes nothing happened while the roots have already moved.
        let mut st = 0x9E3779B97F4A7C15u64;
        for n in 1..40u64 {
            let (mut f, mut base) = (Forest::new(), Stump::new());
            for k in 0..n {
                f.add(leaf(k));
                base.add(leaf(k));
            }
            for _ in 0..300 {
                let mut s = base.clone();
                let (i, pi, pl) = adversarial_case(&f, n, &mut st);
                if !s.delete(i, &pi, &pl) {
                    assert_eq!(s, base, "a rejected delete left the accumulator modified");
                }
            }
        }
    }

    #[test]
    fn empty_and_out_of_range_deletes_are_refused_not_fatal() {
        let empty_proof = Proof { leaf: leaf(0), position: 0, siblings: vec![] };
        let mut s = Stump::new();
        assert!(!s.delete(0, &empty_proof, &empty_proof), "delete on an empty stump must be refused");
        assert_eq!(s.num_leaves, 0, "a refused delete must not touch num_leaves");

        // A GENUINE proof paired with an out-of-range index: this is the case `verify` cannot catch,
        // because the index is not part of what the proof attests.
        let mut f = Forest::new();
        for k in 0..5u64 {
            f.add(leaf(k));
        }
        let mut s = Stump::new();
        for k in 0..5u64 {
            s.add(leaf(k));
        }
        // BOTH proofs must be genuine, and proof_last must be the REAL rightmost, or the guards
        // ahead of tree_of reject first and the test never reaches the path it exists to cover.
        // Using f.prove(0) for both passed against the unfixed code — it was rejected by
        // rightmost_ok long before the panic. The positive control caught that; it is the reason
        // this reads as it does.
        let good = f.prove(0);
        let rightmost = f.prove(4);
        assert!(s.verify(&good), "the proof itself is valid — the index is what is wrong");
        assert!(s.verify(&rightmost), "and so is the rightmost proof");
        let before = s.clone();
        assert!(!s.delete(999, &good, &rightmost), "an out-of-range index must be refused");
        assert_eq!(s, before, "the refusal must not have mutated anything");

        // Guard against the test going vacuous: the same machinery must still accept a real delete.
        let last = f.prove(4);
        assert!(s.delete(4, &last, &last), "a legitimate delete must still succeed");
        assert_eq!(s.num_leaves, 4);
    }

    #[test]
    fn tree_of_reports_out_of_range_instead_of_panicking() {
        let mut s = Stump::new();
        for k in 0..5u64 {
            s.add(leaf(k));
        }
        assert!(s.tree_of(0).is_some());
        assert!(s.tree_of(4).is_some());
        assert!(s.tree_of(5).is_none(), "one past the end is out of range");
        assert!(s.tree_of(u64::MAX).is_none());
        assert!(Stump::new().tree_of(0).is_none(), "nothing is in range in an empty forest");
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

#[cfg(test)]
mod cached_internals_equivalence {
    //! The cached-internals `Forest` must be indistinguishable from the leaves-only one it replaced.
    //!
    //! `Stump::verify` recomputes the root from a proof and compares, so a wrong proof here fails
    //! CLOSED — a block would refuse to prove rather than a bad proof being accepted. That bounds the
    //! blast radius but does not make it cheap: a divergence at scale stalls the bridge. So the old
    //! implementation is kept here verbatim as a reference oracle and diffed against, rather than
    //! trusting that the rewrite "looks right".
    use super::*;

    /// The pre-cache implementations, verbatim. Reference only.
    fn naive_subtree_root(leaves: &[Hash], offset: usize, height: usize) -> Hash {
        let mut level: Vec<Hash> = leaves[offset..offset + (1 << height)].to_vec();
        while level.len() > 1 {
            level = level.chunks(2).map(|c| parent(&c[0], &c[1])).collect();
        }
        level[0]
    }

    fn naive_trees(n: usize) -> Vec<(usize, usize)> {
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

    fn naive_roots(leaves: &[Hash]) -> Vec<Option<Hash>> {
        let mut roots =
            vec![None; (leaves.len().max(1)).next_power_of_two().trailing_zeros() as usize + 1];
        for (offset, height) in naive_trees(leaves.len()) {
            if height >= roots.len() {
                roots.resize(height + 1, None);
            }
            roots[height] = Some(naive_subtree_root(leaves, offset, height));
        }
        roots
    }

    fn naive_prove(leaves: &[Hash], index: usize) -> Proof {
        let (offset, height) = naive_trees(leaves.len())
            .into_iter()
            .find(|&(off, h)| index >= off && index < off + (1 << h))
            .expect("index out of range");
        let local = index - offset;
        let mut level: Vec<Hash> = leaves[offset..offset + (1 << height)].to_vec();
        let mut pos = local;
        let mut siblings = Vec::with_capacity(height);
        while level.len() > 1 {
            let sib = if pos & 1 == 0 { level[pos + 1] } else { level[pos - 1] };
            siblings.push(sib);
            level = level.chunks(2).map(|c| parent(&c[0], &c[1])).collect();
            pos >>= 1;
        }
        Proof { leaf: leaves[index], position: local as u64, siblings }
    }

    fn lf(i: u64) -> Hash {
        hash_leaf(&i.to_le_bytes())
    }

    fn mix(state: &mut u64) -> u64 {
        *state = state.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = *state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }

    /// Every leaf count from 1..=512: roots agree, and EVERY index proves identically.
    /// Exhaustive over sizes, because the tree decomposition changes shape at each power of two and a
    /// bug that only appears at one boundary is exactly what a sampled test would miss.
    #[test]
    fn exhaustive_sizes_add_only() {
        let mut f = Forest::new();
        for n in 1..=512u64 {
            f.add(lf(n));
            assert_eq!(f.roots(), naive_roots(&f.leaves), "roots differ at n={n}");
            for i in 0..f.leaves.len() {
                assert_eq!(f.prove(i), naive_prove(&f.leaves, i), "proof differs at n={n} i={i}");
            }
        }
    }

    /// Interleaved adds and deletes — the path that actually runs in the bridge, where a delete
    /// follows every input and re-shapes the forest under the next proof.
    #[test]
    fn random_add_delete_walk_matches_reference() {
        let mut st = 0x5EED_1234_u64;
        let mut f = Forest::new();
        let mut added = 0u64;
        for step in 0..4000 {
            let n = f.leaves.len();
            if n == 0 || mix(&mut st) % 3 != 0 {
                added += 1;
                f.add(lf(added));
            } else {
                f.delete((mix(&mut st) as usize) % n);
            }
            assert_eq!(f.roots(), naive_roots(&f.leaves), "roots differ at step {step}");
            if !f.leaves.is_empty() {
                // proving every index every step is O(n^2); probe the ends and a random interior one,
                // and prove everything on the small sizes where exhaustive is affordable.
                let n = f.leaves.len();
                let mut probes = vec![0, n - 1, (mix(&mut st) as usize) % n];
                if n <= 40 {
                    probes = (0..n).collect();
                }
                for i in probes {
                    assert_eq!(f.prove(i), naive_prove(&f.leaves, i), "proof differs step {step} i={i}");
                }
            }
        }
    }

    /// The cache must never disagree with a rebuild-from-leaves, and the invariant
    /// `internals[k].len() == level(k).len() / 2` must hold after every operation.
    #[test]
    fn internal_invariant_holds_through_deletes() {
        let mut st = 0xC0FFEE_u64;
        let mut f = Forest::new();
        for i in 0..1000u64 {
            f.add(lf(i));
        }
        for _ in 0..900 {
            let n = f.leaves.len();
            f.delete((mix(&mut st) as usize) % n);
            for k in 0..f.internals.len() {
                let below = if k == 0 { f.leaves.len() } else { f.internals[k - 1].len() };
                assert_eq!(f.internals[k].len(), below / 2, "invariant broken at level {}", k + 1);
            }
            assert_eq!(f.roots(), naive_roots(&f.leaves));
        }
    }

    /// A proof from the cached forest must still VERIFY against a Stump driven in lockstep — the
    /// property the bridge actually depends on, as opposed to merely matching the old code.
    #[test]
    fn proofs_verify_against_lockstep_stump() {
        let mut st = 0xABCD_EF01_u64;
        let mut f = Forest::new();
        let mut s = Stump::new();
        let mut added = 0u64;
        for _ in 0..600 {
            added += 1;
            let l = lf(added);
            f.add(l);
            s.add(l);
        }
        for _ in 0..300 {
            let n = f.leaves.len();
            let i = (mix(&mut st) as usize) % n;
            let last = n - 1;
            let pi = f.prove(i);
            let pl = f.prove(last);
            assert!(s.verify(&pi), "stump rejected a proof the cached forest produced");
            assert!(s.verify(&pl), "stump rejected the rightmost proof");
            assert!(s.delete(i as u64, &pi, &pl), "stump refused a delete built from cached-forest proofs");
            f.delete(i);
            // Trailing `None`s are a representation artifact, not state: the two vectors are sized
            // by different rules (Stump grows on demand, Forest sizes from the leaf count), so one
            // can carry padding the other does not. `normalize_roots` exists in the rangestate crate
            // for exactly this. Compare the meaningful prefix.
            let trim = |mut v: Vec<Option<Hash>>| {
                while v.last() == Some(&None) {
                    v.pop();
                }
                v
            };
            assert_eq!(
                trim(s.roots.clone()),
                trim(f.roots()),
                "stump and forest diverged after delete"
            );
        }
    }
}

#[cfg(test)]
mod from_leaves_tests {
    use super::*;

    #[test]
    fn from_leaves_matches_incremental_adds_at_every_size() {
        // The checkpoint-resume path must produce a forest indistinguishable from one grown by adds,
        // or the bridge silently serves wrong proofs for everything after a restart.
        for n in 0..=600u64 {
            let leaves: Vec<Hash> = (0..n).map(|i| hash_leaf(&i.to_le_bytes())).collect();
            let mut grown = Forest::new();
            for l in &leaves {
                grown.add(*l);
            }
            let loaded = Forest::from_leaves(leaves);
            assert_eq!(loaded.internals, grown.internals, "cache differs at n={n}");
            assert_eq!(loaded.roots(), grown.roots(), "roots differ at n={n}");
            for i in 0..(n as usize) {
                assert_eq!(loaded.prove(i), grown.prove(i), "proof differs at n={n} i={i}");
            }
        }
    }
}

#[cfg(test)]
mod position_index_tests {
    //! `find` must be indistinguishable from the linear scan it replaces — INCLUDING when the same
    //! leaf appears twice. BIP30 permits a coinbase to duplicate an earlier still-unspent one (block
    //! 91842 duplicates 91812) — but note those produce DISTINCT leaves here, since the leaf commits
    //! height; the Vec preserves `position()`'s first-match semantics rather than handling an expected
    //! collision. `position()` returns the
    //! FIRST match. An index that returned "some position holding this leaf" would pick the wrong
    //! coin on exactly those blocks and nowhere else.
    use super::*;

    fn lf(i: u64) -> Hash {
        hash_leaf(&i.to_le_bytes())
    }

    fn mix(s: &mut u64) -> u64 {
        *s = s.wrapping_add(0x9E3779B97F4A7C15);
        let mut z = *s;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
        z ^ (z >> 31)
    }

    /// The property, over a long add/delete walk: for EVERY leaf present, `find` == `position`.
    #[test]
    fn find_matches_linear_scan_through_a_random_walk() {
        let mut st = 0xFEED_BEEFu64;
        let mut f = Forest::new();
        let mut added = 0u64;
        for step in 0..3000 {
            if f.leaves.is_empty() || mix(&mut st) % 3 != 0 {
                added += 1;
                // deliberately draw from a small pool so duplicates arise constantly
                f.add(lf(added % 400));
            } else {
                let n = f.leaves.len();
                f.delete((mix(&mut st) as usize) % n);
            }
            // every distinct leaf currently present
            let mut seen: Vec<Hash> = f.leaves.clone();
            seen.sort();
            seen.dedup();
            for l in &seen {
                let want = f.leaves.iter().position(|x| x == l);
                assert_eq!(f.find(l), want, "find != position at step {step}");
            }
            // and a leaf that is NOT present must report absent
            assert_eq!(f.find(&lf(999_999)), None, "phantom hit at step {step}");
        }
    }

    /// The BIP30 shape explicitly: two identical leaves, and the first one must win until removed.
    #[test]
    fn duplicate_leaves_resolve_to_the_first_position() {
        let mut f = Forest::new();
        let dup = lf(42);
        f.add(lf(1));
        f.add(dup); // position 1
        f.add(lf(2));
        f.add(dup); // position 3
        assert_eq!(f.find(&dup), Some(1));
        assert_eq!(f.find(&dup), f.leaves.iter().position(|x| *x == dup));

        // remove the FIRST copy; the second must then be found, and must match the scan
        f.delete(1);
        assert_eq!(f.find(&dup), f.leaves.iter().position(|x| *x == dup));
        assert!(f.find(&dup).is_some(), "second copy vanished with the first");

        // remove the remaining copy; now absent
        let p = f.find(&dup).unwrap();
        f.delete(p);
        assert_eq!(f.find(&dup), None);
        assert_eq!(f.leaves.iter().position(|x| *x == dup), None);
    }

    /// Deleting the rightmost leaf is the `i == last` path, where `gone == moved` — removing the
    /// index entry twice there would drop a *different* copy of a duplicated leaf.
    #[test]
    fn deleting_rightmost_duplicate_keeps_the_other_copy() {
        let mut f = Forest::new();
        let dup = lf(7);
        f.add(dup);
        f.add(lf(8));
        f.add(dup); // rightmost, position 2
        assert_eq!(f.find(&dup), Some(0));
        f.delete(2); // i == last
        assert_eq!(f.find(&dup), Some(0), "deleting the rightmost copy removed the wrong entry");
        assert_eq!(f.find(&dup), f.leaves.iter().position(|x| *x == dup));
    }

    /// from_leaves must produce the same index as incremental adds, duplicates included.
    #[test]
    fn from_leaves_index_matches_incremental() {
        let leaves: Vec<Hash> = (0..500u64).map(|i| lf(i % 60)).collect();
        let mut grown = Forest::new();
        for l in &leaves {
            grown.add(*l);
        }
        let loaded = Forest::from_leaves(leaves.clone());
        for l in &leaves {
            assert_eq!(loaded.find(l), grown.find(l));
            assert_eq!(loaded.find(l), leaves.iter().position(|x| x == l));
        }
    }
}

#[cfg(test)]
mod domain_separation_tests {
    //! The tags must actually be applied. A constant that is declared but never hashed separates
    //! nothing, and would pass every structural check while leaving the original hole open.
    use super::*;
    use sha2::{Digest, Sha256};

    #[test]
    fn tags_are_applied_and_change_the_hash() {
        let data = b"a leaf preimage";
        let untagged: Hash = {
            let mut h = Sha256::new();
            h.update(data);
            h.finalize().into()
        };
        assert_ne!(hash_leaf(data), untagged, "TAG_LEAF is declared but not hashed");

        let (l, r) = ([1u8; 32], [2u8; 32]);
        let untagged: Hash = {
            let mut h = Sha256::new();
            h.update(l);
            h.update(r);
            h.finalize().into()
        };
        assert_ne!(parent(&l, &r), untagged, "TAG_NODE is declared but not hashed");
    }

    #[test]
    fn tags_are_distinct() {
        assert_ne!(TAG_LEAF, TAG_NODE, "a shared prefix is not domain separation");
    }

    /// The concrete collision the tags close: a leaf preimage is `57 + scriptPubKey` bytes, so a
    /// 7-byte scriptPubKey produces a 64-byte preimage — exactly what an interior node hashes. With
    /// no tag those two constructions are the same function over the same input length, and the only
    /// remaining barrier is that a leaf preimage opens with a txid. A txid is the hash of a
    /// transaction an attacker can construct and grind, so that is a cost, not a separation.
    #[test]
    fn a_64_byte_leaf_preimage_does_not_collide_with_an_interior_node() {
        let mut preimage = Vec::new();
        preimage.extend_from_slice(&[0xAAu8; 32]); // "txid" — stands in for a ground one
        preimage.extend_from_slice(&[0xBBu8; 32]); // the rest of the leaf fields
        assert_eq!(preimage.len(), 64, "this test is only meaningful at the colliding length");

        let as_leaf = hash_leaf(&preimage);
        let as_node = parent(&[0xAAu8; 32], &[0xBBu8; 32]);
        assert_ne!(
            as_leaf, as_node,
            "a 64-byte leaf preimage hashes identically to the interior node over the same bytes"
        );

        // And without the tags it WOULD collide — the property is the tags, not the field layout.
        let raw_leaf: Hash = {
            let mut h = Sha256::new();
            h.update(&preimage);
            h.finalize().into()
        };
        let raw_node: Hash = {
            let mut h = Sha256::new();
            h.update([0xAAu8; 32]);
            h.update([0xBBu8; 32]);
            h.finalize().into()
        };
        assert_eq!(raw_leaf, raw_node, "the untagged collision this defends against is real");
    }
}

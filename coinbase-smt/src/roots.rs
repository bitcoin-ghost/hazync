//! The roots-only half of the coinbase SMT — the code the GUEST compiles.
//!
//! SPLIT OUT OF lib.rs SO THE GUEST CAN `#[path]`-INCLUDE IT (hazync#88), not for tidiness. A guest
//! that depends on this crate as a Cargo PATH DEPENDENCY bakes the dependency's ABSOLUTE path into the
//! ELF, so the image id changes with the checkout location — the same tree gave four different ids
//! depending on where it was built, and a release host was produced that would have rejected every
//! proof from its own guest.
//!
//! Including the file directly makes it guest source rather than an external crate, so the path is
//! recorded relative to the guest crate and the id stops depending on where the repo lives.
//!
//! THERE IS STILL EXACTLY ONE COPY. This file is compiled twice — once as part of this crate for the
//! host and bridge, once included by path into the guest — and never duplicated, so the drift that a
//! ported copy would invite cannot occur. That property is why `#[path]` was chosen over vendoring a
//! second copy under the guest, which is how `utreexo.rs` is handled.
//!
//! NO CRATE-LEVEL ATTRIBUTES MAY APPEAR HERE. `#![...]` is only legal at a crate root, so adding one
//! breaks the guest build. `no_std`, `extern crate alloc` and the feature gates live in lib.rs.

use alloc::vec::Vec;
use sha2::{Digest, Sha256};

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
/// The root of a tree holding nothing — the state a from-genesis chain necessarily starts in.
///
/// Pinned by `RangeState::is_genesis_anchored`. Without that pin a prover could start from any SMT
/// root at all, including one in which a coinbase it intends to duplicate is already recorded as
/// fully spent, and the BIP30 check would verify happily against a history that never happened.
pub fn empty_root() -> Hash {
    empty_hashes()[DEPTH]
}

pub fn empty_hashes() -> [Hash; DEPTH + 1] {
    let mut e = [[0u8; 32]; DEPTH + 1];
    for d in 1..=DEPTH {
        e[d] = hash_node(&e[d - 1], &e[d - 1]);
    }
    e
}

/// Bit `i` of the key, MSB first — the direction taken at depth `i` walking down from the root.
#[inline]
pub(crate) fn bit(key: &Key, i: usize) -> bool {
    (key[i / 8] >> (7 - (i % 8))) & 1 == 1
}

/// An inclusion **or** non-inclusion proof.
///
/// `siblings` carries ONLY the non-default ones, in ASCENDING DEPTH — root-to-leaf — with `bitmap`
/// marking which depths they belong to. `compute_root` folds leaf-to-root (depth descending) and so
/// consumes this vector from the END.
///
/// This comment said "leaf-to-root" until audit #3 (N-1). That is the wrong direction, and it is the
/// wrong direction in the one place a reader checks before touching `prove` — where the FIRST bug in
/// this file was a spurious `sibs.reverse()` that folded to a well-formed but incorrect root. See the
/// "NOT reversed" note at the end of `prove`, which was correct all along; this is now consistent
/// with it rather than contradicting it. A 256-deep path is otherwise 8 KB of mostly-constant hashes; in a tree of
/// ~1M entries roughly 20 are non-default, so this is ~640 bytes. That compression is not cosmetic —
/// it is the difference between ~1 GB and ~8 GB of extra bundle data across the chain.
#[derive(Clone, Debug, PartialEq, Eq)]
#[cfg_attr(feature = "serde", derive(serde::Serialize, serde::Deserialize))]
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

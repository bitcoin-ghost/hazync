//! Guest-side Utreexo accumulator (roots-only `Stump`). Ported verbatim from the natively-tested
//! `hazync-utreexo` crate — same logic, but `parent` hashes via `sha2` which is the RISC0
//! SHA-accelerated build in the guest. No `Forest` here (that's the host/bridge oracle).

extern crate alloc;
use alloc::vec::Vec;
use sha2::{Digest, Sha256};

pub type Hash = [u8; 32];

pub fn parent(left: &Hash, right: &Hash) -> Hash {
    let mut h = Sha256::new();
    h.update(left);
    h.update(right);
    h.finalize().into()
}

pub struct Proof {
    pub leaf: Hash,
    pub position: u64,
    pub siblings: Vec<Hash>,
}

impl Proof {
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

#[derive(Clone)]
pub struct Stump {
    pub roots: Vec<Option<Hash>>,
    pub num_leaves: u64,
}

impl Stump {
    pub fn new(roots: Vec<Option<Hash>>, num_leaves: u64) -> Self {
        Stump { roots, num_leaves }
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

    pub fn add(&mut self, leaf: Hash) {
        let mut node = leaf;
        let mut h = 0usize;
        while self.root_at(h).is_some() {
            let existing = self.root_at(h).unwrap();
            node = parent(&existing, &node);
            self.set_root(h, None);
            h += 1;
        }
        self.set_root(h, Some(node));
        self.num_leaves += 1;
    }

    pub fn verify(&self, proof: &Proof) -> bool {
        self.root_at(proof.siblings.len()) == Some(proof.compute_root())
    }

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
        panic!("position out of range");
    }

    fn fold(position: u64, leaf: Hash, siblings: &[Hash]) -> Hash {
        let mut node = leaf;
        let mut pos = position;
        for s in siblings {
            node = if pos & 1 == 0 { parent(&node, s) } else { parent(s, &node) };
            pos >>= 1;
        }
        node
    }

    fn remove_rightmost(&mut self, proof_last: &Proof) {
        let h = proof_last.siblings.len();
        self.set_root(h, None);
        for (j, sib) in proof_last.siblings.iter().enumerate() {
            self.set_root(j, Some(*sib));
        }
        self.num_leaves -= 1;
    }

    /// Delete coin at global position `i` (swap-and-shrink). Both proofs against current roots.
    pub fn delete(&mut self, i: u64, proof_i: &Proof, proof_last: &Proof) -> bool {
        // SEC-2: `verify` proves membership, not LOCATION, yet the swap-and-shrink math uses `i`. Pin
        // `i` to the proven leaf's actual global position: its tree height must equal the proof's, and
        // its local offset (`i - tree_offset`) must equal `proof_i.position` (which is the LOCAL index).
        // Without this a prover could feed an `i` inconsistent with the proof and corrupt the
        // accumulator, risking a "spent" coin surviving (double-spend).
        if i >= self.num_leaves {
            return false;
        }
        let (off_chk, h_chk) = self.tree_of(i);
        if proof_i.siblings.len() != h_chk || proof_i.position != i - off_chk {
            return false;
        }
        if !self.verify(proof_i) {
            return false;
        }
        let last = self.num_leaves - 1;
        if i == last {
            self.remove_rightmost(proof_i);
            return true;
        }
        // proof_last must be the CURRENT rightmost coin (position `last`); remove_rightmost + the swap
        // rely on it.
        let (off_l, h_l) = self.tree_of(last);
        if proof_last.siblings.len() != h_l || proof_last.position != last - off_l {
            return false;
        }
        if !self.verify(proof_last) {
            return false;
        }
        let l_hash = proof_last.leaf;
        let (off_i, h_i) = self.tree_of(i);
        let (off_last, _) = self.tree_of(last);
        if off_i != off_last {
            let new_root = Self::fold(proof_i.position, l_hash, &proof_i.siblings);
            self.set_root(h_i, Some(new_root));
            self.remove_rightmost(proof_last);
        } else {
            self.remove_rightmost(proof_last);
            let px = proof_i.position;
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

    /// Normalised roots (trailing `None` trimmed) for equality against an expected root set.
    pub fn normalized(&self) -> Vec<Option<Hash>> {
        let mut v = self.roots.clone();
        while v.last() == Some(&None) {
            v.pop();
        }
        v
    }
}

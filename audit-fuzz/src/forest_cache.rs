//! Differential harness: the cached `Forest` against the pre-cache implementation (hazync#50, item 2).
//!
//! The accumulator's assurance rests on exhaustive small-n equivalence, and #40 rewrote `Forest` to
//! cache internal nodes underneath that assurance. The evidence offered at the time was byte-identical
//! bundle output over 100 real blocks — strong, but evidence about *those blocks*, not a property over
//! all inputs. This drives the data structure directly instead.
//!
//! The reference is `hazync_utreexo::reference`, the same one the crate's own unit tests use. That is
//! deliberate: a private copy in this crate would only ever prove the copy self-consistent.
//!
//! Three assertions per step, ordered by what they can catch:
//!   1. the LEAF VECTOR against a plain `swap_remove` model. This is not derived from the forest,
//!      because 2 and 3 recompute over the model and would agree with each other even if the forest's
//!      own leaf bookkeeping were wrong;
//!   2. the cached roots;
//!   3. proofs — the expensive one, so it is sampled per step and exhaustive when the forest is small.

use arbitrary::Arbitrary;
use hazync_utreexo::{hash_leaf, reference, Forest, Hash};

#[derive(Arbitrary, Debug, Clone)]
pub enum FOp {
    /// Append a fresh, globally-unique coin.
    Add,
    /// Delete `idx % len` — swap-and-shrink, the operation the cache rewrite most endangered.
    Delete { idx: u16 },
}

#[derive(Arbitrary, Debug)]
pub struct Seq {
    pub ops: Vec<FOp>,
}

pub fn run(s: Seq) {
    let mut f = Forest::new();
    let mut model: Vec<Hash> = Vec::new();
    let mut minted: u64 = 0;

    // Bound the sequence so one pathological input cannot dominate the campaign. Proofs are O(log n)
    // each but there are up to `len` of them per step, so an unbounded run degrades to a timeout —
    // which reads as a hang rather than a finding.
    for op in s.ops.iter().take(300) {
        match op {
            FOp::Add => {
                minted += 1;
                let l = hash_leaf(&minted.to_le_bytes());
                f.add(l);
                model.push(l);
            }
            FOp::Delete { idx } => {
                if model.is_empty() {
                    continue; // deleting from empty is the caller's error, not the forest's
                }
                let i = (*idx as usize) % model.len();
                f.delete(i);
                model.swap_remove(i); // exactly what Forest::delete does: pop last, write into i
            }
        }

        assert_eq!(f.leaves, model, "leaf vector diverged");
        assert_eq!(f.roots(), reference::naive_roots(&model), "cached roots diverged");

        // Proofs: all of them while that is cheap, otherwise the ends plus a moving interior probe.
        // The ends are where the tree decomposition changes shape, and the interior probe walks so a
        // long run still covers the middle.
        let n = model.len();
        if n > 0 {
            if n <= 32 {
                for k in 0..n {
                    assert_eq!(f.prove(k), reference::naive_prove(&model, k), "proof diverged at {k}");
                }
            } else {
                for k in [0, n - 1, (minted as usize) % n] {
                    assert_eq!(f.prove(k), reference::naive_prove(&model, k), "proof diverged at {k}");
                }
            }
        }
    }
}

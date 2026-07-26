//! Adversarial differential fuzz harness for the Hazync Utreexo accumulator.
//!
//! What it exercises: the *guest's* SEC-2-hardened `Stump::delete`
//! (`prover/methods/guest/src/utreexo.rs`) — the code that actually runs in the zkVM and is
//! the soundness authority. We `include!` it verbatim; its only non-portable dependency is
//! `sha2`, whose host output is bit-identical to the RISC0-accelerated build, so the logic
//! transfers unchanged.
//!
//! Model: a ground-truth `Forest` (the honest bridge oracle from the reference crate) is kept
//! in lockstep with a guest `Stump`. The fuzzer drives honest adds/deletes AND *forged* deletes
//! (attacker-chosen `i`, tampered proofs). We then assert the three properties that matter:
//!
//!   * SOUNDNESS  — if `delete` returns true it MUST equal honestly deleting the coin genuinely
//!                  at global position `i` (swap-and-shrink). Anything else = a spent coin
//!                  surviving / the wrong coin removed / state injection (the SEC-2 class).
//!   * ATOMICITY  — if `delete` returns false the accumulator MUST be byte-for-byte unchanged
//!                  (no partial mutation on a rejected/forged spend).
//!   * COMPLETENESS — an honest spend of a real coin MUST be accepted (else valid blocks can't
//!                  be proven — a liveness break).
//!
//! No panic is allowed on any input (libFuzzer treats a panic as a crash).

use arbitrary::Arbitrary;
use hazync_utreexo::{hash_leaf, Forest, Proof as RefProof};

/// The guest's hardened accumulator, compiled natively. `sha2` host == accelerated guest output.
/// `#[path]` points at the real guest file so there is zero drift from what the zkVM runs.
#[path = "../../prover/methods/guest/src/utreexo.rs"]
mod guest;

type H = [u8; 32];

fn to_guest_proof(p: &RefProof) -> guest::Proof {
    guest::Proof { leaf: p.leaf, position: p.position, siblings: p.siblings.clone() }
}

fn norm(mut v: Vec<Option<H>>) -> Vec<Option<H>> {
    while v.last() == Some(&None) {
        v.pop();
    }
    v
}

/// Guest roots must equal the honest oracle's roots at every step.
fn assert_lockstep(g: &guest::Stump, f: &Forest, ctx: &str) {
    assert_eq!(
        norm(g.normalized()),
        norm(f.roots()),
        "SOUNDNESS: guest roots diverged from honest oracle [{ctx}]"
    );
    assert_eq!(
        g.num_leaves,
        f.leaves.len() as u64,
        "SOUNDNESS: leaf count diverged from honest oracle [{ctx}]"
    );
}

/// How to corrupt the proofs handed to `delete`. Each field is an independent tamper knob so the
/// coverage-guided fuzzer can combine them to search for a forgery that slips past the hardening.
#[derive(Arbitrary, Debug, Clone)]
pub struct Forge {
    /// Take `proof_i` from a *different* coin than the `i` we claim (the core SEC-2 attack:
    /// membership-without-location).
    borrow_i_from_other: bool,
    /// Take `proof_last` from a coin that is NOT the current rightmost.
    last_not_rightmost: bool,
    /// Claim an `i` that is out of range (>= num_leaves).
    i_out_of_range: bool,
    /// XOR the low bits of `proof_i.position` (try to satisfy the position pin by brute force).
    flip_pos_i: u8,
    /// Overwrite `proof_i.position` outright with an attacker value.
    set_pos_i: Option<u64>,
    /// Replace `proof_i.leaf` with an attacker leaf (spend a coin that isn't there).
    swap_leaf_i: bool,
    /// Tamper one sibling hash in `proof_i`.
    tamper_sib_i: Option<u16>,
    /// Push an extra sibling onto `proof_i` (wrong height).
    grow_sibs_i: bool,
    /// Drop the last sibling of `proof_i` (wrong height).
    shrink_sibs_i: bool,
    /// Same tamper set for proof_last.
    swap_leaf_last: bool,
    tamper_sib_last: Option<u16>,
    set_pos_last: Option<u64>,
}

/// A fully attacker-authored proof — arbitrary leaf, position, and sibling stack, *not* derived
/// from the honest `Forest`. Explores the structural proof space (wrong heights, giant positions,
/// junk siblings) that tamper-from-honest can't reach — panic-safety + false-accept hunting.
#[derive(Arbitrary, Debug, Clone)]
pub struct RawProof {
    leaf: [u8; 32],
    position: u64,
    siblings: Vec<[u8; 32]>,
}

impl RawProof {
    fn into_guest(mut self) -> guest::Proof {
        self.siblings.truncate(72); // cap: heights never exceed 64; bound compute/alloc
        guest::Proof { leaf: self.leaf, position: self.position, siblings: self.siblings }
    }
}

#[derive(Arbitrary, Debug, Clone)]
pub enum Op {
    /// Append a fresh, globally-unique coin (honest).
    Add,
    /// Honestly spend the coin at index `idx % len` — MUST be accepted.
    DeleteHonest { idx: u16 },
    /// Adversarially attempt to spend, with forged proofs / mismatched `i`.
    DeleteForged { i_sel: u16, src_i: u16, src_last: u16, forge: Forge },
    /// Attempt to spend with FULLY arbitrary (non-oracle) proof structures.
    DeleteArbitrary { i_sel: u16, oob: bool, pi: RawProof, pl: RawProof },
    /// Call `verify` with a fully arbitrary proof against the live Stump (panic-safety).
    VerifyArbitrary { p: RawProof },
}

#[derive(Arbitrary, Debug)]
pub struct Scenario {
    pub ops: Vec<Op>,
}

fn tamper(p: &mut guest::Proof, leaf: bool, sib: Option<u16>, pos: Option<u64>) {
    if leaf {
        p.leaf[0] ^= 0xA5;
        p.leaf[31] ^= 0x5A;
    }
    if let Some(k) = sib {
        if !p.siblings.is_empty() {
            let idx = (k as usize) % p.siblings.len();
            p.siblings[idx][0] ^= 0xFF;
        }
    }
    if let Some(v) = pos {
        p.position = v;
    }
}

pub fn run(s: Scenario) {
    let mut forest = Forest::new();
    let mut stump = guest::Stump::new(Vec::new(), 0);
    let mut ctr: u64 = 0;

    // Bound work per input so libFuzzer stays fast and each unit is deterministic.
    for op in s.ops.into_iter().take(400) {
        match op {
            Op::Add => {
                let l = hash_leaf(&ctr.to_le_bytes());
                ctr += 1;
                forest.add(l);
                stump.add(l);
                assert_lockstep(&stump, &forest, "after add");
            }

            Op::DeleteHonest { idx } => {
                let n = forest.leaves.len();
                if n == 0 {
                    continue;
                }
                let i = (idx as usize) % n;
                let last = n - 1;
                let pi = to_guest_proof(&forest.prove(i));
                let pl = to_guest_proof(&forest.prove(last));

                let ok = stump.delete(i as u64, &pi, &pl);
                assert!(ok, "COMPLETENESS: honest spend rejected (n={n}, i={i})");
                forest.delete(i);
                assert_lockstep(&stump, &forest, "after honest delete");
            }

            Op::DeleteForged { i_sel, src_i, src_last, forge } => {
                let n = forest.leaves.len();
                if n == 0 {
                    continue;
                }

                // Claimed target position.
                let i: u64 = if forge.i_out_of_range {
                    n as u64 + (i_sel as u64 % 4)
                } else {
                    (i_sel as usize % n) as u64
                };

                // proof_i: from `i` itself (honest-ish) unless told to borrow another coin's proof.
                let src_i_idx = if forge.borrow_i_from_other {
                    (src_i as usize) % n
                } else if (i as usize) < n {
                    i as usize
                } else {
                    (src_i as usize) % n
                };
                let mut pi = to_guest_proof(&forest.prove(src_i_idx));

                // proof_last: the true rightmost unless told otherwise.
                let last_idx =
                    if forge.last_not_rightmost { (src_last as usize) % n } else { n - 1 };
                let mut pl = to_guest_proof(&forest.prove(last_idx));

                // Apply position tampering on proof_i.
                if forge.flip_pos_i != 0 {
                    pi.position ^= forge.flip_pos_i as u64;
                }
                tamper(&mut pi, forge.swap_leaf_i, forge.tamper_sib_i, forge.set_pos_i);
                if forge.grow_sibs_i {
                    pi.siblings.push([0x11u8; 32]);
                }
                if forge.shrink_sibs_i {
                    pi.siblings.pop();
                }
                tamper(&mut pl, forge.swap_leaf_last, forge.tamper_sib_last, forge.set_pos_last);

                // Snapshot for the atomicity check.
                let before_roots = stump.roots.clone();
                let before_n = stump.num_leaves;

                let ok = stump.delete(i, &pi, &pl);

                if ok {
                    // The hardening guarantees acceptance => proof_i genuinely proves the coin at
                    // global `i` and proof_last genuinely proves the rightmost. So the ONLY sound
                    // outcome is honestly deleting the coin at `i`. Verify that exactly.
                    assert!(
                        (i as usize) < forest.leaves.len(),
                        "SOUNDNESS: accepted a delete with i out of range (i={i}, len={})",
                        forest.leaves.len()
                    );
                    forest.delete(i as usize);
                    assert_lockstep(&stump, &forest, "after ACCEPTED forged delete");
                } else {
                    // Rejected spends must not mutate the accumulator at all.
                    assert_eq!(
                        stump.roots, before_roots,
                        "ATOMICITY: rejected delete mutated roots"
                    );
                    assert_eq!(
                        stump.num_leaves, before_n,
                        "ATOMICITY: rejected delete mutated leaf count"
                    );
                }
            }

            Op::DeleteArbitrary { i_sel, oob, pi, pl } => {
                let n = forest.leaves.len();
                if n == 0 {
                    continue;
                }
                let i: u64 = if oob {
                    n as u64 + (i_sel as u64 % 4)
                } else {
                    (i_sel as usize % n) as u64
                };
                let gpi = pi.into_guest();
                let gpl = pl.into_guest();

                let before_roots = stump.roots.clone();
                let before_n = stump.num_leaves;

                // Must not panic on any arbitrary proof structure.
                let ok = stump.delete(i, &gpi, &gpl);

                if ok {
                    // A fully-arbitrary proof that is ACCEPTED must nonetheless be genuinely valid
                    // (it forged a real root + passed the position pin). The only sound outcome is
                    // honestly deleting the coin at `i`. If a junk proof is accepted with a
                    // divergent result, that is a false-accept / forgery — the assert fires.
                    assert!(
                        (i as usize) < forest.leaves.len(),
                        "FALSE-ACCEPT: arbitrary proof accepted an out-of-range delete"
                    );
                    forest.delete(i as usize);
                    assert_lockstep(&stump, &forest, "after ACCEPTED arbitrary delete");
                } else {
                    assert_eq!(stump.roots, before_roots, "ATOMICITY: arbitrary reject mutated roots");
                    assert_eq!(stump.num_leaves, before_n, "ATOMICITY: arbitrary reject mutated count");
                }
            }

            Op::VerifyArbitrary { p } => {
                // Pure panic-safety: verify must tolerate any proof shape without crashing.
                // (A `true` here would require folding to a committed root by chance — SHA makes
                // that infeasible, so we assert only robustness, not the boolean.)
                let _ = stump.verify(&p.into_guest());
            }
        }
    }
}

// -------------------------------------------------------------------------------------------
// Differential control: the SAME scenario against the *unhardened* reference `Stump`
// (`hazync-utreexo`), which by design lacks the SEC-2 position pin. If this run breaks and the
// guest run does not, that is direct evidence (a) the harness detects the SEC-2 class, and
// (b) the guest hardening is what closes it. A rejected delete here may `panic` (unguarded
// `tree_of`/underflow) OR silently corrupt — both are failures the oracle catches.
// -------------------------------------------------------------------------------------------

fn tamper_ref(p: &mut RefProof, leaf: bool, sib: Option<u16>, pos: Option<u64>) {
    if leaf {
        p.leaf[0] ^= 0xA5;
        p.leaf[31] ^= 0x5A;
    }
    if let Some(k) = sib {
        if !p.siblings.is_empty() {
            let idx = (k as usize) % p.siblings.len();
            p.siblings[idx][0] ^= 0xFF;
        }
    }
    if let Some(v) = pos {
        p.position = v;
    }
}

pub fn run_reference(s: Scenario) {
    use hazync_utreexo::Stump as RefStump;
    let mut forest = Forest::new();
    let mut stump = RefStump::new();
    let mut ctr: u64 = 0;

    for op in s.ops.into_iter().take(400) {
        match op {
            Op::Add => {
                let l = hash_leaf(&ctr.to_le_bytes());
                ctr += 1;
                forest.add(l);
                stump.add(l);
            }
            Op::DeleteHonest { idx } => {
                let n = forest.leaves.len();
                if n == 0 {
                    continue;
                }
                let i = (idx as usize) % n;
                let pi = forest.prove(i);
                let pl = forest.prove(n - 1);
                if stump.delete(i as u64, &pi, &pl) {
                    forest.delete(i);
                }
            }
            Op::DeleteForged { i_sel, src_i, src_last, forge } => {
                let n = forest.leaves.len();
                if n == 0 {
                    continue;
                }
                let i: u64 = if forge.i_out_of_range {
                    n as u64 + (i_sel as u64 % 4)
                } else {
                    (i_sel as usize % n) as u64
                };
                let src_i_idx = if forge.borrow_i_from_other {
                    (src_i as usize) % n
                } else if (i as usize) < n {
                    i as usize
                } else {
                    (src_i as usize) % n
                };
                let mut pi = forest.prove(src_i_idx);
                let last_idx =
                    if forge.last_not_rightmost { (src_last as usize) % n } else { n - 1 };
                let mut pl = forest.prove(last_idx);

                if forge.flip_pos_i != 0 {
                    pi.position ^= forge.flip_pos_i as u64;
                }
                tamper_ref(&mut pi, forge.swap_leaf_i, forge.tamper_sib_i, forge.set_pos_i);
                if forge.grow_sibs_i {
                    pi.siblings.push([0x11u8; 32]);
                }
                if forge.shrink_sibs_i {
                    pi.siblings.pop();
                }
                tamper_ref(&mut pl, forge.swap_leaf_last, forge.tamper_sib_last, forge.set_pos_last);

                let before_roots = stump.roots.clone();
                let before_n = stump.num_leaves;
                // NB: guard i in-range for the oracle side; if the reference accepts an
                // out-of-range i that is itself the bug, surfaced by the assert below.
                if stump.delete(i, &pi, &pl) {
                    assert!(
                        (i as usize) < forest.leaves.len(),
                        "REFERENCE accepted out-of-range delete"
                    );
                    forest.delete(i as usize);
                    assert_eq!(
                        norm(stump.roots.clone()),
                        norm(forest.roots()),
                        "REFERENCE SOUNDNESS: accepted forged delete diverged from oracle"
                    );
                } else {
                    assert_eq!(stump.roots, before_roots, "REFERENCE ATOMICITY: roots mutated");
                    assert_eq!(stump.num_leaves, before_n, "REFERENCE ATOMICITY: count mutated");
                }
            }
            // The arbitrary-proof ops target the hardened guest; the reference control skips them.
            Op::DeleteArbitrary { .. } | Op::VerifyArbitrary { .. } => continue,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // A hand-scripted scenario: build a forest, do an honest spend, then a battery of forged
    // spends, and confirm the harness's own plumbing (oracle lockstep, accept/reject paths) works
    // before we hand it to libFuzzer.
    #[test]
    fn smoke_honest_and_forged() {
        let mut ops = Vec::new();
        for _ in 0..20 {
            ops.push(Op::Add);
        }
        ops.push(Op::DeleteHonest { idx: 7 });
        ops.push(Op::DeleteForged {
            i_sel: 3,
            src_i: 9,
            src_last: 2,
            forge: Forge {
                borrow_i_from_other: true,
                last_not_rightmost: true,
                i_out_of_range: false,
                flip_pos_i: 1,
                set_pos_i: None,
                swap_leaf_i: false,
                tamper_sib_i: Some(0),
                grow_sibs_i: false,
                shrink_sibs_i: false,
                swap_leaf_last: false,
                tamper_sib_last: None,
                set_pos_last: None,
            },
        });
        run(Scenario { ops });
    }

    // Directly assert the SEC-2 hardening: borrow coin 9's proof but claim position 3.
    // The guest must REJECT (position pin), and must not mutate.
    #[test]
    fn sec2_location_confusion_is_rejected() {
        let mut forest = Forest::new();
        let mut stump = guest::Stump::new(Vec::new(), 0);
        for i in 0..25u64 {
            let l = hash_leaf(&i.to_le_bytes());
            forest.add(l);
            stump.add(l);
        }
        let last = forest.leaves.len() - 1;
        // Honest proof of coin 9, but we claim to be deleting position 3.
        let pi = to_guest_proof(&forest.prove(9));
        let pl = to_guest_proof(&forest.prove(last));
        let before = stump.roots.clone();
        let ok = stump.delete(3, &pi, &pl);
        assert!(!ok, "SEC-2: location-confused delete was ACCEPTED");
        assert_eq!(stump.roots, before, "rejected delete mutated state");
    }
}

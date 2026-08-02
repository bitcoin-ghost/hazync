//! The guest's Utreexo `Stump` — the copy that actually runs inside the proof (hazync#76).
//!
//! Until now its correctness rested on three things, none of which was a test of this file:
//! it is "ported verbatim" from the natively-tested reference (an assertion), `check-utreexo.sh`
//! gates the hash construction (real, but only tags and preimage shape), and every board proof
//! exercises it (true, but catches nothing that fails CLOSED — a reject-valid regression looks like a
//! stalled board, not a test failure).
//!
//! THE MODULE IS INCLUDED BY PATH, NEVER COPIED. `#[path]` compiles the real guest source, so these
//! tests cannot pass against a stale duplicate — the same zero-drift rule `build.rs` follows for
//! `main.rs`. It also means this file must not edit the guest: any change there moves `METHOD_ID` and
//! invalidates the board, comments included.
//!
//! WHAT IS ACTUALLY WORTH TESTING is not the shared logic — that is already covered natively by the
//! reference's 24 tests — but the part where the guest DELIBERATELY DIFFERS: the SEC-2 pinning. The
//! reference is an oracle fed honest proofs; the guest is fed proofs by a possibly-malicious prover,
//! so it pins `i` to the proven leaf's real position. External audit #2 verified by hand that this
//! pinning *implies* the properties the reference gets from its explicit L-2 guards. That is exactly
//! the reasoning a test should encode, so the next reviewer does not have to re-derive it.

#[path = "../../prover/methods/guest/src/utreexo.rs"]
mod guest;

use hazync_utreexo as reference;

fn gleaf(i: u64) -> guest::Hash {
    // Mirrors reference::hash_leaf: SHA256(TAG_LEAF || data). Written out rather than imported so a
    // divergence in the reference's leaf hashing cannot silently make both sides agree.
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update([guest::TAG_LEAF]);
    h.update(i.to_le_bytes());
    h.finalize().into()
}
fn rleaf(i: u64) -> reference::Hash {
    reference::hash_leaf(&i.to_le_bytes())
}

fn build(n: u64) -> (guest::Stump, reference::Forest) {
    let mut g = guest::Stump::new(Vec::new(), 0);
    let mut f = reference::Forest::new();
    for k in 0..n {
        g.add(gleaf(k));
        f.add(rleaf(k));
    }
    (g, f)
}

/// Reference `Proof` → guest `Proof`. The two structs are identical by construction; this is where a
/// field-order or semantics drift would show up.
fn to_guest(p: &reference::Proof) -> guest::Proof {
    guest::Proof { leaf: p.leaf, position: p.position, siblings: p.siblings.clone() }
}

/// Compare root vectors by CONTENT, not width.
///
/// The two sides legitimately differ in trailing-`None` PADDING — at n=0 the guest holds `[]` and the
/// reference `[None]`. That is not a divergence: a height with no tree is absent or None, and both
/// mean the same thing. Asserting `len()` equality fails on a difference that carries no information,
/// which is why the guest has `normalized()` at all.
///
/// This is the same padding asymmetry that broke spine seam-matching once — a raw `assert_eq!` on the
/// vectors rejected `[Some(A),Some(B),None]` against `[Some(A),Some(B)]`, i.e. the same accumulator.
/// Encoding it here means the next person meets it as a documented property, not a puzzle.
fn roots_agree(g: &[Option<guest::Hash>], r: &[Option<reference::Hash>]) -> bool {
    let n = g.len().max(r.len());
    (0..n).all(|h| g.get(h).copied().flatten() == r.get(h).copied().flatten())
}

#[test]
fn guest_and_reference_agree_on_roots_for_every_forest_shape() {
    // Shape boundaries are at powers of two, so walk every size rather than sampling.
    for n in 0..=128u64 {
        let (g, f) = build(n);
        assert_eq!(g.num_leaves, n, "n={n}");
        assert!(roots_agree(&g.roots, &f.roots()), "roots differ at n={n}");
    }
}

#[test]
fn an_honest_delete_matches_the_reference_step_for_step() {
    for n in 1..=40u64 {
        for i in 0..n {
            let (mut g, mut f) = build(n);
            let (pi, pl) = (f.prove(i as usize), f.prove((n - 1) as usize));
            assert!(g.delete(i, &to_guest(&pi), &to_guest(&pl)), "guest refused an honest delete n={n} i={i}");
            f.delete(i as usize);
            assert_eq!(g.num_leaves, n - 1, "leaf count diverged n={n} i={i}");
            assert!(roots_agree(&g.roots, &f.roots()), "roots diverged after delete n={n} i={i}");
        }
    }
}

#[test]
fn sec2_pinning_rejects_an_index_inconsistent_with_the_proof() {
    // THE defence the guest adds over the reference. `verify` proves MEMBERSHIP, not LOCATION, so a
    // prover could otherwise present an honest proof for leaf A while naming index B — and the
    // swap-and-shrink maths uses the index. The failure that buys is a "spent" coin surviving.
    let n = 16u64;
    for i in 0..n {
        let (_, f) = build(n);
        let pi = f.prove(i as usize);
        let pl = f.prove((n - 1) as usize);
        for j in 0..n {
            if j == i {
                continue;
            }
            let (mut g, _) = build(n);
            assert!(
                !g.delete(j, &to_guest(&pi), &to_guest(&pl)),
                "guest ACCEPTED proof for leaf {i} while deleting index {j} — SEC-2 pin not holding"
            );
            assert_eq!(g.num_leaves, n, "a rejected delete still mutated the stump");
        }
    }
}

#[test]
fn a_proof_last_that_is_not_the_rightmost_is_rejected() {
    // The swap relies on proof_last being the CURRENT rightmost. Anything else corrupts the shrink.
    let n = 24u64;
    let (_, f) = build(n);
    let pi = f.prove(3);
    for other in 0..(n - 1) {
        let mut g = build(n).0;
        let bad_last = f.prove(other as usize);
        assert!(
            !g.delete(3, &to_guest(&pi), &to_guest(&bad_last)),
            "guest accepted a non-rightmost proof_last (position {other})"
        );
        assert_eq!(g.num_leaves, n, "a rejected delete mutated the stump");
    }
}

#[test]
fn out_of_range_and_empty_are_refused_without_panicking() {
    let mut empty = guest::Stump::new(Vec::new(), 0);
    let p = guest::Proof { leaf: gleaf(0), position: 0, siblings: Vec::new() };
    assert!(!empty.delete(0, &p, &p), "delete on an empty stump must be refused");

    let (_, f) = build(5);
    let good = to_guest(&f.prove(0));
    let last = to_guest(&f.prove(4));
    let mut g = build(5).0;
    assert!(!g.delete(999, &good, &last), "out-of-range index must be refused");
    assert!(!g.delete(u64::MAX, &good, &last), "u64::MAX index must be refused");
    assert_eq!(g.num_leaves, 5, "refusals must not mutate");

    // Guard against the whole test going vacuous: the same machinery must still accept a real delete.
    assert!(g.delete(4, &last, &last), "a legitimate delete must still succeed");
}

#[test]
fn stale_proof_after_a_delete_is_rejected() {
    // The double-spend shape: prove a coin, delete it, then try the same proof again.
    let (mut g, f) = build(12);
    let pi = to_guest(&f.prove(5));
    let pl = to_guest(&f.prove(11));
    assert!(g.delete(5, &pi, &pl), "first delete should succeed");
    assert!(!g.delete(5, &pi, &pl), "the SAME proof must not delete the coin twice");
}

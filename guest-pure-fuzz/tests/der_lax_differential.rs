//! hazync#139 — is the guest's lax DER parser the SAME parser Bitcoin uses?
//!
//! The guest's wholesale ECDSA path replaces Core's `ecdsa_signature_parse_der_lax`. That parser
//! exists to accept the format violations present in the chain BEFORE BIP66 (block 363,725):
//! negative integers, excessive padding, garbage at the end, overly long length descriptors. Getting
//! it subtly wrong yields a prover that disagrees with Bitcoin about a historical block — a bug that
//! passes every ordinary test and is wrong on some 2012 transaction.
//!
//! So this does not test the parser against expectations. It tests it against `secp256k1`'s
//! `from_der_lax`, which is a port of the same C function, over structured mutations of real
//! signatures and over random bytes. Disagreement is the finding.

use guest_pure_fuzz::ecdsa_der::parse_der_lax;
use rand::{Rng, SeedableRng};
use secp256k1::{ecdsa, Message, Secp256k1, SecretKey};

/// The authority. Returns the compact form when the reference parser accepts.
fn reference(der: &[u8]) -> Option<[u8; 64]> {
    ecdsa::Signature::from_der_lax(der).ok().map(|s| s.serialize_compact())
}

/// Compare ours against the reference, reporting the input on disagreement.
fn agree(der: &[u8], label: &str) -> bool {
    let ours = parse_der_lax(der);
    let theirs = reference(der);
    match (ours, theirs) {
        (None, None) => true,
        (Some(a), Some(b)) if a == b => true,
        // Core returns 1 with an all-zero (parseable but invalid) signature on overflow. A reference
        // that rejects instead is a REAL semantic difference and must be surfaced, not smoothed over:
        // it decides whether the input is rejected at parse time or at verify time.
        (Some(a), None) if a == [0u8; 64] => {
            eprintln!("OVERFLOW-SEMANTICS [{label}]: ours=zero-sig (Core returns 1), reference rejects");
            eprintln!("  der = {}", hex(der));
            false
        }
        (o, t) => {
            eprintln!("DISAGREEMENT [{label}]");
            eprintln!("  der    = {}", hex(der));
            eprintln!("  ours   = {}", o.map(|v| hex(&v)).unwrap_or_else(|| "reject".into()));
            eprintln!("  theirs = {}", t.map(|v| hex(&v)).unwrap_or_else(|| "reject".into()));
            false
        }
    }
}

fn hex(b: &[u8]) -> String { b.iter().map(|x| format!("{x:02x}")).collect() }

/// Real, valid DER signatures — the case that must never disagree.
fn real_signatures(n: usize) -> Vec<Vec<u8>> {
    let secp = Secp256k1::new();
    let mut out = Vec::new();
    for i in 1..=n as u32 {
        let mut sk = [0u8; 32];
        sk[28..].copy_from_slice(&i.to_be_bytes());
        sk[0] = 1;
        let sk = SecretKey::from_slice(&sk).unwrap();
        let mut msg = [0u8; 32];
        msg[24..28].copy_from_slice(&i.to_be_bytes());
        msg[0] = 0x5a;
        out.push(secp.sign_ecdsa(&Message::from_digest(msg), &sk).serialize_der().to_vec());
    }
    out
}

#[test]
fn valid_der_signatures_agree() {
    let mut bad = 0;
    for (i, der) in real_signatures(256).iter().enumerate() {
        if !agree(der, &format!("valid #{i}")) { bad += 1; }
    }
    assert_eq!(bad, 0, "{bad} valid signatures parsed differently from the reference");
}

#[test]
fn historical_violations_agree() {
    // The violations the lax parser exists for. Each is applied to a real signature, so the mutation
    // is the only variable.
    let base = real_signatures(24);
    let mut cases: Vec<(String, Vec<u8>)> = Vec::new();
    for (i, der) in base.iter().enumerate() {
        cases.push((format!("garbage-suffix #{i}"), [der.clone(), vec![0xde, 0xad]].concat()));

        // Long-form sequence length descriptor (0x81 <len>) instead of short form.
        let mut long_seq = der.clone();
        if long_seq.len() > 2 { let l = long_seq[1]; long_seq[1] = 0x81; long_seq.insert(2, l); }
        cases.push((format!("long-form-seq-len #{i}"), long_seq));

        // Excessive zero padding on R.
        let mut padded = der.clone();
        if padded.len() > 4 { padded[3] += 1; padded.insert(4, 0x00); padded[1] += 1; }
        cases.push((format!("padded-R #{i}"), padded));

        // Truncations — every prefix is a malformed input the parser must handle without panicking.
        for cut in 1..der.len().min(12) { cases.push((format!("trunc-{cut} #{i}"), der[..cut].to_vec())); }

        // Tag corruption.
        let mut t = der.clone(); t[0] = 0x31;
        cases.push((format!("bad-seq-tag #{i}"), t));
        let mut t2 = der.clone(); if t2.len() > 2 { t2[2] = 0x03; }
        cases.push((format!("bad-int-tag #{i}"), t2));
    }
    let mut bad = 0;
    for (label, der) in &cases { if !agree(der, label) { bad += 1; } }
    assert_eq!(bad, 0, "{bad} of {} mutated cases disagreed", cases.len());
}

#[test]
fn random_bytes_agree_and_never_panic() {
    // Rust slices panic where the C reads out of bounds, so this is also a memory-safety check on
    // the transcription: a bounds slip shows up here as a panic rather than as silent UB.
    let mut rng = rand::rngs::StdRng::seed_from_u64(0x139);
    let mut bad = 0;
    for i in 0..20_000 {
        let len = rng.gen_range(0..80);
        let mut v: Vec<u8> = (0..len).map(|_| rng.gen()).collect();
        // Bias a third of them toward looking like a SEQUENCE so the parser gets past its first gate.
        if i % 3 == 0 && !v.is_empty() { v[0] = 0x30; }
        if !agree(&v, &format!("random #{i}")) { bad += 1; }
    }
    assert_eq!(bad, 0, "{bad} random inputs disagreed");
}

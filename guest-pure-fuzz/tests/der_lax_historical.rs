//! hazync#139 — the violations the lax parser actually EXISTS for.
//!
//! `der_lax_differential.rs` checks real signatures and random mutations. Neither reaches the cases
//! that motivated Core's lax parser, because those are RARE BY NATURE — they needed a special parser
//! rather than a rule change precisely because they are scattered thinly through pre-BIP66 history.
//! Sampling real blocks will not find them: blocks 130000 and 140000 both parsed clean, which says
//! they contain well-formed DER, not that the parser handles the malformed kind.
//!
//! So these are CONSTRUCTED, one per violation class named in Core's own comment — "negative
//! integers, excessive padding, garbage at the end, and overly long length descriptors" — plus the
//! boundary conditions around the group order where the overflow path lives.
//!
//! The authority is `from_der_lax`, a port of the same C function. Disagreement here is a
//! pre-BIP66 block the prover would judge differently from Bitcoin.

use guest_pure_fuzz::ecdsa_der::parse_der_lax;
use secp256k1::ecdsa;

fn reference(der: &[u8]) -> Option<[u8; 64]> {
    ecdsa::Signature::from_der_lax(der).ok().map(|s| s.serialize_compact())
}

fn hex(b: &[u8]) -> String { b.iter().map(|x| format!("{x:02x}")).collect() }

/// Assemble a DER SEQUENCE from raw R and S integer CONTENTS, short-form lengths.
fn der(r: &[u8], s: &[u8]) -> Vec<u8> {
    let body_len = 2 + r.len() + 2 + s.len();
    let mut v = vec![0x30, body_len as u8, 0x02, r.len() as u8];
    v.extend_from_slice(r);
    v.push(0x02);
    v.push(s.len() as u8);
    v.extend_from_slice(s);
    v
}

const N_MINUS_1: [u8; 32] = [
    0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFE,
    0xBA,0xAE,0xDC,0xE6,0xAF,0x48,0xA0,0x3B,0xBF,0xD2,0x5E,0x8C,0xD0,0x36,0x41,0x40,
];
const N: [u8; 32] = [
    0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFE,
    0xBA,0xAE,0xDC,0xE6,0xAF,0x48,0xA0,0x3B,0xBF,0xD2,0x5E,0x8C,0xD0,0x36,0x41,0x41,
];

fn cases() -> Vec<(&'static str, Vec<u8>)> {
    // A well-formed positive integer: high bit clear, no padding.
    let ok_r: Vec<u8> = { let mut v = vec![0x2a]; v.extend_from_slice(&[0x11; 31]); v };
    let ok_s: Vec<u8> = { let mut v = vec![0x3b]; v.extend_from_slice(&[0x22; 31]); v };
    let mut c: Vec<(&'static str, Vec<u8>)> = Vec::new();

    c.push(("baseline well-formed", der(&ok_r, &ok_s)));

    // ── "negative integers" — high bit set with NO 0x00 sign byte. Strict DER forbids it; the chain
    //    contains it, which is the single most-cited reason this parser exists.
    let neg: Vec<u8> = { let mut v = vec![0x80]; v.extend_from_slice(&[0x44; 31]); v };
    c.push(("negative R (high bit, no sign byte)", der(&neg, &ok_s)));
    c.push(("negative S (high bit, no sign byte)", der(&ok_r, &neg)));
    c.push(("negative BOTH", der(&neg, &neg)));

    // ── "excessive padding" — leading zero bytes beyond the one DER allows.
    for pad in [1usize, 2, 5, 10] {
        let mut p = vec![0x00; pad]; p.extend_from_slice(&ok_r);
        c.push((Box::leak(format!("R padded with {pad} zero byte(s)").into_boxed_str()), der(&p, &ok_s)));
    }
    let mut ps = vec![0x00; 3]; ps.extend_from_slice(&ok_s);
    c.push(("S padded with 3 zero bytes", der(&ok_r, &ps)));

    // ── "garbage at the end" — trailing bytes past the SEQUENCE body.
    let mut g = der(&ok_r, &ok_s); g.extend_from_slice(&[0xde, 0xad, 0xbe, 0xef]);
    c.push(("garbage suffix (4 bytes)", g));
    let mut g2 = der(&ok_r, &ok_s); g2.extend_from_slice(&[0x00; 40]);
    c.push(("garbage suffix (40 zero bytes)", g2));

    // ── "overly long length descriptors" — long-form lengths where short form would do.
    let base = der(&ok_r, &ok_s);
    let mut l1 = base.clone(); let bl = l1[1]; l1[1] = 0x81; l1.insert(2, bl);
    c.push(("SEQUENCE long-form length 0x81", l1));
    let mut l2 = base.clone(); let bl2 = l2[1]; l2[1] = 0x82; l2.insert(2, 0x00); l2.insert(3, bl2);
    c.push(("SEQUENCE long-form length 0x82 with leading zero", l2));
    // Long-form on the R integer length.
    {
        let mut v = vec![0x30, 0x00, 0x02, 0x81, ok_r.len() as u8];
        v.extend_from_slice(&ok_r); v.push(0x02); v.push(ok_s.len() as u8); v.extend_from_slice(&ok_s);
        let bl = (v.len() - 2) as u8; v[1] = bl;
        c.push(("R integer long-form length 0x81", v));
    }
    // Length descriptor with >= 4 bytes — Core rejects these outright.
    {
        let mut v = vec![0x30, 0x00, 0x02, 0x84, 0x00, 0x00, 0x00, ok_r.len() as u8];
        v.extend_from_slice(&ok_r); v.push(0x02); v.push(ok_s.len() as u8); v.extend_from_slice(&ok_s);
        let bl = (v.len() - 2) as u8; v[1] = bl;
        c.push(("R length descriptor 4 bytes (must reject)", v));
    }

    // ── group-order boundaries: where the overflow path and the range check live.
    c.push(("r = n-1 (largest valid)", der(&N_MINUS_1, &ok_s)));
    c.push(("s = n-1 (largest valid)", der(&ok_r, &N_MINUS_1)));
    c.push(("r = n exactly (OVERFLOW)", der(&N, &ok_s)));
    c.push(("s = n exactly (OVERFLOW)", der(&ok_r, &N)));
    c.push(("r = 33 bytes (OVERFLOW)", der(&[0x01; 33], &ok_s)));
    c.push(("r = 33 bytes but leading zero (fits after strip)", der(&{ let mut v=vec![0x00]; v.extend_from_slice(&ok_r); v }, &ok_s)));
    c.push(("r = 40 bytes (OVERFLOW)", der(&[0x02; 40], &ok_s)));

    // ── degenerate integers.
    c.push(("r zero-length", der(&[], &ok_s)));
    c.push(("s zero-length", der(&ok_r, &[])));
    c.push(("r = 0x00 (single zero byte)", der(&[0x00], &ok_s)));
    c.push(("both zero-length", der(&[], &[])));
    c.push(("r = all zeroes, 32 bytes", der(&[0x00; 32], &ok_s)));

    // ── structural corruption that must be REJECTED identically.
    let mut t1 = base.clone(); t1[0] = 0x31; c.push(("wrong SEQUENCE tag", t1));
    let mut t2 = base.clone(); t2[2] = 0x03; c.push(("wrong R INTEGER tag", t2));
    let mut t3 = base.clone(); let p = 4 + ok_r.len(); t3[p] = 0x04; c.push(("wrong S INTEGER tag", t3));
    let mut t4 = base.clone(); t4[3] = 0xff; c.push(("R length exceeds input", t4));
    c.push(("empty input", vec![]));
    c.push(("SEQUENCE tag only", vec![0x30]));
    c.push(("SEQUENCE with zero length", vec![0x30, 0x00]));
    c.push(("truncated mid-R", base[..6].to_vec()));

    c
}

#[test]
fn historical_violation_classes_agree_with_libsecp() {
    let mut bad = 0;
    let all = cases();
    for (label, der_bytes) in &all {
        let ours = parse_der_lax(der_bytes);
        let theirs = reference(der_bytes);
        let agree = match (ours, theirs) {
            (None, None) => true,
            (Some(a), Some(b)) => a == b,
            // Core returns 1 with an all-zero signature on overflow. A reference that rejects
            // instead is a genuine semantic split and must be reported, not absorbed.
            (Some(a), None) if a == [0u8; 64] => {
                eprintln!("OVERFLOW SPLIT [{label}]: ours = zero-sig (Core returns 1), reference rejects");
                false
            }
            (o, t) => {
                eprintln!("DISAGREEMENT [{label}]");
                eprintln!("  der    = {}", hex(der_bytes));
                eprintln!("  ours   = {}", o.map(|v| hex(&v)).unwrap_or_else(|| "reject".into()));
                eprintln!("  theirs = {}", t.map(|v| hex(&v)).unwrap_or_else(|| "reject".into()));
                false
            }
        };
        if !agree { bad += 1; }
    }
    eprintln!("checked {} constructed violation cases", all.len());
    assert_eq!(bad, 0, "{bad} of {} historical-violation cases disagreed", all.len());
}

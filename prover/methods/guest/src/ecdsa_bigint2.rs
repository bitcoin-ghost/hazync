// hazync#139 EXPERIMENT — wholesale bigint2 ECDSA verification. GUEST ONLY.
//
// ⛔ THIS FILE CANNOT BE HOST-TESTED. `risc0-crypto`'s bigint2 backend calls the `sys_bigint2_3` /
// `sys_bigint2_4` zkVM precompiles, which have no host definition — the crate compiles for a host
// target and then fails to LINK. Discovered the hard way; recorded so nobody re-attempts it.
//
// The consequence for testing: the DER parser lives in `ecdsa_der.rs` precisely because it is pure
// and can be differentially tested on the host against `from_der_lax`. Everything that needs the
// precompile lives here, and its differential runs IN-GUEST in execute mode — which needs no GPU,
// because agreement is a correctness question, not a timing one.

use crate::ecdsa_der::parse_der_lax;
// ─────────────────────────────────────────────────────────────────────────────────────────────────
// The verify path. Mirrors `CPubKey::Verify` step for step:
//     secp256k1_ec_pubkey_parse -> ecdsa_signature_parse_der_lax -> normalize_s -> verify
// Every deviation from that order or those acceptance rules is a consensus divergence.

use risc0_crypto::curves::secp256k1::{Affine, Config, Fq, Fr};
use risc0_crypto::ecdsa::Signature;

/// Field prime p, big-endian — the range `secp256k1_fe_set_b32_limit` enforces on x and y.
const P: [u8; 32] = [
    0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,
    0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFE,0xFF,0xFF,0xFC,0x2F,
];

fn ge_field_prime(v: &[u8]) -> bool {
    for i in 0..32 {
        if v[i] < P[i] { return false; }
        if v[i] > P[i] { return true; }
    }
    true
}

/// Port of libsecp's `secp256k1_eckey_pubkey_parse`.
///
/// ⚠ ACCEPTS MORE THAN THE OBVIOUS TWO FORMS, and that is not an oversight in libsecp:
///   33 bytes, tag 0x02/0x03  — compressed, tag gives y's parity
///   65 bytes, tag 0x04       — uncompressed, x‖y
///   65 bytes, tag 0x06/0x07  — HYBRID: carries y in full AND encodes its parity in the tag, which
///                              must agree. `CPubKey::GetLen` returns 65 for these, so `IsValid()`
///                              lets them through and they can appear in a script. The bake-off arm
///                              this path grew out of handled only the compressed case; a wholesale
///                              swap that did the same would silently reject a hybrid key.
/// x and y are rejected at or above the field prime, matching `secp256k1_fe_set_b32_limit`.
fn parse_pubkey(pk: &[u8]) -> Option<Affine> {
    match (pk.len(), pk.first().copied()) {
        (33, Some(tag @ (0x02 | 0x03))) => {
            if ge_field_prime(&pk[1..33]) { return None; }
            Affine::decompress(Fq::from_be_bytes_mod_order(&pk[1..33]), tag == 0x03)
        }
        (65, Some(tag @ (0x04 | 0x06 | 0x07))) => {
            if ge_field_prime(&pk[1..33]) || ge_field_prime(&pk[33..65]) { return None; }
            // Hybrid tags must agree with y's actual parity; libsecp checks this and so must we.
            if tag != 0x04 && ((pk[64] & 1) == 1) != (tag == 0x07) { return None; }
            Affine::new_in_subgroup(
                Fq::from_be_bytes_mod_order(&pk[1..33]),
                Fq::from_be_bytes_mod_order(&pk[33..65]),
            )
        }
        _ => None,
    }
}

/// The wholesale replacement for `CPubKey::Verify`.
///
/// ⛔ EXPERIMENTAL (hazync#139). Built to be measured against libsecp, not to decide validity.
pub fn verify_wholesale(pk: &[u8], sig_der: &[u8], msg32: &[u8; 32]) -> bool {
    let Some(q) = parse_pubkey(pk) else { return false };
    let Some(compact) = parse_der_lax(sig_der) else { return false };

    // `parse_der_lax` already rejected r,s >= n by zeroing, so the mod-order reduction below is a
    // no-op rather than a silent acceptance of an out-of-range scalar. That ordering is load-bearing:
    // reducing first would make an out-of-range signature verify as a different, in-range one.
    let r = Fr::from_be_bytes_mod_order(&compact[..32]);
    let s = Fr::from_be_bytes_mod_order(&compact[32..]);
    let Some(sig) = Signature::<Config, 8>::new(r, s) else { return false };

    // Core normalises before verifying because libsecp REJECTS high-S. risc0-crypto's verify accepts
    // either (ECDSA is malleable, so both give the same answer), but mirroring Core keeps the step
    // order identical and removes the question.
    sig.normalized_s().verify(&q, msg32)
}

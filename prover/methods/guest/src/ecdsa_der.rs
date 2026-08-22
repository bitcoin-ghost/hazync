// hazync#139 EXPERIMENT — wholesale bigint2 ECDSA, built to be MEASURED, not shipped.
//
// `CPubKey::Verify` is pubkey parse -> lax DER parse -> low-S normalise -> group arithmetic.
// The middle path swaps only the last step and keeps Core's parsing; wholesale replaces all of it,
// which is where its extra speed comes from (parse is 201,558 cycles/verify under libsecp against
// 47,276 under bigint2) and also where its entire risk sits.
//
// ⛔ THE POINT OF THIS FILE IS THE DIFFERENTIAL, NOT THE SPEED. Every function here has a libsecp
// counterpart that is the authority. `hz_ecdsa_verify_differential` runs both and reports
// disagreement, so "what wholesale costs us" is a COUNTED number over real chain data rather than
// an argument. Nothing here should decide a block's validity until that count is zero over a
// meaningful span of history.
//
// ⚠ The lax parser below is transcribed from Bitcoin Core's `ecdsa_signature_parse_der_lax`
// line for line, deliberately NOT rewritten idiomatically. It exists to accept the format
// violations present in the chain BEFORE BIP66 activation (block 363,725) — negative integers,
// excessive padding, garbage at the end, overly long length descriptors. After BIP66 signatures
// are strict DER, so these quirks only bite on historical blocks — which is exactly what a
// genesis-anchored chain proof spends most of its time on.

/// Faithful port of Core's `ecdsa_signature_parse_der_lax`.
/// Returns the 64-byte compact (r||s) form, or None where Core returns 0.
///
/// Core returns 1 with a deliberately-invalid all-zero signature on overflow rather than failing;
/// that is reproduced as `Some([0u8; 64])`, because the caller's behaviour differs between the two
/// and collapsing them would silently change which inputs are rejected.
pub fn parse_der_lax(input: &[u8]) -> Option<[u8; 64]> {
    let inputlen = input.len();
    let mut pos: usize = 0;
    let mut tmpsig = [0u8; 64];
    let mut overflow = false;

    // Sequence tag byte
    if pos == inputlen || input[pos] != 0x30 { return None; }
    pos += 1;

    // Sequence length bytes
    if pos == inputlen { return None; }
    let mut lenbyte = input[pos] as usize; pos += 1;
    if lenbyte & 0x80 != 0 {
        lenbyte -= 0x80;
        if lenbyte > inputlen - pos { return None; }
        pos += lenbyte;
    }

    // Integer tag byte for R
    if pos == inputlen || input[pos] != 0x02 { return None; }
    pos += 1;

    // Integer length for R
    if pos == inputlen { return None; }
    let mut lenbyte = input[pos] as usize; pos += 1;
    let rlen: usize;
    if lenbyte & 0x80 != 0 {
        lenbyte -= 0x80;
        if lenbyte > inputlen - pos { return None; }
        while lenbyte > 0 && input[pos] == 0 { pos += 1; lenbyte -= 1; }
        if lenbyte >= 4 { return None; }
        let mut r = 0usize;
        while lenbyte > 0 { r = (r << 8) + input[pos] as usize; pos += 1; lenbyte -= 1; }
        rlen = r;
    } else {
        rlen = lenbyte;
    }
    if rlen > inputlen - pos { return None; }
    let mut rpos = pos;
    let mut rlen = rlen;
    pos += rlen;

    // Integer tag byte for S
    if pos == inputlen || input[pos] != 0x02 { return None; }
    pos += 1;

    // Integer length for S
    if pos == inputlen { return None; }
    let mut lenbyte = input[pos] as usize; pos += 1;
    let slen: usize;
    if lenbyte & 0x80 != 0 {
        lenbyte -= 0x80;
        if lenbyte > inputlen - pos { return None; }
        while lenbyte > 0 && input[pos] == 0 { pos += 1; lenbyte -= 1; }
        if lenbyte >= 4 { return None; }
        let mut s = 0usize;
        while lenbyte > 0 { s = (s << 8) + input[pos] as usize; pos += 1; lenbyte -= 1; }
        slen = s;
    } else {
        slen = lenbyte;
    }
    if slen > inputlen - pos { return None; }
    let mut spos = pos;
    let mut slen = slen;

    // Ignore leading zeroes in R
    while rlen > 0 && input[rpos] == 0 { rlen -= 1; rpos += 1; }
    if rlen > 32 { overflow = true; } else { tmpsig[32 - rlen..32].copy_from_slice(&input[rpos..rpos + rlen]); }

    // Ignore leading zeroes in S
    while slen > 0 && input[spos] == 0 { slen -= 1; spos += 1; }
    if slen > 32 { overflow = true; } else { tmpsig[64 - slen..64].copy_from_slice(&input[spos..spos + slen]); }

    // Core defers the range check to secp256k1_ecdsa_signature_parse_compact, which rejects r or s
    // >= the group order. Mirrored here so an out-of-range value overflows rather than being
    // silently accepted with different semantics.
    if !overflow && (ge_group_order(&tmpsig[0..32]) || ge_group_order(&tmpsig[32..64])) {
        overflow = true;
    }
    if overflow { tmpsig = [0u8; 64]; }
    Some(tmpsig)
}

/// secp256k1 group order n, big-endian.
const N: [u8; 32] = [
    0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFF,0xFE,
    0xBA,0xAE,0xDC,0xE6,0xAF,0x48,0xA0,0x3B,0xBF,0xD2,0x5E,0x8C,0xD0,0x36,0x41,0x41,
];

fn ge_group_order(v: &[u8]) -> bool {
    for i in 0..32 {
        if v[i] < N[i] { return false; }
        if v[i] > N[i] { return true; }
    }
    true // equal to n is also out of range
}

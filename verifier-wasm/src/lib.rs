//! `hazync-verify-wasm` — the verifier, in a browser.
//!
//! Why this exists: the claim is that anyone can check Bitcoin's chain validity on low-compute
//! hardware. A downloadable binary makes that true for people who will run an unsigned binary from
//! the internet on a machine they own. This makes it true for someone holding a phone.
//!
//! Crucially it is **local verification, not a verification service**. If a device asks a server
//! whether a proof is valid, the device trusts the server and the entire trust argument collapses —
//! we would have replaced "trust Bitcoin Core's developers" with "trust our API", which is worse than
//! the status quo, not better. The proof is checked on the device, by this module, with no network.
//!
//! It calls the same `hazync_verify::verify` as the CLI and the C ABI. There is no second
//! implementation of the anchoring rules here — see `../verifier/src/lib.rs`.
//!
//! ## Deliberately no wasm-bindgen
//!
//! The ABI is raw: two exported functions and linear memory. wasm-bindgen would be more ergonomic,
//! but it requires a version-matched `wasm-bindgen-cli` at build time to generate JS glue, and that
//! is a codegen step standing between this source and the artifact people are asked to trust. A
//! verifier's whole job is to be checkable. `cargo build --target wasm32-unknown-unknown` and nothing
//! else means the .wasm can be rebuilt and compared byte-for-byte by anyone, with no extra tooling.
//!
//! The cost is ~40 lines of hand-written JS in `hazync-verify.js`, which is a good trade.
//!
//! ## ABI
//!
//! ```text
//!   alloc(len: u32) -> ptr           reserve `len` bytes for the caller to write the proof into
//!   verify(ptr: u32, len: u32) -> p  verify; returns a pointer to [u32 little-endian length][UTF-8 JSON]
//! ```
//!
//! The result JSON always carries a `status` field mirroring the CLI's exit codes:
//!
//! ```text
//!   "verified"      the proof is valid AND genesis-anchored      (CLI exit 0)
//!   "invalid"       forged, tampered, corrupt, or wrong guest    (CLI exit 1)
//!   "not_anchored"  cryptographically valid, but a mid-chain segment (CLI exit 2)
//! ```
//!
//! `not_anchored` is a refusal, not a success — but it is reported distinctly because the proof-party
//! board links every range to its proof, so segment proofs are the most common thing anyone will drop
//! into this. Calling a perfectly good segment proof "forged" would make the board look broken.

use hazync_verify::{verify, VerifyError, METHOD_ID_HEX};

/// Reserve `len` bytes and hand the caller a pointer to write into.
///
/// The Vec is deliberately leaked: the caller owns this memory until it passes the pointer back to
/// `verify`, which reclaims it. A browser tab verifying a handful of proofs does not need a free
/// function, and adding one would add a use-after-free to a security tool for no benefit.
#[no_mangle]
pub extern "C" fn alloc(len: usize) -> *mut u8 {
    let mut buf = Vec::with_capacity(len);
    let ptr = buf.as_mut_ptr();
    std::mem::forget(buf);
    ptr
}

fn escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 16);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

/// Return a length-prefixed UTF-8 string to the caller: `[u32 LE length][bytes]`.
fn ret(s: String) -> *const u8 {
    let bytes = s.into_bytes();
    let mut out = Vec::with_capacity(4 + bytes.len());
    out.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
    out.extend_from_slice(&bytes);
    let ptr = out.as_ptr();
    std::mem::forget(out);
    ptr
}

#[no_mangle]
pub extern "C" fn verify_proof(ptr: *mut u8, len: usize) -> *const u8 {
    // Reclaim the allocation made by `alloc`. `from_raw_parts` requires the same capacity that was
    // requested, which is `len` — `Vec::with_capacity` may over-allocate, but `alloc` is only ever
    // called with the exact proof length, so the caller's contract holds.
    let bytes = unsafe { Vec::from_raw_parts(ptr, len, len) };

    ret(match verify(&bytes) {
        Ok(v) => {
            // to_json() is the CLI's --json output verbatim, so a browser and a shell describe the
            // same proof identically. Only the status tag is added.
            let body = v.to_json();
            let inner = body.trim_start_matches('{').trim_end_matches('}');
            format!("{{\n  \"status\": \"verified\",{inner}}}")
        }
        Err(VerifyError::Invalid(m)) => format!(
            "{{\"status\": \"invalid\", \"guest_image_id\": \"{METHOD_ID_HEX}\", \"error\": \"{}\"}}",
            escape(&m)
        ),
        Err(VerifyError::NotAnchored { lo, hi, detail }) => format!(
            "{{\"status\": \"not_anchored\", \"guest_image_id\": \"{METHOD_ID_HEX}\", \
             \"lo\": {lo}, \"hi\": {hi}, \"detail\": \"{}\"}}",
            escape(&detail)
        ),
    })
}

/// The guest image id this module verifies against, so a page can display it without hardcoding a
/// second copy that could drift from `reproduce/METHOD_ID`.
#[no_mangle]
pub extern "C" fn method_id() -> *const u8 {
    ret(METHOD_ID_HEX.to_string())
}

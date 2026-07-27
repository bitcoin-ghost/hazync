//! Extract the guest's pure-Rust consensus helpers from main.rs verbatim (zero drift), make them
//! `pub`, and emit them into $OUT_DIR/extracted.rs for the tests to include. If a signature or the
//! set of items ever changes, this build fails loudly rather than testing a stale copy.
use std::{env, fs, path::Path};

// The guest is more than one file, and items MOVE between them (block_script_flags and the two
// exception hashes migrated from main.rs into script_flags.rs during the chainparams carve). Search
// every guest source rather than one, or this harness silently rots the moment something is
// refactored — which is exactly what happened: it failed to build for want of BIP16_EXCEPTION.
const GUEST_SRCS: [&str; 2] = [
    "../prover/methods/guest/src/main.rs",
    "../prover/methods/guest/src/script_flags.rs",
];

/// Take a whole `const NAME ... ;` line, or a whole `fn NAME(...) { ... }` item (naive brace match;
/// valid here because none of the extracted items contain braces inside strings or comments).
fn extract(srcs: &[(String, String)], needle: &str, is_fn: bool) -> String {
    let (name, src) = srcs
        .iter()
        .find(|(_, s)| s.contains(needle))
        .unwrap_or_else(|| panic!("item not found in any guest source: {needle}"));
    let src = src.as_str();
    let _ = name;
    let start = src.find(needle).expect("checked by contains");
    if !is_fn {
        // These consts are single-line (a `;` also appears inside the `[u8; 32]` type, so grab the
        // whole line rather than up to the first `;`).
        let nl = src[start..].find('\n').unwrap_or(src.len() - start);
        return src[start..start + nl].to_string();
    }
    let bytes = src.as_bytes();
    let mut i = start + src[start..].find('{').expect("fn missing {");
    let mut depth = 0i32;
    loop {
        match bytes[i] {
            b'{' => depth += 1,
            b'}' => {
                depth -= 1;
                if depth == 0 {
                    break;
                }
            }
            _ => {}
        }
        i += 1;
    }
    src[start..=i].to_string()
}

fn main() {
    let srcs: Vec<(String, String)> = GUEST_SRCS
        .iter()
        .map(|f| {
            println!("cargo:rerun-if-changed={f}");
            (f.to_string(), fs::read_to_string(f).unwrap_or_else(|e| panic!("cannot read {f}: {e}")))
        })
        .collect();

    // block_script_flags references the SCRIPT_VERIFY_* bit positions and the buried-deployment
    // heights, so those must come across too — extracted verbatim from the same source, never
    // re-typed here, or the test would be checking constants that had drifted from the guest.
    let items = [
        extract(&srcs, "const BIP16_EXCEPTION", false),
        extract(&srcs, "const TAPROOT_EXCEPTION", false),
        extract(&srcs, "const P2SH", false),
        extract(&srcs, "const DERSIG", false),
        extract(&srcs, "const NULLDUMMY", false),
        extract(&srcs, "const CLTV", false),
        extract(&srcs, "const CSV: u32", false),
        extract(&srcs, "const WITNESS", false),
        extract(&srcs, "const TAPROOT: u32", false),
        extract(&srcs, "const BIP66_HEIGHT", false),
        extract(&srcs, "const BIP65_HEIGHT", false),
        extract(&srcs, "const CSV_HEIGHT", false),
        extract(&srcs, "const SEGWIT_HEIGHT", false),
        extract(&srcs, "fn block_script_flags", true),
        extract(&srcs, "fn add256", true),
        extract(&srcs, "fn median_time_past", true),
    ];
    // Make each item `pub` so the test module can reach it.
    let body: String = items
        .iter()
        .map(|s| {
            let s = s.trim_start();
            if s.starts_with("pub ") {
                format!("{s}\n\n")            // already public (script_flags.rs)
            } else if s.starts_with("const ") || s.starts_with("fn ") {
                format!("pub {s}\n\n")
            } else {
                format!("{s}\n\n")
            }
        })
        .collect();

    let out = Path::new(&env::var("OUT_DIR").unwrap()).join("extracted.rs");
    fs::write(&out, body).unwrap();
}

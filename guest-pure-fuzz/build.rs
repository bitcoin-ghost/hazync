//! Extract the guest's pure-Rust consensus helpers from main.rs verbatim (zero drift), make them
//! `pub`, and emit them into $OUT_DIR/extracted.rs for the tests to include. If a signature or the
//! set of items ever changes, this build fails loudly rather than testing a stale copy.
use std::{env, fs, path::Path};

const GUEST_MAIN: &str = "../prover/methods/guest/src/main.rs";

/// Take a whole `const NAME ... ;` line, or a whole `fn NAME(...) { ... }` item (naive brace match;
/// valid here because none of the extracted items contain braces inside strings or comments).
fn extract(src: &str, needle: &str, is_fn: bool) -> String {
    let start = src.find(needle).unwrap_or_else(|| panic!("item not found in main.rs: {needle}"));
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
    println!("cargo:rerun-if-changed={GUEST_MAIN}");
    let src = fs::read_to_string(GUEST_MAIN)
        .unwrap_or_else(|e| panic!("cannot read {GUEST_MAIN}: {e}"));

    let items = [
        extract(&src, "const BIP16_EXCEPTION", false),
        extract(&src, "const TAPROOT_EXCEPTION", false),
        extract(&src, "fn block_script_flags", true),
        extract(&src, "fn add256", true),
        extract(&src, "fn median_time_past", true),
    ];
    // Make each item `pub` so the test module can reach it.
    let body: String = items
        .iter()
        .map(|s| {
            let s = s.trim_start();
            if s.starts_with("const ") {
                format!("pub {s}\n\n")
            } else if s.starts_with("fn ") {
                format!("pub {s}\n\n")
            } else {
                format!("{s}\n\n")
            }
        })
        .collect();

    let out = Path::new(&env::var("OUT_DIR").unwrap()).join("extracted.rs");
    fs::write(&out, body).unwrap();
}

// Copyright 2025 RISC Zero, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#[cfg(feature = "prove")]
fn generate_zkr_table() {
    use std::fmt::Write as _;
    use std::path::Path;

    use liblzma::read::XzDecoder;

    let mut output = String::new();

    writeln!(&mut output, "const ZKRS: &[(&[u8], usize)] = &[").unwrap();

    // Pre-calculate the sizes of these to help make decoding faster later. This is a bit
    // quick-and-dirty, probably better to invent some way for zirgen bootstrap to save this
    // information.
    for po2 in 14..=18 {
        let zkr_name = format!("keccak_lift_{po2}.zkr.xz");
        let zkr_path = std::fs::canonicalize(Path::new("src/prove").join(&zkr_name)).unwrap();
        println!("cargo:rerun-if-changed={}", zkr_path.display());
        let mut decoder = XzDecoder::new(std::fs::File::open(&zkr_path).unwrap());
        std::io::copy(&mut decoder, &mut std::io::sink()).unwrap();
        let size = decoder.total_out();
        // HAZYNC_WINDOWS_PATH_ESCAPE -- hazync#616. This interpolated an ABSOLUTE PATH straight
        // into a Rust string literal. On Linux that is harmless; on Windows `canonicalize` returns
        // the extended-length form `\\?\C:\Users\...` and every `\U`, `\r`, `\k` becomes an
        // invalid character escape. Measured, run 36907036212: 40 errors, e.g.
        //
        //   error: unknown character escape: `k`
        //    --> .../out/zkr_table.rs:6:133
        //      (include_bytes!("\\?\C:\Users\runneradmin\.cargo\...\keccak_lift_18.zkr.xz"), ...)
        //
        // rustc's own suggestion is a raw string literal; escaping the backslashes is equivalent and
        // needs no reasoning about whether the path itself contains a quote.
        //
        // ⛔ LINUX OUTPUT IS BYTE-IDENTICAL: a path with no backslashes makes `replace` a no-op, and
        // the generated zkr_table.rs is diffed against the pre-patch baseline to prove it.
        //
        // ⚠ UPSTREAM BUG, not a local preference -- worth reporting. And note what it costs: a
        // Windows contributor must compile a circuit this project never executes (the Bitcoin guest
        // uses SHA-256 and RIPEMD-160, never Keccak), and it does not compile.
        let zkr_lit = zkr_path.display().to_string().replace('\\', "\\\\");
        writeln!(&mut output, "(include_bytes!(\"{zkr_lit}\"), {size}),").unwrap();
    }
    writeln!(&mut output, "];").unwrap();

    let out_dir = std::env::var_os("OUT_DIR").unwrap();
    std::fs::write(Path::new(&out_dir).join("zkr_table.rs"), &output).unwrap();
}

fn main() {
    #[cfg(feature = "prove")]
    generate_zkr_table();
}

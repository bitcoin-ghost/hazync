// Build the guest and embed its ELF + image id.
//
// The `rerun-if-changed` lines below are not boilerplate — they are the difference between a stale
// METHOD_ID and a correct one.
//
// Cargo's DEFAULT for a build script is "re-run if anything in this package changed", and that default
// is silently DISABLED the moment a build script emits any `rerun-if-changed` of its own. So this list
// has to be complete: `guest` and `build.rs` are here to restore what the default covered, not as
// decoration. Deleting either re-introduces the bug in the other direction.
//
// What the default never covered is the reason this exists. Since #88 the guest `#[path]`-includes
// source from OUTSIDE this package (`coinbase-smt/src/roots.rs`, `bip30.rs`). Cargo does not watch
// those, so editing the SMT would leave the previously-built guest ELF in place and the build would
// report success while the id silently described the OLD source. A wrong id that looks like a clean
// build is worse than a build failure.
//
// The includes are PARSED from the guest rather than listed here, because a hand-maintained copy of
// that list is exactly what goes stale — and it would go stale in the direction that fails silently.
// `scripts/check-guest-inputs.sh` independently asserts that every `#[path]` include is covered here.

use std::path::{Path, PathBuf};

/// Every file the guest pulls in via `#[path]`, resolved the way rustc resolves them: relative to the
/// directory of the file carrying the attribute.
fn path_includes(main_rs: &Path) -> Vec<PathBuf> {
    let src = match std::fs::read_to_string(main_rs) {
        Ok(s) => s,
        // Not this build script's job to diagnose a missing guest — the guest build will say so, far
        // more clearly than a panic in here would.
        Err(_) => return Vec::new(),
    };
    let base = main_rs.parent().unwrap_or(Path::new("."));
    let mut out = Vec::new();
    for line in src.lines() {
        let line = line.trim();
        // `#[path = "…"]`. Deliberately not a general Rust parser: this is the only form the guest
        // uses, and check-guest-inputs.sh fails the build if that stops being true.
        let Some(rest) = line.strip_prefix("#[path") else { continue };
        let Some(open) = rest.find('"') else { continue };
        let Some(len) = rest[open + 1..].find('"') else { continue };
        let rel = &rest[open + 1..open + 1 + len];
        out.push(base.join(rel));
    }
    out
}

fn main() {
    // Restores the default this script's own emissions would otherwise switch off.
    println!("cargo:rerun-if-changed=guest");
    println!("cargo:rerun-if-changed=build.rs");

    // ⭐ EMBED A PREBUILT GUEST INSTEAD OF BUILDING ONE (hazync#615).
    //
    // 📏 THE PROBLEM. The guest ELF carries the BUILD MACHINE'S PATHS inside it -- 8 occurrences on
    // one laptop, all panic and debug strings from dependencies:
    //
    //     /home/<user>/.cargo/registry/src/…/risc0-zkvm-3.0.5/src/guest/env
    //
    // so the image id is PATH-DEPENDENT, and `reproduce/Dockerfile`'s fixed WORKDIR is what makes it
    // reproducible. Measured 2026-09-30 from an unmodified tree with every flag unset: a native
    // build gives fb4d7352…, the container gives 37987b85…. Same source, same flags, different id.
    //
    // ⇒ That is why there is exactly ONE supported platform. It is not a toolchain limit: a macOS or
    // Windows host would embed a non-canonical guest and have every proof rejected (exit 78).
    //
    // ⭐ BUT THE GUEST IS PLATFORM-INDEPENDENT -- it is a RISC-V ELF, and the host's OS is irrelevant
    // to it. So it is built ONCE in the container and every platform's host embeds THOSE bytes. The
    // ELF never needs committing (70 MB) or publishing: it is a CI artifact passed between jobs.
    //
    // ⛔ AND THE ID IS CHECKED, NOT TRUSTED. The build REFUSES unless the id computed from the
    // supplied ELF equals reproduce/METHOD_ID. That is strictly stronger than the default path,
    // where a native build silently produces a different id and only CI notices, on one platform.
    println!("cargo:rerun-if-env-changed=HAZYNC_GUEST_ELF");
    if let Ok(elf_path) = std::env::var("HAZYNC_GUEST_ELF") {
        embed_prebuilt_guest(&elf_path);
        return;
    }

    let main_rs = Path::new("guest/src/main.rs");
    for inc in path_includes(main_rs) {
        // Emitted even if the file is missing: a deleted include must trigger a re-run, and a path
        // that never resolves is a guest build failure, which is the outcome we want anyway.
        println!("cargo:rerun-if-changed={}", inc.display());
    }

    // hazync#139 middle-path experiment (EXPERIMENTAL, opt-in). Unset — the overwhelmingly normal
    // case — takes the plain embed_methods() path below, byte-for-byte as before, so METHOD_ID is
    // unmoved. Set to 1 and the guest gains the `bigint2-ecdsa` feature, which compiles
    // guest/src/bigint2_ecmult.rs and MOVES METHOD_ID. See patches/0005 for the libsecp half.
    println!("cargo:rerun-if-env-changed=HAZYNC_BIGINT2_ECDSA");
    // hazync#205 / GHOST_GAINS G1: recover a pubkey's Y through the bigint2 coprocessor instead of
    // libsecp's software sqrt. Independent of the middle path -- it needs no witness plumbing, so it
    // can be enabled alone or alongside. See patches/0007 for the libsecp half.
    println!("cargo:rerun-if-env-changed=HAZYNC_LIFTX_ACCEL");

    // Accumulate rather than early-return per flag. The previous shape returned on the FIRST flag it
    // matched, so setting two silently built with only one -- the same class of silent default that
    // already cost a measurement day: merging #139 without patch 0005 builds stock libsecp and says
    // nothing, and #190 without its constant edit repacks nothing and says nothing.
    let mut features: Vec<String> = Vec::new();
    if std::env::var("HAZYNC_BIGINT2_ECDSA").as_deref() == Ok("1") {
        features.push("bigint2-ecdsa".to_string());
    }
    if std::env::var("HAZYNC_LIFTX_ACCEL").as_deref() == Ok("1") {
        features.push("liftx-accel".to_string());
    }
    // #136's read_slice fix for the aggregate. Host side is runtime-gated on the SAME variable.
    println!("cargo:rerun-if-env-changed=HAZYNC_AGG_READSLICE");
    if std::env::var("HAZYNC_AGG_READSLICE").as_deref() == Ok("1") {
        features.push("agg-readslice".to_string());
    }
    println!("cargo:rerun-if-env-changed=HAZYNC_MSM");
    if std::env::var("HAZYNC_MSM").as_deref() == Ok("1") {
        features.push("msm".to_string());
    }
    println!("cargo:rerun-if-env-changed=HAZYNC_FIELD_BENCH");
    if std::env::var("HAZYNC_FIELD_BENCH").as_deref() == Ok("1") {
        features.push("field-bench".to_string());
    }
    println!("cargo:rerun-if-env-changed=HAZYNC_LIFTX_HINT");
    println!("cargo:rerun-if-env-changed=HAZYNC_FIELD_BIGINT2");
    if std::env::var("HAZYNC_FIELD_BIGINT2").as_deref() == Ok("1") {
        features.push("field-bigint2".to_string());
    }
    // ⚠ HAZYNC_LIFTX_HINT must hold the SAME value at build time and at run time. The guest only
    // reads the hint block when built with the feature, so a mismatch desynchronises the input
    // stream rather than merely losing the optimisation.
    if std::env::var("HAZYNC_LIFTX_HINT").as_deref() == Ok("1") {
        features.push("liftx-hint".to_string());
    }
    println!("cargo:rerun-if-env-changed=HAZYNC_SCALAR_INV_ACCEL");
    if std::env::var("HAZYNC_SCALAR_INV_ACCEL").as_deref() == Ok("1") {
        features.push("scalar-inv-accel".to_string());
    }

    if !features.is_empty() {
        use std::collections::HashMap;
        // GuestOptions is #[non_exhaustive], so it cannot be built with a struct expression from
        // outside risc0-build; default-then-assign is the supported shape.
        let mut guest = risc0_build::GuestOptions::default();
        guest.features = features;
        let mut opts = HashMap::new();
        opts.insert("method", guest);
        risc0_build::embed_methods_with_options(opts);
        return;
    }

    risc0_build::embed_methods();
}

/// Emit `methods.rs` from an ELF built elsewhere, refusing anything but the canonical guest.
fn embed_prebuilt_guest(elf_path: &str) {
    use std::path::Path;

    println!("cargo:rerun-if-changed={elf_path}");
    let elf = std::fs::read(elf_path)
        .unwrap_or_else(|e| panic!("HAZYNC_GUEST_ELF={elf_path}: cannot read it: {e}"));
    if elf.is_empty() {
        panic!("HAZYNC_GUEST_ELF={elf_path}: the file is empty");
    }
    let digest = risc0_binfmt::compute_image_id(&elf)
        .unwrap_or_else(|e| panic!("HAZYNC_GUEST_ELF={elf_path}: not a usable guest ELF: {e}"));
    let got: String = digest.as_bytes().iter().map(|b| format!("{b:02x}")).collect();

    // ⚠ The pin lives at the repo root, two levels above this crate. Read it rather than hardcoding
    // the id here: one place to change, and it is the same file CI asserts against.
    let pin_path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../reproduce/METHOD_ID");
    println!("cargo:rerun-if-changed={}", pin_path.display());
    let pin_src = std::fs::read_to_string(&pin_path)
        .unwrap_or_else(|e| panic!("cannot read {}: {e}", pin_path.display()));
    let want = pin_src
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty() && !l.starts_with('#'))
        .unwrap_or_else(|| panic!("{} has no id line", pin_path.display()));

    // ⛔ THE WHOLE POINT. Embedding an unverified guest would let a build ship a program that is not
    // the one every published proof attests to, and the failure would surface only as rejected
    // proofs on someone else's machine.
    if got != want {
        panic!(
            "HAZYNC_GUEST_ELF={elf_path} is NOT the canonical guest.\n               its id : {got}\n  canonical: {want}\n               Build the guest with reproduce/Dockerfile and pass that ELF, or unset              HAZYNC_GUEST_ELF to build from source."
        );
    }

    let words = digest.as_words();
    let ids = words.iter().map(|w| w.to_string()).collect::<Vec<_>>().join(", ");
    let abs = std::fs::canonicalize(elf_path)
        .unwrap_or_else(|e| panic!("cannot resolve {elf_path}: {e}"));
    let out = Path::new(&std::env::var("OUT_DIR").expect("OUT_DIR")).join("methods.rs");
    std::fs::write(
        &out,
        format!(
            "pub const METHOD_ELF: &[u8] = include_bytes!({:?});\n\
             pub const METHOD_PATH: &str = {:?};\n\
             pub const METHOD_ID: [u32; 8] = [{}];\n",
            abs, abs.to_string_lossy(), ids
        ),
    )
    .unwrap_or_else(|e| panic!("cannot write {}: {e}", out.display()));

    println!("cargo:warning=methods: embedded the prebuilt canonical guest ({got}) from {elf_path}");
}

// Build the guest's REAL consensus C++ natively (x86-64) so its leaf builders can be called from a
// test (hazync#50).
//
// Adapted from prover/methods/guest/build.rs, which is the authority — this file must track it for
// the source LIST, because a test that compiles a different set of translation units than the guest
// is not testing the guest. Two deliberate differences, and only two:
//
//   * no -march=rv32im / cross toolchain: the whole point is to run on the host;
//   * no cshims.c: those are the FREESTANDING libc shims the bare-metal guest needs, and linking
//     them here would shadow the real libc.
//
// The -ffile-prefix-map is kept even though METHOD_ID reproducibility is irrelevant here, so the two
// builds stay as close as possible.

// Compile the REAL Bitcoin Core script-validation engine (+ libsecp256k1) into the RISC0 guest.
// Portable: consensus source from $HAZYNC_BASE (fallback: local scratchpad), riscv toolchain
// discovered under $RISC0_HOME, lib paths derived from gcc itself (robust to toolchain versions).
use std::path::PathBuf;


// dirname of `gcc -march=rv32im -mabi=ilp32 <query>` (matches the multilib we build against)

fn main() {
    // Consensus source root: Bitcoin Core + secp256k1 + the coreshim, laid out by provision-vps.sh.
    // Set HAZYNC_BASE to point at it; the default matches provision's WORK dir ($HOME/hazync-build).
    let base = std::env::var("HAZYNC_BASE").unwrap_or_else(|_| {
        format!("{}/hazync-build", std::env::var("HOME").unwrap_or_default())
    });
    let secp = format!("{base}/secp256k1");
    let core = format!("{base}/bitcoin-core/src");
    let shim = format!("{base}/coreshim");

    // Guard a STALE HAZYNC_BASE. provision-vps.sh copies coreshim/*.h into $HAZYNC_BASE/coreshim, but a
    // base provisioned BEFORE a shim was added (e.g. logging.h, added for the chainparams carve) silently
    // lacks it, the build falls back to Core's real header, and it dies deep in the C++ compile with a
    // cryptic error ("'StdLockGuard' was not declared", etc.). Fail fast, clearly, and actionably instead:
    // every shim the repo ships (the source of truth, next to this crate) must exist in the base.
    let repo_shim = format!("{}/../coreshim", std::env::var("CARGO_MANIFEST_DIR").unwrap());
    if let Ok(entries) = std::fs::read_dir(&repo_shim) {
        for e in entries.flatten() {
            let n = e.file_name();
            let is_h = std::path::Path::new(&n).extension().map_or(false, |x| x == "h");
            if is_h && !std::path::Path::new(&format!("{shim}/{}", n.to_string_lossy())).exists() {
                panic!(
                    "coreshim is stale: {shim} is missing '{}' (the repo ships it in coreshim/). Your \
                     HAZYNC_BASE was provisioned before this shim existed. Re-run provision-vps.sh — it \
                     copies coreshim/*.h into $HAZYNC_BASE/coreshim.",
                    n.to_string_lossy()
                );
            }
        }
    }

    // Reproducible builds: remap the absolute source root to a fixed virtual path so __FILE__ and
    // debug strings baked into the compiled Core/secp objects don't carry $HAZYNC_BASE / the build
    // machine's home dir — which would otherwise change the guest image id (METHOD_ID) per machine.
    let fpm = format!("-ffile-prefix-map={base}=/hazync");


    // ECMULT_WINDOW_SIZE tuning (issue #12): 19 is the measured cycle optimum — ~1.9% fewer guest cycles
    // than libsecp's default 15 (block 130000), and beyond 19 the resident pre_g table's paging cost
    // overtakes the wNAF saving. The checked-in precomputed_ecmult.c is generated for windows <=15 and
    // HARD-ERRORS above that (`#if ECMULT_WINDOW_SIZE > 15 -> #error`), so for a larger window we regenerate
    // it with libsecp's OWN generator (src/precompute_ecmult.c). That output is deterministic EC math —
    // byte-identical on any machine — so the guest image id (METHOD_ID) stays reproducible. Regenerate only
    // when the on-disk table isn't already ours (keeps incremental builds fast; the reproduce container
    // starts from a fresh <=15 clone and regenerates once).
    const ECMULT_WINDOW: u32 = 19;
    let win = ECMULT_WINDOW.to_string();
    if ECMULT_WINDOW > 15 {
        let pc = format!("{secp}/src/precomputed_ecmult.c");
        let want = format!("#if ECMULT_WINDOW_SIZE > {ECMULT_WINDOW}");
        let up_to_date = std::fs::read_to_string(&pc).map_or(false, |s| s.contains(&want));
        if !up_to_date {
            let out = std::env::var("OUT_DIR").unwrap();
            let genbin = format!("{out}/precompute_ecmult");
            let host_cc = std::env::var("HOST_CC").unwrap_or_else(|_| "cc".into());
            let compiled = std::process::Command::new(&host_cc)
                .current_dir(&secp)
                .args(["-O2", &format!("-DECMULT_WINDOW_SIZE={ECMULT_WINDOW}"),
                       "-DENABLE_MODULE_SCHNORRSIG=1", "-DENABLE_MODULE_EXTRAKEYS=1", "-DVERIFY",
                       "-I.", "-Isrc", "-Iinclude", "src/precompute_ecmult.c", "-o", &genbin])
                .status().expect("compile precompute_ecmult (host cc)");
            assert!(compiled.success(), "failed to compile libsecp's ecmult table generator");
            let ran = std::process::Command::new(&genbin).current_dir(&secp).status()
                .expect("run precompute_ecmult");
            assert!(ran.success(), "failed to regenerate precomputed_ecmult.c for window {ECMULT_WINDOW}");
        }
    }

    // 1) REAL libsecp256k1 (C) + libc-glue shims.
    cc::Build::new()
        .opt_level(2).warnings(false)
        .flag(&fpm)
        .include(&secp).include(format!("{secp}/src"))
        .define("ECMULT_WINDOW_SIZE", win.as_str()).define("ECMULT_GEN_KB", "22")
        .define("ENABLE_MODULE_SCHNORRSIG", "1").define("ENABLE_MODULE_EXTRAKEYS", "1")
        // NOT defined natively (the guest defines it): the external callbacks live in cshims.c,
        // which is the freestanding libc this build deliberately omits. libsecp's own defaults are
        // used instead — an abort-on-internal-error policy, which cannot affect a leaf preimage.
        .file(format!("{secp}/src/secp256k1.c"))
        .file(format!("{secp}/src/precomputed_ecmult.c"))
        .file(format!("{secp}/src/precomputed_ecmult_gen.c"))
        .compile("secp256k1");

    // 2) REAL Bitcoin Core consensus C++ (interpreter + sighash + deps) + our wrapper.
    let core_tus = [
        "script/interpreter.cpp", "script/script.cpp", "script/script_error.cpp",
        "primitives/transaction.cpp", "pubkey.cpp", "hash.cpp", "uint256.cpp",
        "crypto/sha256.cpp", "crypto/sha512.cpp", "crypto/ripemd160.cpp",
        "crypto/sha1.cpp", "crypto/hmac_sha512.cpp", "util/strencodings.cpp",
        "crypto/hex_base.cpp",
        "consensus/tx_check.cpp",  // real CheckTransaction (structural consensus checks)
        "consensus/merkle.cpp",    // real ComputeMerkleRoot
        "arith_uint256.cpp",       // real SetCompact / target arithmetic for PoW
        "pow.cpp",                 // real CalculateNextWorkRequired (difficulty retarget)
        "chain.cpp",               // real CBlockIndex::GetAncestor / GetBlockProof (pow.cpp deps)
        "kernel/chainparams.cpp",  // real mainnet Consensus::Params (authoritative consensus constants)
        "primitives/block.cpp",    // CBlockHeader::GetHash (chainparams genesis) — pow/subsidy dep
        "util/chaintype.cpp",      // ChainType strings (chainparams dep)
    ];
    // The guest's Core tree carries two patches that are RIGHT for rv32 and do not compile natively:
    //   * serialize.h — an ILP32 shim adding bare `int`/`unsigned` Serialize overloads, because on
    //     riscv32 `int32_t` is `long` so `int` matches nothing. On LP64 `int32_t` IS `int`, so the
    //     shim is a redefinition and a hard error.
    //   * crypto/sha256.cpp — routed to the RISC0 SHA accelerator, which does not exist here.
    //
    // `prover/chainparams_check.sh` handles this by checking the stock files out over the patched
    // ones, compiling, and restoring on exit. That mutates a tree the guest build also reads, so a
    // concurrent build would see stock sources and silently produce a DIFFERENT guest. Here the stock
    // versions are extracted from git into OUT_DIR instead, and the shadow directory is placed first
    // on the include path. Nothing outside OUT_DIR is touched.
    let out = std::env::var("OUT_DIR").unwrap();
    let shadow = format!("{out}/shadow");
    std::fs::create_dir_all(format!("{shadow}/crypto")).unwrap();
    let stock = |rel: &str| -> Vec<u8> {
        let o = std::process::Command::new("git")
            .args(["-C", &format!("{base}/bitcoin-core"), "show", &format!("HEAD:src/{rel}")])
            .output()
            .unwrap_or_else(|e| panic!("git show HEAD:src/{rel} — the Core tree must be a git \
                                        checkout so the stock sources can be recovered: {e}"));
        assert!(o.status.success(),
            "git show HEAD:src/{rel} failed. The native build needs the UNPATCHED file; the patched \
             one in the worktree is rv32-only.");
        o.stdout
    };
    std::fs::write(format!("{shadow}/serialize.h"), stock("serialize.h")).unwrap();
    let stock_sha = format!("{shadow}/crypto/sha256.cpp");
    std::fs::write(&stock_sha, stock("crypto/sha256.cpp")).unwrap();

    let mut b = cc::Build::new();
    b.cpp(true)
        .flag("-std=c++20")
        .flag("-fexceptions").flag("-fno-rtti").opt_level(2).warnings(false)
        .flag(&fpm)
        // coreshim FIRST: its no-op sync.h/threadsafety.h override Core's pthread-backed versions so
        // the real chain.h CBlockIndex + pow.cpp compile on the single-threaded freestanding guest.
        .include(&shadow).include(&shim).include(&core).include(format!("{secp}/include"));
    for tu in core_tus {
        if tu == "crypto/sha256.cpp" { b.file(&stock_sha); } else { b.file(format!("{core}/{tu}")); }
    }
    // Core's SSE4/AVX2 SHA paths are separate TUs the guest never builds; natively, sha256.cpp's
    // runtime dispatch expects them. Disable dispatch instead of pulling them in — this is a hashing
    // BACKEND choice and cannot change the leaf bytes, which is the only thing under test.
    b.define("DISABLE_OPTIMIZED_SHA256", None);
    b.file("../prover/methods/guest/verify_input.cpp");

    // Tell cargo what this build actually depends on. Without these the C++ dependency is INVISIBLE:
    // on 2026-07-30 a change to verify_input.cpp's leaf commitment did not recompile — the guest ELF
    // was relinked from a verify_input.o three days stale, so the Rust half of the change took effect
    // and the C++ half did not. METHOD_ID still changed (the Rust did rebuild), which is the dangerous
    // part: a new id looked like proof the guest had been rebuilt, and it is not. The regression caught
    // it only because the two halves then disagreed about leaf hashes.
    //
    // Core's sources live outside this package, so cargo cannot infer them at all; the guest's own
    // files are covered by the default fingerprint but are listed anyway, because relying on a default
    // is what produced a three-day-old object.
    println!("cargo:rerun-if-changed=../prover/methods/guest/verify_input.cpp");
    
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src");
    for tu in core_tus { println!("cargo:rerun-if-changed={core}/{tu}"); }
    println!("cargo:rerun-if-changed={shim}");

    b.compile("bitcoinconsensus");

    // 3) C++ runtime. Natively this is just the system libstdc++ — the guest's newlib/nosys static
    //    link exists because it is freestanding, and reproducing it here would be linking a second
    //    libc next to the real one.
    println!("cargo:rustc-link-lib=dylib=stdc++");
}

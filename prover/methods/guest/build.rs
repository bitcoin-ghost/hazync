// Compile the REAL Bitcoin Core script-validation engine (+ libsecp256k1) into the RISC0 guest.
// Portable: consensus source from $HAZYNC_BASE (fallback: local scratchpad), riscv toolchain
// discovered under $RISC0_HOME, lib paths derived from gcc itself (robust to toolchain versions).
use std::path::PathBuf;
use std::process::Command;

fn find_riscv_bin() -> String {
    if let Ok(b) = std::env::var("HAZYNC_RISCV_BIN") {
        return b;
    }
    let home = std::env::var("RISC0_HOME")
        .unwrap_or_else(|_| format!("{}/.risc0", std::env::var("HOME").unwrap_or_default()));
    if let Ok(rd) = std::fs::read_dir(format!("{home}/toolchains")) {
        // Collect ALL matching cpp toolchains and pick deterministically (sorted) rather than taking the
        // first read_dir entry — filesystem/inode iteration order is not stable, so on a machine with more
        // than one cpp toolchain installed "first wins" would compile the guest with a source-dependent
        // toolchain and yield a NONDETERMINISTIC METHOD_ID. The sanctioned path (Docker reproduce/ image,
        // provision-vps.sh --force) installs exactly one; warn loudly if that invariant is ever broken.
        let mut cands: Vec<PathBuf> = rd
            .flatten()
            .map(|e| e.path().join("riscv32im-linux-x86_64/bin"))
            .filter(|c| c.join("riscv32-unknown-elf-gcc").exists())
            .collect();
        cands.sort();
        if cands.len() > 1 {
            println!(
                "cargo:warning=multiple riscv cpp toolchains under {home}/toolchains; using {} (sorted) — \
                 remove the others to guarantee a reproducible METHOD_ID: {:?}",
                cands[0].display(),
                cands
            );
        }
        if let Some(c) = cands.first() {
            return c.to_string_lossy().into_owned();
        }
    }
    String::new() // fall back to PATH
}

// dirname of `gcc -march=rv32im -mabi=ilp32 <query>` (matches the multilib we build against)
fn lib_dir(gcc: &str, query: &[&str]) -> String {
    let out = Command::new(gcc)
        .args(["-march=rv32im", "-mabi=ilp32"])
        .args(query)
        .output()
        .expect("run gcc for lib path");
    let p = String::from_utf8_lossy(&out.stdout).trim().to_string();
    PathBuf::from(p).parent().map(|d| d.to_string_lossy().into_owned()).unwrap_or_default()
}

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
    let repo_shim = format!("{}/../../../coreshim", std::env::var("CARGO_MANIFEST_DIR").unwrap());
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

    let bin = find_riscv_bin();
    let pfx = if bin.is_empty() { String::new() } else { format!("{bin}/") };
    let gcc = format!("{pfx}riscv32-unknown-elf-gcc");
    let gpp = format!("{pfx}riscv32-unknown-elf-g++");
    let ar = format!("{pfx}riscv32-unknown-elf-gcc-ar");

    // ECMULT_WINDOW_SIZE tuning (issue #12). MEASURED on block 140,000, 212 inputs, execute mode
    // (2026-08-26 for 19/20 in TIER0_RESULTS_2026-08-26.md, 2026-08-28 for 21; same harness, same
    // journal digest 607f4a7e... on every arm, so each number prices the same computation):
    //
    //     19  376,662,184  (shipped)     20  375,914,975  -0.198%     21  371,971,773  -1.245%
    //
    // 21 is the optimum. It is the arm experiment E4 specified and never ran, and it is worth ~6x the
    // window-20 change that was going to ship instead.
    //
    // ⚠ Measure window arms at a REALISTIC input count. A sweep on block 130,000 (10 inputs) put 20
    // ABOVE 19 and read it as a local bump; at 212 inputs the curve falls monotonically 19 -> 21.
    // Ten inputs is too little EC work to amortise the pre_g table, so a toy block under-rates larger
    // windows. Production chunks carry 64-180 inputs.
    //
    // ⚠ The pre_g table doubles per step (~16 MB at 19 -> ~64 MB at 21). The cycle figures are net of
    // the paging that costs, but the guest memory footprint grows; check it against segment sizing.
    //
    // 19 remains the DEFAULT deliberately: changing it edits the guest source and therefore moves
    // METHOD_ID, so it must ride the next re-baselining rather than ship on its own.
    // Full verdict and consequences: docs/TOPOLOGY_AND_SETTINGS.md 4.1.
    //
    // The checked-in precomputed_ecmult.c is generated for windows <=15 and HARD-ERRORS above that
    // (`#if ECMULT_WINDOW_SIZE > 15 -> #error`), so for a larger window we regenerate it with libsecp's
    // OWN generator (src/precompute_ecmult.c). That output is deterministic EC math — byte-identical on
    // any machine — so the guest image id (METHOD_ID) stays reproducible. Regenerate only when the
    // on-disk table isn't already ours (keeps incremental builds fast; the reproduce container starts
    // from a fresh <=15 clone and regenerates once).
    //
    // ECMULT_GEN_KB is INERT for this workload — 2, 22 and 86 all produce bit-identical guest cycles.
    // It sizes the ecmult_gen table used to compute k*G when SIGNING; verification goes through
    // secp256k1_ecmult against pre_g, which ECMULT_WINDOW_SIZE sizes, and Hazync only ever verifies.
    // Exposed so that stays re-checkable, and so the 22 -> 2 memory saving can be taken for free during
    // the same re-baselining.
    // hazync#139 middle path. See prover/methods/guest/src/bigint2_ecmult.rs and patches/0005.
    println!("cargo:rerun-if-env-changed=HAZYNC_BIGINT2_ECDSA");
    let bigint2 = std::env::var("HAZYNC_BIGINT2_ECDSA").as_deref() == Ok("1");
    // hazync#205 / GHOST_GAINS G1. Same two-part shape as the middle path above, and the same trap:
    // patch 0007 ADDS the #ifdef to group_impl.h, this define is what turns it on. Either alone
    // compiles the stock sqrt and says nothing.
    println!("cargo:rerun-if-env-changed=HAZYNC_LIFTX_ACCEL");
    let liftx_accel = std::env::var("HAZYNC_LIFTX_ACCEL").as_deref() == Ok("1");
    // hazync#205 / G3 — Schnorr through the same accelerator. Needs bigint2 too: it reuses
    // hazync_ecmult_verify, which only the bigint2-ecdsa feature exports.
    println!("cargo:rerun-if-env-changed=HAZYNC_BIGINT2_SCHNORR");
    let schnorr_accel = std::env::var("HAZYNC_BIGINT2_SCHNORR").as_deref() == Ok("1");
    // The ECDSA scalar inverse, which patch 0005 left as literal libsecp. 7.0% of the current stack.
    println!("cargo:rerun-if-env-changed=HAZYNC_SCALAR_INV_ACCEL");
    let scalar_inv = std::env::var("HAZYNC_SCALAR_INV_ACCEL").as_deref() == Ok("1");
    // SHA Transform fast path: inline byte swap, and no staging copy when already aligned.
    println!("cargo:rerun-if-env-changed=HAZYNC_SHA_FASTPATH");
    let sha_fast = std::env::var("HAZYNC_SHA_FASTPATH").as_deref() == Ok("1");
    // Merkle-node double-SHA through the accelerated Transform. Different function from Transform;
    // patches/0002 never touched it. See patches/0010.
    println!("cargo:rerun-if-env-changed=HAZYNC_SHA_D64_ACCEL");
    let sha_d64 = std::env::var("HAZYNC_SHA_D64_ACCEL").as_deref() == Ok("1");

    println!("cargo:rerun-if-env-changed=HAZYNC_ECMULT_WINDOW");
    println!("cargo:rerun-if-env-changed=HAZYNC_ECMULT_GEN_KB");
    // TIER 0: default raised 19 -> 21, the measured optimum (-1.245%). This is a re-baselining
    // change -- it edits the guest image and therefore moves METHOD_ID -- so it ships only in the
    // batch, never on its own.
    let ecmult_window: u32 = std::env::var("HAZYNC_ECMULT_WINDOW")
        .map(|s| s.parse().expect("HAZYNC_ECMULT_WINDOW must be an integer"))
        .unwrap_or(21);
    let gen_kb = std::env::var("HAZYNC_ECMULT_GEN_KB").unwrap_or_else(|_| "22".into());
    let win = ecmult_window.to_string();
    if ecmult_window > 15 {
        let pc = format!("{secp}/src/precomputed_ecmult.c");
        let want = format!("#if ECMULT_WINDOW_SIZE > {ecmult_window}");
        let up_to_date = std::fs::read_to_string(&pc).map_or(false, |s| s.contains(&want));
        if !up_to_date {
            let out = std::env::var("OUT_DIR").unwrap();
            let genbin = format!("{out}/precompute_ecmult");
            let host_cc = std::env::var("HOST_CC").unwrap_or_else(|_| "cc".into());
            let compiled = std::process::Command::new(&host_cc)
                .current_dir(&secp)
                .args(["-O2", &format!("-DECMULT_WINDOW_SIZE={ecmult_window}"),
                       "-DENABLE_MODULE_SCHNORRSIG=1", "-DENABLE_MODULE_EXTRAKEYS=1", "-DVERIFY",
                       "-I.", "-Isrc", "-Iinclude", "src/precompute_ecmult.c", "-o", &genbin])
                .status().expect("compile precompute_ecmult (host cc)");
            assert!(compiled.success(), "failed to compile libsecp's ecmult table generator");
            let ran = std::process::Command::new(&genbin).current_dir(&secp).status()
                .expect("run precompute_ecmult");
            assert!(ran.success(), "failed to regenerate precomputed_ecmult.c for window {ecmult_window}");
        }
    } else {
        // Only the >15 branch above ever rewrites the table, so a previous >15 arm leaves ITS table on
        // disk and a sweep back down would silently compile against the wrong one — a guest that is
        // wrong rather than merely slow. Fail loudly instead of measuring a lie.
        let pc = format!("{secp}/src/precomputed_ecmult.c");
        let pristine = std::fs::read_to_string(&pc)
            .map_or(false, |s| s.contains("#if ECMULT_WINDOW_SIZE > 15"));
        assert!(pristine, "precomputed_ecmult.c has been regenerated for a window >15 and cannot be \
            reused at window {ecmult_window}; run `git checkout -- {pc}` before building");
    }

    // 1) REAL libsecp256k1 (C) + libc-glue shims.
    cc::Build::new()
        .compiler(&gcc).archiver(&ar)
        // TIER 0: -O3 (E1, -0.264%). Modest, as expected -- libsecp is hand-unrolled C and rv32im
        // has no vector unit, which is most of what -O3 adds over -O2. C/C++ -flto is NOT here and
        // is not an oversight: rust-lld cannot read GCC's LTO bytecode, and -ffat-lto-objects links
        // but performs no cross-TU optimisation. See TIER0_RESULTS_2026-08-26.md 3.
        .flag("-march=rv32im").flag("-mabi=ilp32").opt_level(3).warnings(false)
        .flag(&fpm)
        .include(&secp).include(format!("{secp}/src"))
        .define("ECMULT_WINDOW_SIZE", win.as_str()).define("ECMULT_GEN_KB", gen_kb.as_str())
        // hazync#139 middle-path EXPERIMENT — enables patch 0005's #ifdef'd block in ecdsa_impl.h.
        // Unset (the normal case) compiles the stock secp256k1_ecmult path and moves nothing.
        .define(
            "HAZYNC_BIGINT2_ECDSA",
            if bigint2 { Some("1") } else { None },
        )
        // hazync#205 G1 — enables patch 0007's #ifdef'd block in group_impl.h, routing the pubkey Y
        // recovery to the coprocessor. Unset compiles libsecp's own fe_sqrt and moves nothing.
        .define(
            "HAZYNC_LIFTX_ACCEL",
            if liftx_accel { Some("1") } else { None },
        )
        // G3 — enables patch 0006 in modules/schnorrsig/main_impl.h.
        .define(
            "HAZYNC_BIGINT2_SCHNORR",
            if schnorr_accel { Some("1") } else { None },
        )
        .define(
            "HAZYNC_SCALAR_INV_ACCEL",
            if scalar_inv { Some("1") } else { None },
        )
        .define(
            "HAZYNC_SHA_FASTPATH",
            if sha_fast { Some("1") } else { None },
        )
        .define(
            "HAZYNC_SHA_D64_ACCEL",
            if sha_d64 { Some("1") } else { None },
        )
        .define("ENABLE_MODULE_SCHNORRSIG", "1").define("ENABLE_MODULE_EXTRAKEYS", "1")
        .define("USE_EXTERNAL_DEFAULT_CALLBACKS", "1")
        .file(format!("{secp}/src/secp256k1.c"))
        .file(format!("{secp}/src/precomputed_ecmult.c"))
        .file(format!("{secp}/src/precomputed_ecmult_gen.c"))
        .file("cshims.c")
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
    let mut b = cc::Build::new();
    b.cpp(true).compiler(&gpp).archiver(&ar)
        .flag("-march=rv32im").flag("-mabi=ilp32").flag("-std=c++20")
        .flag("-fexceptions").flag("-fno-rtti").opt_level(3).warnings(false)  // TIER 0: -O3
        .flag(&fpm)
        // coreshim FIRST: its no-op sync.h/threadsafety.h override Core's pthread-backed versions so
        // the real chain.h CBlockIndex + pow.cpp compile on the single-threaded freestanding guest.
        .include(&shim).include(&core).include(format!("{secp}/include"));
    for tu in core_tus { b.file(format!("{core}/{tu}")); }
    b.file("verify_input.cpp");

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
    println!("cargo:rerun-if-changed=verify_input.cpp");
    println!("cargo:rerun-if-changed=cshims.c");
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=src");
    for tu in core_tus { println!("cargo:rerun-if-changed={core}/{tu}"); }
    println!("cargo:rerun-if-changed={shim}");
    // secp256k1's sources are compiled by the FIRST cc::Build above, and they live outside this
    // package exactly like Core's do — so cargo cannot infer them either. They were missing from this
    // list, which is the same staleness class the list exists to prevent (audit #5, N-1): a manual edit
    // under {secp}/src on a dev box would leave a stale object linked into a guest that reports a fresh
    // id. Release builds are container-fresh so the shipped path was never affected; a dev machine is
    // precisely where you would be editing these.
    for tu in ["secp256k1.c", "precomputed_ecmult.c", "precomputed_ecmult_gen.c"] {
        println!("cargo:rerun-if-changed={secp}/src/{tu}");
    }

    b.compile("bitcoinconsensus");

    // A DISCARDED re-compile of OUR OWN two translation units with the warnings turned back on.
    //
    // Both cc::Builds above set warnings(false), which passes `-w`. That is not laziness — Core and
    // libsecp are third-party trees that warn copiously, and their noise is not ours to fix. But `-w`
    // is global to the compile, so it silences our files too, and it silenced a real defect: coin_leaf
    // was declared `bool` and fell off the end of its success path with no `return true`. Control
    // reaching the end of a non-void function is UB, and coin_leaf_only branches on that return value
    // to decide whether to zero the leaf — so a VALID coin could be zeroed on a compiler's whim, which
    // in a zkVM means an honest block failing its accumulator delete non-deterministically.
    //
    // It survived a container build, CI, and an audit round, because nothing anywhere was looking.
    //
    // Doing this by adding `-Werror=return-type` to the builds above does NOT work, and that was
    // measured rather than assumed: `-w` beats it in EVERY flag order (`-w -Werror=return-type`,
    // `-Werror=return-type -w`, and with an explicit `-Wreturn-type` in between — all three compile the
    // broken file silently). GCC's `-w` inhibits the diagnostic outright, so promoting a warning that
    // is never issued promotes nothing. A separate compile is the only thing that actually fires.
    //
    // It lives in build.rs rather than a scripts/check-*.sh so it cannot be skipped: it runs on every
    // guest build — container, CI, and dev box alike — and needs no toolchain discovery of its own. The
    // objects go to a scratch path and are thrown away, so the guest ELF and METHOD_ID are untouched.
    // Only OUR files are checked; Core and libsecp keep their `-w`.
    let out = std::env::var("OUT_DIR").expect("OUT_DIR");
    for (compiler, src, lang) in [
        (&gpp, "verify_input.cpp", &["-std=c++20", "-fexceptions", "-fno-rtti"][..]),
        (&gcc, "cshims.c", &[][..]),
    ] {
        let mut c = std::process::Command::new(compiler);
        c.args(["-march=rv32im", "-mabi=ilp32", "-O3"]).args(lang)  // TIER 0: -O3
            // Only the classes that are UB or a silent miscompile, not a style sweep — a warning set
            // this code has never been held to would fail on noise and get switched off within a week.
            .args(["-Werror=return-type", "-Wreturn-type"])
            .arg("-I").arg(&shim).arg("-I").arg(&core)
            .arg("-I").arg(format!("{secp}/include"))
            .arg("-I").arg(&secp).arg("-I").arg(format!("{secp}/src"))
            // kept in step with the real build above, so the check compiles what actually ships
            .arg(format!("-DECMULT_WINDOW_SIZE={ecmult_window}"))
            .arg(format!("-DECMULT_GEN_KB={gen_kb}"))
            .args(["-DENABLE_MODULE_SCHNORRSIG=1", "-DENABLE_MODULE_EXTRAKEYS=1",
                   "-DUSE_EXTERNAL_DEFAULT_CALLBACKS=1"])
            .args(["-c", src, "-o", &format!("{out}/warncheck.o")]);
        let st = c.status().expect("run the guest warning check");
        assert!(st.success(), "guest warning check FAILED on {src} (see the diagnostic above). This \
            compile exists because the real build passes -w, which hides UB in our own sources — it \
            already hid a non-void function with no return on its success path. Fix the source; do \
            not remove this check.");
    }

    // 3) C++ runtime: libstdc++ + libgcc (unwinder, dormant) + newlib libc/nosys.
    //    Lib dirs derived from gcc so they track whatever toolchain version rzup installed.
    let stdcxx_dir = lib_dir(&gcc, &["-print-file-name=libstdc++.a"]);
    let libgcc_dir = lib_dir(&gcc, &["-print-libgcc-file-name"]);
    let libc_dir = lib_dir(&gcc, &["-print-file-name=libc.a"]);
    for d in [&stdcxx_dir, &libgcc_dir, &libc_dir] {
        println!("cargo:rustc-link-search=native={d}");
    }
    println!("cargo:rustc-link-lib=static=stdc++");
    println!("cargo:rustc-link-lib=static=gcc");
    println!("cargo:rustc-link-lib=static=c");
    println!("cargo:rustc-link-lib=static=nosys");
    println!("cargo:rustc-link-arg=--allow-multiple-definition");
}

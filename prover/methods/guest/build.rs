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

    // Reproducible builds: remap the absolute source root to a fixed virtual path so __FILE__ and
    // debug strings baked into the compiled Core/secp objects don't carry $HAZYNC_BASE / the build
    // machine's home dir — which would otherwise change the guest image id (METHOD_ID) per machine.
    let fpm = format!("-ffile-prefix-map={base}=/hazync");

    let bin = find_riscv_bin();
    let pfx = if bin.is_empty() { String::new() } else { format!("{bin}/") };
    let gcc = format!("{pfx}riscv32-unknown-elf-gcc");
    let gpp = format!("{pfx}riscv32-unknown-elf-g++");
    let ar = format!("{pfx}riscv32-unknown-elf-gcc-ar");

    // 1) REAL libsecp256k1 (C) + libc-glue shims.
    cc::Build::new()
        .compiler(&gcc).archiver(&ar)
        .flag("-march=rv32im").flag("-mabi=ilp32").opt_level(2).warnings(false)
        .flag(&fpm)
        .include(&secp).include(format!("{secp}/src"))
        .define("ECMULT_WINDOW_SIZE", "15").define("ECMULT_GEN_KB", "22")
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
    ];
    let mut b = cc::Build::new();
    b.cpp(true).compiler(&gpp).archiver(&ar)
        .flag("-march=rv32im").flag("-mabi=ilp32").flag("-std=c++20")
        .flag("-fexceptions").flag("-fno-rtti").opt_level(2).warnings(false)
        .flag(&fpm)
        .include(&core).include(&shim).include(format!("{secp}/include"));
    for tu in core_tus { b.file(format!("{core}/{tu}")); }
    b.file("verify_input.cpp");
    b.compile("bitcoinconsensus");

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

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

use std::{
    env,
    path::{Path, PathBuf},
};

use risc0_build_kernel::{KernelBuild, KernelType};

fn main() {
    if env::var("CARGO_FEATURE_CUDA").is_ok() {
        build_cuda_kernels();
    }

    build_cpu_kernels();
}

fn build_cpu_kernels() {
    rerun_if_changed("kernels/cxx");
    KernelBuild::new(KernelType::Cpp)
        .files(glob_paths("kernels/cxx/*.cpp"))
        .deps(glob_paths("kernels/cxx/*.h"))
        .deps(glob_paths("kernels/cxx/*.cpp.inc"))
        .deps(glob_paths("kernels/cxx/*.h.inc"))
        .include(env::var("DEP_RISC0_SYS_CXX_ROOT").unwrap())
        .compile("risc0_rv32im_cpu");
}

fn build_cuda_kernels() {
    let output = "risc0_rv32im_cuda";

    println!("cargo:rerun-if-env-changed=NVCC_APPEND_FLAGS");
    println!("cargo:rerun-if-env-changed=NVCC_PREPEND_FLAGS");
    println!("cargo:rerun-if-env-changed=SCCACHE_RECACHE");
    rerun_if_changed("kernels/cuda");

    env::set_var("SCCACHE_IDLE_TIMEOUT", "0");

    if env::var("RISC0_SKIP_BUILD_KERNELS").is_ok() {
        let out_dir = env::var("OUT_DIR").map(PathBuf::from).unwrap();
        let out_path = out_dir.join(format!("lib{output}-skip.a"));
        std::fs::OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&out_path)
            .unwrap();
        println!("cargo:{}={}", output, out_path.display());
        return;
    }

    let mut build = cc::Build::new();
    build
        .cuda(true)
        .cudart("static")
        .debug(false)
        .flag("-diag-suppress=177")
        .flag("-diag-suppress=550")
        .flag("-diag-suppress=2922")
        .flag("-std=c++17");

    // HAZYNC_631_MSVC_FLAGS — hazync#631. `-Xcompiler` forwards its argument VERBATIM to the host
    // compiler, and on Windows that host compiler is MSVC `cl.exe`, which does not speak
    // `-Wno-unused-function` or `-O3`. Everything else above is nvcc's own and portable.
    //
    // ⛔ THE NON-MSVC BRANCH IS BYTE-IDENTICAL TO WHAT SHIPPED. Linux and macOS CUDA builds get
    // exactly the flags, in exactly the order, they got before — this adds a branch, it does not
    // change the one that is in use. test_nvcc_flags.py asserts that, because a "portability fix"
    // that quietly re-flags the prover everyone actually runs would be the real damage.
    //
    // ⚠ UNVERIFIED ON WINDOWS. Neither CI nor the development box has a Windows machine with an
    // NVIDIA GPU, so nothing here has compiled a CUDA kernel under MSVC. The flags are the
    // documented equivalents (/wd4505 unreferenced local function, /wd4100 unreferenced formal
    // parameter, /O2 for -O3) and they are the first obstacle, not the only one — sppark comes
    // through DEP_SPPARK_ROOT below and its Windows support is also untested.
    //
    // ⚠ And MSVC may not have needed this: it usually downgrades an unknown option to warning
    // D9002 rather than erroring. If a Windows CUDA build ever succeeds, check whether reverting
    // this branch still works before assuming it was load-bearing.
    if env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        build
            .flag("-Xcompiler")
            .flag("/wd4505")
            .flag("-Xcompiler")
            .flag("/wd4100")
            .flag("-Xcompiler")
            .flag("/O2");
    } else {
        build
            .flag("-Xcompiler")
            .flag("-Wno-unused-function,-Wno-unused-parameter")
            .flag("-Xcompiler")
            .flag("-O3");
    }

    build
        .flag("-Xptxas")
        .flag("-O3")
        .include(env::var("DEP_RISC0_SYS_CUDA_ROOT").unwrap())
        .include(env::var("DEP_RISC0_SYS_CXX_ROOT").unwrap())
        .include(env::var("DEP_SPPARK_ROOT").unwrap());
    if env::var_os("NVCC_PREPEND_FLAGS").is_none() && env::var_os("NVCC_APPEND_FLAGS").is_none() {
        build.flag("-arch=native");
    }
    build.files(glob_paths("kernels/cuda/*.cu")).compile(output);
}

fn rerun_if_changed<P: AsRef<Path>>(path: P) {
    println!("cargo:rerun-if-changed={}", path.as_ref().display());
}

fn glob_paths(pattern: &str) -> Vec<PathBuf> {
    glob::glob(pattern).unwrap().map(|x| x.unwrap()).collect()
}

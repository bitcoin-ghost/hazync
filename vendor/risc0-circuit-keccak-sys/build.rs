// Copyright 2024 RISC Zero, Inc.
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
        .include(env::var("DEP_RISC0_SYS_CXX_ROOT").unwrap())
        .compile("risc0_keccak_cpu");
}

fn build_cuda_kernels() {
    let output = "risc0_keccak_cuda";

    println!("cargo:rerun-if-env-changed=NVCC_APPEND_FLAGS");
    println!("cargo:rerun-if-env-changed=NVCC_PREPEND_FLAGS");
    println!("cargo:rerun-if-env-changed=SCCACHE_RECACHE");
    rerun_if_changed("kernels/cuda");

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

    env::set_var("SCCACHE_IDLE_TIMEOUT", "0");

    let mut build = cc::Build::new();
    build
        .cuda(true)
        .cudart("static")
        .debug(false)
        .flag("-diag-suppress=177")
        .flag("-diag-suppress=550")
        .flag("-diag-suppress=2922")
        .flag("-std=c++17")
        .include(env::var("DEP_RISC0_SYS_CUDA_ROOT").unwrap())
        .include(env::var("DEP_SPPARK_ROOT").unwrap());

    // HAZYNC_631_MSVC_FLAGS — hazync#631. The SAME shape #632 fixed in risc0-circuit-rv32im-sys,
    // and that I then fixed in the SHARED risc0-build-kernel — but this crate has its OWN flag
    // block, so neither reached it:
    //
    //     cl : Command line error D8021 : invalid numeric argument '/Wno-unused-function'
    //
    // measured on windows-2022, run 37116535496. `-Xcompiler` forwards its argument verbatim to
    // the host compiler, and cl parses `/Wno-unused-function` as `/W` plus an invalid number.
    //
    // ⛔ THE NON-MSVC ARM IS BYTE-IDENTICAL: the same two calls, in the same order, with the same
    // comma-joined argument. Every proof on the board compiles through this path on Linux, so a
    // portability fix that re-flagged it would be the real damage.
    //
    // ⚠ ONLY THE FLAGS ARE BRANCHED. An earlier draft of this patch swallowed the `.include(...)`
    // calls, the -arch=native check and `.compile()` into the else arm, which would have left the
    // MSVC build with no include directories at all — caught by reading the patched file back
    // instead of trusting that the edit did what the diff looked like it did.
    if env::var("CARGO_CFG_TARGET_ENV").as_deref() == Ok("msvc") {
        // /wd4505 = unreferenced local function. MSVC has no unused-parameter warning worth
        // suppressing for this code, so nothing is invented for that half.
        build.flag("-Xcompiler").flag("/wd4505");
    } else {
        build
            .flag("-Xcompiler")
            .flag("-Wno-unused-function,-Wno-unused-parameter");
    }
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

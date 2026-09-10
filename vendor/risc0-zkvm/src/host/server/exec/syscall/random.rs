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

use anyhow::Result;
use risc0_zkvm_platform::WORD_SIZE;

use super::{Syscall, SyscallContext};

pub(crate) struct SysRandom;
impl Syscall for SysRandom {
    fn syscall(
        &mut self,
        _syscall: &str,
        _ctx: &mut dyn SyscallContext,
        to_guest: &mut [u32],
    ) -> Result<(u32, u32)> {
        tracing::debug!("SYS_RANDOM: {}", to_guest.len());
        let mut rand_buf = vec![0u8; to_guest.len() * WORD_SIZE];
        // hazync#119: the guest draws host entropy exactly ONCE, 4 words, from Rust's std runtime
        // inside the risc0 guest -- not from any hazync code. Those 16 bytes live in guest memory,
        // so the memory-image root differs at every segment boundary while the committed journal is
        // identical. Measured 2026-09-10: over 22 shared segment indices, po2/seal_words/verifier
        // params matched 22/22 and pre/post state matched 1/22 (the one being the fixed image id).
        //
        // That makes `rand_z` NOT the only entropy, which is why replaying a captured #119 with the
        // recorded rand_z never reproduced it. With this pinned, execution is bit-for-bit
        // reproducible and a captured fault becomes an exact test case.
        //
        // ⛔ DIAGNOSTIC ONLY. Never set this in production: it makes the guest's hash seed
        // predictable. It changes no guest code, so METHOD_ID is unaffected.
        match std::env::var("HAZYNC_FIXED_RANDOM") {
            Ok(v) if !v.is_empty() => {
                let seed: u64 = v.parse().unwrap_or(0x5eed_1119);
                let mut x = seed ^ 0x9E37_79B9_7F4A_7C15;
                for b in rand_buf.iter_mut() {
                    // xorshift64*, deterministic and dependency-free
                    x ^= x >> 12;
                    x ^= x << 25;
                    x ^= x >> 27;
                    *b = (x.wrapping_mul(0x2545_F491_4F6C_DD1D) >> 33) as u8;
                }
                tracing::debug!("SYS_RANDOM: PINNED to seed {seed} (hazync#119 diagnostic)");
            }
            _ => rand::fill(rand_buf.as_mut_slice()),
        }
        bytemuck::cast_slice_mut(to_guest).clone_from_slice(rand_buf.as_slice());
        Ok(((to_guest.len() * WORD_SIZE) as u32, 0))
    }
}

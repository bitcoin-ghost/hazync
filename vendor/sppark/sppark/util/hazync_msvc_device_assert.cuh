// HAZYNC_631_DEVICE_ASSERT — hazync#631. Keep sppark's device-side asserts working under MSVC.
//
// ⛔ THE PROBLEM. sppark asserts real invariants inside __global__ kernels:
//
//     assert(tid < WINDOW_SIZE);
//     assert((row_size & (row_size-1)) == 0);          // power of two
//     assert(lg_domain_size + lg_blowup <= MAX_LG_DOMAIN_SIZE && ...);
//
// On glibc, `assert` expands to `__assert_fail`, for which CUDA ships a __device__ version. On
// MSVC it expands to `_wassert`, which is __host__ only, so nvcc refuses:
//
//     parameters.cuh(177): error: calling a __host__ function("_wassert")
//                                 from a __global__ function("sppark::bn254::...")
//
// ⛔⛔ AND -DNDEBUG IS THE WRONG FIX, WHICH IS WHY IT IS NOT USED HERE. Nothing defines NDEBUG in
// the Linux build either, so the fleet genuinely evaluates these checks every run. Compiling them
// out on Windows alone would give the Windows prover weaker invariants than the machines producing
// the chain, and that divergence would be invisible in the binary.
//
// ⇒ THE CHECK IS PRESERVED; ONLY THE DIAGNOSTIC CHANGES. On MSVC device code the condition is still
// evaluated and a failure still aborts the kernel, via __trap(), which raises an unrecoverable
// error exactly as a failed device assert does. What is lost is the printed file/line message —
// a worse debugging experience, not a weaker check.
//
// ⚠ Scoped as tightly as it can be: MSVC only, device passes only (__CUDA_ARCH__), never clang-cl,
// and the host side keeps the real assert.
#pragma once

#if defined(_MSC_VER) && !defined(__clang__) && defined(__CUDA_ARCH__)
#include <cassert>
#undef assert
#define assert(expr) do { if (!(expr)) { __trap(); } } while (0)
#endif

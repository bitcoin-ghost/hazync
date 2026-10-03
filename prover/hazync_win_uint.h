// HAZYNC_631_UINT — give MSVC the POSIX `uint` that risc0-sys's CUDA kernels use.
//
// ⛔⛔ A TYPEDEF, NOT `-Duint=unsigned`, AND THE DIFFERENCE IS 200 ERRORS.
//
// risc0-sys's kernels declare `uint gid = blockIdx.x * blockDim.x + threadIdx.x;`. `uint` is a
// POSIX typedef that MSVC does not provide, so the first fix was `-Duint=unsigned` on the nvcc
// command line. It cleared those 13 errors and created 200+ new ones, because the CUDA and libcu++
// headers build the vector types `uint1`..`uint4` by TOKEN PASTING `uint` with a digit. A macro
// participates in that paste; the result was `unsigned1`..`unsigned4`, each appearing 96 times in
// run 37113822741, and every tuple trait over them failed to parse.
//
// A typedef is not a macro. It cannot be pasted, so `uint1` stays `uint1` and `uint` still names
// unsigned int.
//
// ⚠ MSVC only: every other target already has this from <sys/types.h>, and defining it twice is an
// error. The guard is the compiler, not the platform, because clang-cl targets Windows too.
#pragma once
#if defined(_MSC_VER) && !defined(__clang__)
typedef unsigned int uint;
#endif

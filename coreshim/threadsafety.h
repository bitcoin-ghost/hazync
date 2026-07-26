#ifndef BITCOIN_THREADSAFETY_H
#define BITCOIN_THREADSAFETY_H
// Hazync zkVM shim — NON-CONSENSUS platform glue only.
//
// The guest runs single-threaded on a freestanding riscv32 target (newlib, no
// pthreads), so Bitcoin Core's Clang thread-safety annotations have nothing to
// analyse. We expand them to no-ops — byte-for-byte what Core's own non-Clang
// build does (see the `#else` arm of the real src/threadsafety.h, lines 36-52) —
// and we OMIT Core's StdMutex / StdLockGuard helpers, which subclass std::mutex
// (unavailable without pthreads). This header is pulled in by chain.h so the real
// Core CBlockIndex struct and pow.cpp retarget math compile unchanged; no
// consensus logic lives here.
#define LOCKABLE
#define SCOPED_LOCKABLE
#define GUARDED_BY(x)
#define PT_GUARDED_BY(x)
#define ACQUIRED_AFTER(...)
#define ACQUIRED_BEFORE(...)
#define EXCLUSIVE_LOCK_FUNCTION(...)
#define SHARED_LOCK_FUNCTION(...)
#define EXCLUSIVE_TRYLOCK_FUNCTION(...)
#define SHARED_TRYLOCK_FUNCTION(...)
#define UNLOCK_FUNCTION(...)
#define LOCK_RETURNED(x)
#define LOCKS_EXCLUDED(...)
#define EXCLUSIVE_LOCKS_REQUIRED(...)
#define SHARED_LOCKS_REQUIRED(...)
#define NO_THREAD_SAFETY_ANALYSIS
#define ASSERT_EXCLUSIVE_LOCK(...)
#endif // BITCOIN_THREADSAFETY_H

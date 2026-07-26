#ifndef BITCOIN_SYNC_H
#define BITCOIN_SYNC_H
// Hazync zkVM shim — NON-CONSENSUS platform glue only.
//
// Bitcoin Core's real src/sync.h wraps std::mutex / std::recursive_mutex, which
// require pthreads — unavailable on the freestanding, single-threaded riscv32
// guest. The guest never contends a lock, so every mutex and lock guard collapses
// to a no-op. We provide exactly the surface that chain.h / chain.cpp reference
// when compiling the real CBlockIndex struct and the real pow.cpp retarget math:
// the Mutex/RecursiveMutex types (the cs_main object is declared by
// kernel/cs_main.h and defined once in verify_input.cpp, never locked), the LOCK
// family, and the AssertLockHeld/NotHeld assertions. No consensus logic here —
// the retarget computation itself is unmodified upstream pow.cpp.
#include <threadsafety.h>
struct Mutex {};
struct RecursiveMutex {};
struct GlobalMutex : Mutex {};
#define LOCK(cs) (void)0
#define LOCK2(cs1, cs2) (void)0
#define TRY_LOCK(cs, name) bool name = true
#define WAIT_LOCK(cs, name) (void)0
#define REVERSE_LOCK(g) (void)0
#define AssertLockHeld(cs) (void)0
#define AssertLockNotHeld(cs) (void)0
#endif // BITCOIN_SYNC_H

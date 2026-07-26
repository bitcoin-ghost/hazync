#ifndef BITCOIN_LOGGING_H
#define BITCOIN_LOGGING_H
// Hazync zkVM shim — NON-CONSENSUS platform glue only.
//
// The guest has no log sink (freestanding, no stdout/file). Bitcoin Core's logging subsystem
// (BCLog::Logger + a StdMutex-guarded singleton) is irrelevant to consensus, so we no-op the logging
// API. This lets headers that include <logging.h> — e.g. kernel/chainparams.cpp, from which we source
// the authoritative mainnet Consensus::Params — compile and link without dragging in logging.cpp.
// The variadic no-op still type-checks the format arguments, then discards them.
#include <cstdint>
namespace BCLog {
    enum LogFlags : uint32_t { NONE = 0, ALL = ~(uint32_t)0 };
    enum class Level { Trace = 0, Debug, Info, Warning, Error };
}
template <typename... Args> static inline void HazyncNoopLog(const Args&...) {}
static inline bool LogAcceptCategory(BCLog::LogFlags, BCLog::Level) { return false; }
#define LogPrintf(...)              HazyncNoopLog(__VA_ARGS__)
#define LogInfo(...)                HazyncNoopLog(__VA_ARGS__)
#define LogWarning(...)             HazyncNoopLog(__VA_ARGS__)
#define LogError(...)               HazyncNoopLog(__VA_ARGS__)
#define LogDebug(category, ...)     HazyncNoopLog(__VA_ARGS__)
#define LogTrace(category, ...)     HazyncNoopLog(__VA_ARGS__)
#define LogPrint(category, ...)     HazyncNoopLog(__VA_ARGS__)
#define LogPrintf_(...)             HazyncNoopLog(__VA_ARGS__)
#define LogPrintLevel(category, level, ...)   HazyncNoopLog(__VA_ARGS__)
#define LogPrintfCategory(category, ...)      HazyncNoopLog(__VA_ARGS__)
#endif // BITCOIN_LOGGING_H

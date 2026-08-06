/* Hazync proof verification — C ABI over the Rust verifier (hazync #31).
 *
 * Links one static library. No proving, no chain access, no peers, no allocation you must free.
 *
 * CONTRACT: a non-zero return means *out was NOT written and must not be read. Zero means every field
 * is valid AND the proof is genesis-anchored. There is deliberately no "verified but not anchored"
 * success case — a caller that forgot to check a separate flag would adopt a fabricated anchor, so the
 * anchoring check is not optional.
 */
#ifndef HAZYNC_VERIFY_H
#define HAZYNC_VERIFY_H

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

#define HAZYNC_OK                    0
#define HAZYNC_ERR_NULL             -1  /* null pointer or zero length */
#define HAZYNC_ERR_PARSE            -2  /* not a receipt */
#define HAZYNC_ERR_PROOF            -3  /* proof invalid for this guest: forged, tampered, or wrong build */
#define HAZYNC_ERR_JOURNAL          -4  /* journal is not a RangeState */
#define HAZYNC_ERR_NOT_ANCHORED     -5  /* valid proof, but NOT genesis-anchored — do not adopt */
#define HAZYNC_ERR_SELF_ID          -6  /* journal self_id != guest image id (S1) */
#define HAZYNC_ERR_KIND             -7  /* wrong domain tag (H8) */
#define HAZYNC_ERR_TOO_MANY_ROOTS   -8  /* more accumulator roots than the struct can carry */
/* hazync_check_utxo_dump */
#define HAZYNC_ERR_DUMP_MAGIC       -9  /* not an HZUTXO dump */
#define HAZYNC_ERR_DUMP_VERSION    -10  /* unsupported dump version */
#define HAZYNC_ERR_DUMP_HEIGHT     -11  /* dump height != the proof's height */
#define HAZYNC_ERR_DUMP_COUNT      -12  /* coin count != the proof's utxo_leaves */
#define HAZYNC_ERR_DUMP_TRUNC      -13  /* truncated, or trailing bytes */
#define HAZYNC_ERR_DUMP_POS        -14  /* positions are not a permutation of 0..n-1 */
#define HAZYNC_ERR_DUMP_ROOTS      -15  /* rebuilt accumulator roots != the proof's roots */

/* popcount(leaves) roots, so 32 covers mainnet scale with room to spare. */
#define HAZYNC_MAX_ROOTS 32

typedef struct {
    uint32_t height;
    uint8_t  tip_hash[32];          /* DISPLAY order — compares directly with getblockhash */
    uint64_t cumulative_work_lo;    /* work is 128-bit; C has no portable u128 */
    uint64_t cumulative_work_hi;
    uint64_t utxo_leaves;
    uint32_t next_bits;
    uint32_t epoch_start_time;
    uint32_t prev_time;
    uint32_t root_count;
    uint8_t  utxo_roots[HAZYNC_MAX_ROOTS][32];
} HazyncState;

/* Verify a genesis-anchored range proof; on HAZYNC_OK, *out holds the state a node may adopt. */
int hazync_verify_proof(const uint8_t* proof, size_t len, HazyncState* out);

/* Check a bridge UTXO dump against the accumulator roots a verified proof commits to — the step
 * that makes assumeutxo PROVEN rather than trusted. `proven` must come from a SUCCESSFUL
 * hazync_verify_proof; passing an unverified struct checks the dump against nothing.
 *
 * MEMORY: rebuilds the forest in RAM — order 15-20 GB at a real mainnet height (~140M coins). Not a
 * soundness concern (the coin count comes from the verified proof, not the untrusted file), but size
 * the machine for it. Core's loadtxoutset is lighter only because it streams into LevelDB. */
int hazync_check_utxo_dump(const uint8_t* dump, size_t len, const HazyncState* proven);

/* Guest image id this library trusts, NUL-terminated hex. */
const char* hazync_method_id(void);

#ifdef __cplusplus
}
#endif
#endif /* HAZYNC_VERIFY_H */

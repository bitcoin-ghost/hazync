# The four-lever stack — plan of record

**Status: IN FORCE from 2026-08-28.** This document exists because the implementation of one of
these levers was lost to a WSL2 VM death while sitting uncommitted in `/tmp`. Everything needed to
rebuild each lever is written down here. **Commit early on every branch; do not hold work in a
worktree under `/tmp`.**

## 1. What is MEASURED, and what the card count actually is

⛔ **THE CARD COUNT IS UNKNOWN.** Not "6 with 17% margin", not "5 with 25%". Unknown.

Every row of the stack table this document used to lead with was a PROJECTION. The one row that has
since been measured moved by 40%: bigint2 on block 962,000 was projected at 7.53x and **measures
4.48x** (execute mode). The projection was built from measured primitives and still under-weighted
the non-ECDSA residual. → `TIP_BLOCK_BIGINT2_2026-08-28.md` · [[feedback_only_measured_numbers]]

⇒ **No fleet size is stated in this document until a proving measurement exists.**

### MEASURED — block 962,000, 8,006 inputs, execute mode, 2026-08-29

| | result |
|---|---|
| control cost | 14.05 G cycles/partition (cross-checks a prior session's 14.06 G to 0.07%) |
| **Tier 0** | **−2.10%** (predicted 2.206%, fitted on a different block) |
| **bigint2 middle path** | **4.48x** |
| **witness encoder** | **7,256,592 → 3,594,208 wire bytes = 2.019x** |
| digest gate, Tier 0 arm | **32/32 byte-identical to control**, `all_valid=1` |
| digest gate, bigint2 arm | **32/32 byte-identical to control**, `all_valid=1` |
| join-tree ordering | **identical across 2,052 leaf counts** (in-crate test, 14 passed) |

### MEASURED earlier — block 140,000 (212 inputs, ~100% ECDSA)

| | result |
|---|---|
| bigint2 GPU proving wall | **8.00x** |
| per-verify ECDSA | 1,723,407 → 140,044 cycles |

⚠ Block 140,000 is the most ECDSA-dense block in the set. Its ratios are a ceiling. **Name the block
beside any figure taken from it.**

### ⛔ NOT MEASURED — and therefore not stated anywhere in this document

- **The proving ratio on a tip block.** Execute cycles are not proving cost; bigint2 is a separate
  coprocessor circuit. **The fleet size depends on this and on nothing else currently available.**
- **The witness encoder's cycle effect.** Bytes shrank 2.019x; what that does to cycles is unrun.
- **The join tree's speedup.** Its ordering is proven; its ~1.4x-at-32-cards claim is not.
- **The aggregate above N=2.** Needs a third box.
- ⇒ **The card count.** It is a function of all four.

### ⏰ The three runs that would settle it

1. **One GPU prove of a block 962,000 chunk, `HAZYNC_BIGINT2_ECDSA=1` vs stock.** Gives the proving
   ratio on a real block. Highest value of anything on this list.
2. **An aggregate execute (mode 5) before/after the encoder**, for its cycle effect. Needs fresh
   chunk receipts because the encoder moves `METHOD_ID`.
3. **An aggregate on a THIRD box**, for the join tree and for scaling above N=2.

## 2. The four levers

| # | branch | lever | worth | side | moves `METHOD_ID`? |
|---|---|---|---|---|---|
| 1 | `feat/bigint2-middle` | #139 middle path | 32 -> 8 cards | guest | **yes** |
| 2 | `feat/aggregate-witness-read` | aggregate witness deserialisation | see 2.2 | guest + host | **yes** |
| 3 | `feat/join-tree-pipelining` | remove the level barrier | ~1.4x at 32 cards; 7-8% margin at 6 | **host** | **no** |
| 4 | `feat/tier0-codegen` | guest codegen flags | ~2% | guest build | **yes** |

Three of four move `METHOD_ID`, so they ship as **one re-baseline batch**. Lever 3 is host-side and
can merge to `main` alone, at any time, with no board reset.

### 2.1 `feat/bigint2-middle` — DONE, pushed

Routes only the ECDSA group arithmetic through the bigint2 accelerator: one line,
`ecdsa_impl.h:212`, the `secp256k1_ecmult` computing `u1*G + u2*Q`. libsecp keeps DER parsing, low-S
handling, r/s checks, inversion and the final comparison.

- **Measured 8.00x at proving time** (wholesale is 9.10x; the extra 14% costs the whole equivalence
  surface). Execute-mode cycles were 9.19x — do not quote the two interchangeably.
- Journal digest identical, `all_valid=1`, `binds=212`, all 212 signatures agree with libsecp.
- Reproducing mutates `$HAZYNC_BASE/secp256k1/src/ecdsa_impl.h`; clean md5
  `308fc36774999286dcc77bf7c7df87b9`. Back it up.

**Open:** no BIP340 in risc0-crypto, so tip-era taproot blocks see less than block 140,000's ~100%
ECDSA figure — this is a ceiling, not a tip number. Exhaustive differential testing over the
historical signature set is **not started and load-bearing**.

### 2.2 `feat/aggregate-witness-read` — MEASURE FIRST, do not write an encoder yet

`write_aggregate_env` does `b.write(w)`; `write_chunk_inputs` already does `write_slice(&padded(..))`
from #136. The aggregate never got that fix, and 78% of block validation is deserialising the witness
at 347 cycles/byte.

⛔ **The 2.05x figure in the stack table is wrong and was caught before any code was written.**
`PackedBytes` already uses risc0's packed byte path, so `txs`/`tx_prevouts` are not 4x-bloated.
Per `TEN_MINUTE_BLOCK.md` §7.8 the `inputs` vector is **~65% of the witness**; transaction bytes are
**21%**. The real target is `BlockInput`'s two `WireProof`s — `[u8;32]` leaf plus siblings, each
costing 4x in the word stream:

```
per input:  564 B -> 180 B   (3.13x)
witness:    7.26 MB -> 4.02 MB  (1.81x)
validate:   3.22G -> 2.10G cycles  (1.54x, not 2.05x)
```

⚠ **That 3.13x is arithmetic over a residual, not a measurement**, and §7.8's own advice applies:
instrument `to_vec` per sub-structure *before* writing an encoder. **First commit on this branch is
instrumentation.** At 1.54x, six cards runs ~9.6 min rather than 9.1 — so the stack needs levers 3
and 4 to hold the margin.

### 2.3 `feat/join-tree-pipelining` — host-side, ships alone

The join driver is **level-synchronous**: every level waits for all of its joins before the next
starts. `prover/host/src/main.rs:4500-4528`, and a second site in `seg_serve_cmd` near line 5115:

```rust
while level_recs.len() > 1 {
    let npairs = level_recs.len() / 2;
    let odd = level_recs.len() % 2 == 1;
    loop {
        let have = (0..npairs).filter(|p| dir.join(format!("jout_{level}_{p:04}.bin")).exists()).count();
        if have == npairs { break; }      // <- the barrier
    }
    ...
}
```

116 segments is 7 levels `[58, 29, 14, 7, 4, 2, 1]`, giving join-tree efficiency:

| cards | 2 | 8 | 16 | 32 |
|---|---|---|---|---|
| efficiency | 93% | 68% | 54% | **40%** |

⛔ **This is why the two-card aggregate test looked healthy — N=2 is the one regime where the barrier
is invisible.**

**The fix:** replace the barrier with an incremental fold — a join publishes the moment both of its
children exist. Precompute the tree shape from the leaf count so ordering is *provably identical*:
position `p` pairs with `p^1`, and the even position is always the left operand (joins chain claims,
so they do not commute). **Only the schedule changes.**

The narrow tail is structural and stays — but it is **free under bounded lag**, since block *h*'s tail
overlaps block *h+1*'s wide segment phase.

### 2.4 `feat/tier0-codegen` — flags only

`-O3`, rust LTO, CGU=1, `ECMULT_WINDOW_SIZE=21`. Measured in `TIER0_RESULTS_2026-08-26.md`; window 21
alone is **-1.245%** at 212 inputs. Whole batch is ~2%.

Two guards, both from real failures:

- **Record and compare `METHOD_ID` on every arm.** A rebuild that silently does not happen returns
  identical cycles and reads as "this change had no effect" — a false negative indistinguishable from
  a real result.
- **Back up and restore `$HAZYNC_BASE`.** Changing the window regenerates a 38 MB
  `precomputed_ecmult.c` in the shared source tree, which changes `METHOD_ID` even at window 19.

## 3. Prerequisite that is not on the list

**#190, the type-aware packer, must land** — without it the post-#139 straggler goes to 2.45x and
roughly halves the win. It is open with checks pending. Treat it as a gate on the whole stack.

## 4. Integration and benchmarking

`feat/stack-integration` carries all four merged, for one build and one benchmark run. Sequence:

1. Land lever 3 (`join-tree-pipelining`) to `main` on its own — it needs nothing from the others.
2. Land #190.
3. Merge levers 1, 2, 4 into `feat/stack-integration` as **one re-baseline**; record the new
   `METHOD_ID` once.
4. Benchmark on block 962,000 at 6 cards. Target: <= 8.3 min, >= 17% margin.

⚠ Chunk receipts must come from the **current** guest — `agg_chunks()` verifies every receipt against
`METHOD_ID` before the execute-mode branch, so a re-baseline invalidates every stored receipt.

## 4.1 STATUS as of 2026-08-28 22:25

All four levers are implemented, committed, pushed, and merged into `feat/stack-integration`
with **no conflicts**. The merged tree type-checks: `REAL_EXIT=0`, zero errors, warning count
unchanged at 8 (all pre-existing and elsewhere in the file).

| branch | commit | state |
|---|---|---|
| `feat/bigint2-middle` | `bae4394` | measured 8.00x at proving time |
| `feat/join-tree-pipelining-v2` | `27163ba` | compiles; **unbenchmarked** — needs a 3rd box |
| `feat/tier0-codegen` | `3d8f1fd` | compiles; window-21 table regenerated (see below) |
| `feat/aggregate-witness-read-v2` | `15a1190` | profiled, encoder written, compiles |
| `feat/stack-integration` | `077b2ee` | all four merged |

✅ **Tier 0 is provably active, not silently ignored.** The build regenerated
`$HAZYNC_BASE/secp256k1/src/precomputed_ecmult.c` from `#if ECMULT_WINDOW_SIZE > 19` to
`> 21`, and the file grew 38.5 MB -> 154 MB. That is the observable side effect of the window
actually changing, which is the guard `TIER0_RESULTS` insists on.

⚠ **The base tree was NOT pristine before this.** It was already regenerated at window 19 by an
earlier experiment. `build.rs` only asserts pristineness on the `<=15` path, so nothing would have
warned. `ecdsa_impl.h` was verified clean at `308fc36774999286dcc77bf7c7df87b9` first, and both
mutable files were backed up to `~/hazync-base-backup-2026-08-28/` before building.

### ✅ RESULT: Tier 0 passes its digest gate (2026-08-28, CPU-only)

Block 962,000, 8,006 inputs, `HAZYNC_CHUNKS=16`, `HAZYNC_PROFILE_EXEC=1`, both partitions
(`count-packed (old)` and `cost-packed (new)`) = 32 chunk executes per arm.

| | control (`main`) | stack (`feat/stack-integration`) |
|---|---|---|
| `METHOD_ID` | `916cde9ed4ff3d0bb469b20a33a0a5e2a52e4161a118acd001b284764a288895` | `70fc6484be0a5e2538dd64fae5b0dfcfc82c4a4ae46ea999601d939e053f084d` |
| journal digests | 32 | 32 |
| `all_valid=1` | 32/32 | 32/32 |

✅ **ALL 32 JOURNAL DIGESTS BYTE-IDENTICAL.** ✅ **The two `METHOD_ID`s DIFFER**, which is the half
of the guard that proves both arms genuinely rebuilt rather than one answering from a stale binary --
without it, "identical" is indistinguishable from "the change never compiled".

⇒ **`-O3` + fat LTO + CGU=1 + ECMULT window 21 compute exactly what `main` computes.** This is a
NEW result: `TIER0_RESULTS_2026-08-26` validated its combined arm at window **20**; window 21 had
never had its digest checked, and the guest builds with `-w`, so `-O3` is precisely where latent UB
would have surfaced. None did.

⚠ **This arm gated Tier 0 ALONE.** bigint2 was not built (see the trap below), and neither the
witness encoder nor the join tree is reachable from mode 4 -- see "The gate this has NOT passed".

### ✅ RESULT: the join tree's schedule is provably order-identical

Both drivers were rebuilt over symbolic nodes -- the old one transcribed literally, `pop()` and all --
and their expression trees compared for **2,052 leaf counts**: every value 1..=2048, plus 116, 501,
1,684 and 8,006. **Identical in every case**: same pairs, same operand order, same carries.

That is the property that would be dangerous if wrong. Joins chain claims (`join` asserts
`a.post == b.pre`), so they do not commute -- a carry-indexing error would yield receipts that fail
to verify, not merely a slower tree.

⚠ Tests a faithful transcription of both algorithms, not the shipped function. **TODO: extract the
width computation into a real function and point the test at it**, so it exercises shipped code.
⚠ Says nothing about the PERFORMANCE claim (~1.4x at 32 cards); that still needs a third box.

### ⛔⛔ MERGING THE BRANCH DOES NOT ENABLE bigint2

**Discovered 2026-08-28 by checking, not by reading.** After a full build of
`feat/stack-integration`, `$HAZYNC_BASE/secp256k1/src/ecdsa_impl.h` was still at the CLEAN md5
`308fc36774999286dcc77bf7c7df87b9`. The stack had built without its largest lever and nothing said so.

Two separate things must BOTH happen, and merging the branch does neither:

1. **Patch 0005 must be applied to `$HAZYNC_BASE`.** The branch ships
   `patches/0005-ecdsa-verify-group-arith-via-bigint2.patch`, but the patch is what ADDS the
   `#ifdef` block to `ecdsa_impl.h`. Un-applied, the block does not exist.
2. **`HAZYNC_BIGINT2_ECDSA=1` must be set.** `guest/build.rs:128` reads it and defines the macro
   that the `#ifdef` from step 1 tests. It defaults to OFF.

⇒ **Setting the env var alone does nothing** (no `#ifdef` to enable), and **applying the patch alone
does nothing** (macro undefined). Either one on its own builds stock libsecp and looks like a
successful build of the stack.

⚠ **Anyone benchmarking `feat/stack-integration` without both steps will measure a stack missing the
32 -> 8 card lever, and conclude the stack does not work.** Verify with the md5 above: if
`ecdsa_impl.h` is still `308fc367...`, bigint2 is NOT in the build.

This is deliberate design -- the fidelity decision is meant to be opt-in -- but it is a silent
default, and a silent default on the biggest lever is a trap.

### ⛔ THE MIRROR TRAP: turning bigint2 OFF again breaks the build

Found 2026-08-28 by running `cargo test --bin host` after the bigint2 arm, without the flag:

```
rust-lld: error: undefined symbol: hazync_ecmult_verify
    >>> referenced by secp256k1.c
    >>>               …-secp256k1.o:(secp256k1_ecdsa_verify) in archive libsecp256k1.a
```

**A build with `HAZYNC_BIGINT2_ECDSA=1` leaves a `libsecp256k1.a` whose `secp256k1_ecdsa_verify`
references `hazync_ecmult_verify`.** The next build WITHOUT the flag drops the Rust side that
exports that symbol, but reuses the cached C archive -- so the link fails on a symbol nothing
should be asking for.

⇒ The pair of traps is symmetric and both are silent in their own way:
- **On:** merging the branch does NOT enable bigint2 — it builds stock libsecp and says nothing.
- **Off:** having once enabled it, turning it off does NOT cleanly disable it — it fails to LINK,
  which at least is loud, but the error names a symbol rather than the cause.

**Recovery (verified):** restore `ecdsa_impl.h` from a clean copy. That changes the C input, so
`cc` rebuilds the archive without the `#ifdef` block and the symbol reference disappears. After
restoring, `cargo test --bin host` returned **14 passed, 0 failed**.

⇒ **Toggling this flag requires the C archive to be rebuilt, not just the Rust side.** Anyone
sweeping bigint2 on/off must restore `$HAZYNC_BASE/secp256k1/src/ecdsa_impl.h` between arms —
clean md5 `308fc36774999286dcc77bf7c7df87b9` — or clear
`prover/target/riscv-guest/`. A sweep that does neither measures a stale archive or fails to link.

### ⛔ The gate this has NOT passed

**An identical journal digest against control.** The wire format changed; the computation must not
have. This is CPU-only -- no GPU -- and it is the same gate Tier 0 passed with `607f4a7e...`:

```
# control (main), then the stack -- digests must be byte-identical
HAZYNC_BLOCK=block_962000.json host chunk-profile
```

Until that passes, the stack is code that compiles, not code that is known correct. Nothing here
should be quoted as a result before it does.

## 5. Standing caveats — do not quote the stack without these

- **Aggregate scaling past 2 cards is UNMEASURED.** 1.81x at N=2 (88-91% efficient) is real; the N=4
  arm that appeared to show saturation ran two worker processes on the same two cards and therefore
  added no compute. Testing above N=2 needs a **third box**, not a fourth worker.
- **The witness read's factor is derived, not run** (see 2.2).
- **Join-tree efficiency calibrates `t_join ~ t_seg` from a single point.**
- **Resolution is not a floor** — 0.28 s per resolve measured, so ~4.5 s at 16 chunks, not the 196 s
  some docs still carry. But it is a serial chain, linear in chunk count: 128 chunks is ~36 s.

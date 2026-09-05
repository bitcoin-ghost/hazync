# History — the development record

⛔ **Nothing in this directory is current.** These documents were true when written. Many have been
superseded, several were retracted by later measurement, and **numbers here should not be quoted.**

They are kept because *how a conclusion was reached* is often the only defence against reaching the
wrong one again — several levers in this project were proposed twice, and the second proposal was
refused on evidence recorded here.

For what is true now, see [`../README.md`](../README.md).

## ⛔ Known-stale figures in this directory

| figure | appears in | superseded by |
|---|---|---|
| card counts of 16 / 28 / 29 / 32 / 48 / 50 | `TEN_MINUTE_BLOCK.md`, `MODELS.md`, `STACK_INTEGRATION_PLAN.md`, `PERF_INVESTIGATION_2026-08-26.md` | `../CORE_VS_GHOST.md` |
| `7.53x` projected for bigint2 on the tip block | `TIP_BLOCK_BIGINT2_2026-08-28.md` | measured **4.48x** in that same document |
| `9.83%` / `1.416 G` for `fe_sqrt` | `MSM_BATCH_VERIFY.md`, and formerly `../LIFTX_HINT.md` | **6.17%** with the field backend — the chain's field ops are ~14x cheaper |
| the Core/Ghost gap as "~5x the hardware" | `MODELS.md` | **~2.6x** — that framing predates the coprocessor field backend |

## What is here

| file | what it recorded | why it moved |
|---|---|---|
| `ACCELERATION.md` | the long acceleration record | superseded by `../CORE_VS_GHOST.md` |
| `TEN_MINUTE_BLOCK.md` | the ten-minute target and fleet arithmetic | its denominators predate every 2026-08-3x measurement |
| `MODELS.md` | the original Core/Ghost framing | superseded by `../CORE_VS_GHOST.md` |
| `STACK_INTEGRATION_PLAN.md` | the four-lever stack plan | executed; results in `../CORE_VS_GHOST.md` |
| `BIGINT2_MIDDLE_PATH.md` | #139 middle vs wholesale | decided: middle path |
| `MSM_BATCH_VERIFY.md` | Pippenger batch verification | **rejected on measurement** — 4.3x at chunk scale, worth one card |
| `PERF_INVESTIGATION_2026-08-26.md` | the perf investigation | dated |
| `TIER0_RESULTS_2026-08-26.md` | Tier 0 codegen results | dated; the wins shipped |
| `WITNESS_WIRE_PROFILE_2026-08-28.md` | witness deserialisation profile | dated; `read_slice` shipped |
| `TIP_BLOCK_BIGINT2_2026-08-28.md` | bigint2 on the tip block | dated; **contains its own retraction** (7.53x → 4.48x) |
| `SEGDIST_STEP2.md`, `SEGDIST_TASKS.md` | segment distribution steps | folded into `../SEGMENT_DISTRIBUTION.md` |

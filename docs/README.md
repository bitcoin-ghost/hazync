# Hazync documentation

Bitcoin Core's own consensus code, executed inside a RISC0 zkVM, so that a block's validity can be
**proven once and verified by anyone** without re-executing it.

⏰ **Current as of 2026-09-01.** Anything not listed here is in [`history/`](history/README.md),
which is the development record and **must not be quoted for numbers**.

## Start here

| | |
|---|---|
| [`EXPLAINER.md`](EXPLAINER.md) | what this is, in plain terms |
| [`GOALS.md`](GOALS.md) | what it is for, and what it is not |
| [`HAZYNC_ARCHITECTURE.md`](HAZYNC_ARCHITECTURE.md) | how the pieces fit together |
| [`SPEC.md`](SPEC.md) | the specification |

## Trust and correctness

| | |
|---|---|
| [`SOUNDNESS.md`](SOUNDNESS.md) | what a proof does and does not establish |
| [`AUDIT_2026-07.md`](AUDIT_2026-07.md) | audit record |
| [`EXTERNAL_REVIEW.md`](EXTERNAL_REVIEW.md) | external review record |
| [`FUZZING.md`](FUZZING.md) | fuzzing posture: independent oracle, positive control, honest scope |

## Running it

| | |
|---|---|
| [`PROVING.md`](PROVING.md) | proving a block |
| [`RUN_YOUR_OWN_COORDINATOR.md`](RUN_YOUR_OWN_COORDINATOR.md) | operating a coordinator |
| [`TOPOLOGY_AND_SETTINGS.md`](TOPOLOGY_AND_SETTINGS.md) | topology and tuning |
| [`GPU_EXPERIMENT_RUNBOOK.md`](GPU_EXPERIMENT_RUNBOOK.md) | reproducing a measurement on GPUs |
| [`SEGMENT_DISTRIBUTION.md`](SEGMENT_DISTRIBUTION.md) | distributing segments across workers |

## Performance — the two operating modes

⏰ **[`CORE_VS_GHOST.md`](CORE_VS_GHOST.md) is the authority on which mode costs what.** Everything
else in this section is supporting detail.

| | |
|---|---|
| [`CORE_VS_GHOST.md`](CORE_VS_GHOST.md) | the two modes, what each concedes, and the measured cost of each |
| [`FIELD_BIGINT2_BACKEND.md`](FIELD_BIGINT2_BACKEND.md) | the coprocessor field backend for libsecp, and the levers rejected around it |
| [`LIFTX_HINT.md`](LIFTX_HINT.md) | recovering a pubkey's Y from a verified hint |
| [`FLEET_SIZING.md`](FLEET_SIZING.md) | ⚠ **card counts here predate 2026-09-01 and are being revised** |

## Plans

| | |
|---|---|
| [`ROADMAP.md`](ROADMAP.md) | where this is going |
| [`RELEASE_PLAN.md`](RELEASE_PLAN.md) | how a release is cut |

## ⛔ How to read a number in this repository

Every performance figure should say **what was measured, on what, and with what still unmeasured.**
This project has repeatedly been wrong by believing a projection, and the corrections are recorded
rather than quietly edited out:

- a bigint2 projection of **7.53x** measured **4.48x**
- a field-backend projection of **3.67x** first measured **2.381x** — the shortfall was three
  redundant memory copies in an FFI wrapper, worth 38% of the block, not the design
- `fe_sqrt` sized at **9.83%** from a flat profile was **6.17%** cumulative once the field ops
  beneath it were accelerated
- a straggler reported as a perfect **1.00x** was **1.563x** on real cycles: it was computed on
  *predicted* cost, and the packer balances its own predictor by construction

**If a figure does not say how it was obtained, treat it as a projection.**

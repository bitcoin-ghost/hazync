# History — the development record

⛔ **Nothing in this directory is current.** These documents were true when written. Many have been
superseded, several were retracted by later measurement, and **numbers here should not be quoted** without
the correction next to them. Each file carries a banner naming what superseded it.

They are kept because *how a conclusion was reached* is often the only defence against reaching the
wrong one again — several levers in this project were proposed twice, and the second proposal was
refused on evidence recorded here.

For what is true now, see [`../README.md`](../README.md); for releases, [`../../CHANGELOG.md`](../../CHANGELOG.md).

## ⛔ Known-stale figures

| figure | appears in | superseded by |
|---|---|---|
| the 600 s block as an open target | `TEN_MINUTE_BLOCK.md`, `FLEET_SIZING.md`, `STACK_INTEGRATION_PLAN.md` | **met**: block 966,256 in 544.0 s on 27 RTX 4090s (`MILESTONE_966256_RUN4_2026-09-10.md`) |
| card counts of 16 / 28 / 29 / 32 / 48, and FLEET_SIZING's self-retracted ~50 | `TEN_MINUTE_BLOCK.md`, `FLEET_SIZING.md`, `MODELS.md`, `STACK_INTEGRATION_PLAN.md`, `PERF_INVESTIGATION_2026-08-26.md` | measured CORE fleet: ~13 L40S for a sub-ten-minute block, from a formula that reproduces the measured 8-card block (`BENCH_8xL40S_2026-09-08.md`) |
| "~7 cards" after #139 | `TEN_MINUTE_BLOCK.md` (annotated), `HELIX_DUAL_BACKEND.md` | measured 2026-09-02 on two L40S: CORE 10, GHOST 5 (`../BUILDS.md` §1); the 8-card CORE fleet then put CORE at ~13 |
| the Core/Ghost gap as "~5x the hardware" | `CORE_VS_GHOST.md` | measured 10 vs 5 cards (`../BUILDS.md` §1); the memo's own §1 already projected ~1.8x |
| `7.53x` projected for bigint2 on the tip block | `TIP_BLOCK_BIGINT2_2026-08-28.md` | measured **4.48x** execute (Tier 0 + bigint2 against control; bigint2 alone 4.384x) in that document, and **4.112x** proved (`GHOST_GAINS.md` §0, arm S) |
| `7.18x` "MEASURED" for #139 wholesale | `ACCELERATION.md`, `TEN_MINUTE_BLOCK.md` (both annotated) | a block bound derived from the n=256 `ec-bench` microbenchmark, never a block measurement; see the 4.48x row |
| `5.25x` / `6.95x` for #139 middle path / wholesale + packer | `ACCELERATION.md`, `MODELS.md`, `TEN_MINUTE_BLOCK.md`, `GPU_EXPERIMENT_RUNBOOK.md` | execute-derived projections; see the 4.48x row. The wholesale arm was never run (`9b767b5`) |
| worker processes "1.20x" per card | `PERF_INVESTIGATION_2026-08-26.md`, `GPU_EXPERIMENT_RUNBOOK.md`, `ACCELERATION.md` (annotated) | ceiling **≤1.09x** once the card was measured 91.5% busy (`TEN_MINUTE_BLOCK.md` §3) |
| the ~18-minute floor "at infinite cards" | `SEGMENT_DISTRIBUTION.md` | refuted in the same file: the aggregate distributes, 2.78x on three L40S (#153) |
| `9.83%` / `1.416 G` for `fe_sqrt` | `../LIFTX_HINT.md` §1 (a flat profile) | **6.17%** cumulative (241,023,621 cycles) with the field backend — `patches/0013` header |

## What is here

| file | what it recorded | status |
|---|---|---|
| `ACCELERATION.md` | the long acceleration record | superseded by `../BUILDS.md`, `../FIELD_BIGINT2_BACKEND.md`, `TIP_BLOCK_BIGINT2_2026-08-28.md` |
| `AUDIT_2026-07.md` | the round-8 soundness audit, 2026-07-22 | superseded by `../SOUNDNESS.md` and `../../SECURITY.md`; G1 replaced by the coinbase SMT (#54) |
| `BENCH_8xL40S_2026-09-08.md` | an 8×L40S CORE fleet: chunk count, stragglers, sizing | measured; the runbook it informs is `../FLEET_OPERATIONS.md` |
| `BIGINT2_MIDDLE_PATH.md` | #139 middle path vs wholesale, with its trial harness (formerly `EXPERIMENT_139_BIGINT2.md`) | Ghost channel only; never shipped in CORE |
| `CORE_VS_GHOST.md` | the Core-or-Ghost decision memo, 2026-08-30 | decided: CORE ships, since v0.21.0 (`../BUILDS.md`) |
| `FLEET_SIZING.md` | the first fleet-size estimate, and coordinator egress | superseded by `BENCH_8xL40S_2026-09-08.md` and the milestone record |
| `GHOST_GAINS.md` | every remaining Ghost gain, priced | G1, G3, G6 built; G4, G5 not; next build in `../BUILDS.md` §3.1 |
| `GPU_EXPERIMENT_RUNBOOK.md` | the experiments that needed a card | run, closed or overtaken; #182 closed unmerged |
| `HAZYNC_ARCHITECTURE.md` | the original design and integration plan | superseded by `../SPEC.md`, `../SOUNDNESS.md`, `../PROVING.md` |
| `HELIX_DUAL_BACKEND.md` | one guest, both backends, height-gated | verdict: probably not needed |
| `MILESTONE_966256_RUN4_2026-09-10.md` | block 966,256 on rented RTX 4090 fleets, runs 1–4 (runs 1 and 2 merged in) | the 600 s target met: 544.0 s on 27 cards |
| `MODELS.md` | the original Core/Ghost framing | superseded by `../BUILDS.md` |
| `MSM_BATCH_VERIFY.md` | Pippenger batch verification | declined by decision; nothing in it was measured on hardware |
| `OVERNIGHT_2026-08-03.md` | an overnight log: audit #3, #54, #83 (formerly `tasks/`) | dated record |
| `PERF_INVESTIGATION_2026-08-26.md` | the perf investigation | Tier 0 shipped; worker-process ceiling ≤1.09x |
| `RELEASE_PLAN.md` | the early-August finishing plan — not a release procedure | obsolete; the v0.21.0 cutover happened 2026-09-07 |
| `ROADMAP.md` | the task inventory and completed-work log | superseded; `../../CHANGELOG.md` and the issue tracker |
| `SECURITY_AUDIT_LOG.md` | every security review round, 1–9 self and 10–11 external, plus the 2026-07 self- and coverage audits (moved out of `SECURITY.md`) | dated record; `../../SECURITY.md` keeps status, open items and the finding index |
| `SEGDIST_TASKS.md` | segment-distribution tasks, with step 2 (formerly `SEGDIST_STEP2.md`) | complete; the last segment moved to workers in #158 |
| `SEGMENT_DISTRIBUTION.md` | segment-distribution design and its two- and three-card measurements | superseded by `../FLEET_OPERATIONS.md` |
| `STACK_INTEGRATION_PLAN.md` | the four-lever stack plan | three levers shipped via #208; the #139 middle path is Ghost-only |
| `TEN_MINUTE_BLOCK.md` | the ten-minute target and fleet arithmetic | target met; `../TOPOLOGY_AND_SETTINGS.md`, `../BUILDS.md` |
| `TOPOLOGY_AND_SETTINGS_2026-09-05.md` | the topology page before its CORE rewrite: stock guest, #139 scenarios, the 2-card aggregate measurement | superseded by `../TOPOLOGY_AND_SETTINGS.md` (#308) |
| `TIER0_RESULTS_2026-08-26.md` | Tier 0 codegen results | shipped; ecmult window 21 is the default |
| `TIP_BLOCK_BIGINT2_2026-08-28.md` | bigint2 on the tip block | **contains its own retraction** (7.53x → 4.48x) |
| `WITNESS_WIRE_PROFILE_2026-08-28.md` | witness deserialisation profile and the `PackedHash` encoder | encoder shipped (#208) |
| [`releases/`](releases/) | release bodies of v0.20.0 (pre-publish draft) and v0.21.0–v0.21.4 | the GitHub releases are canonical; `../../CHANGELOG.md` |

## Where the experiment branches went

The arms these records cite are **git tags**, not branches: `archive/<branch-name-with-dashes>`.

An experiment branch is a fixed point, not work in progress, and leaving ten of them in the branch
list makes the two that ARE work in progress hard to see. A tag says the same thing and says it more
honestly. Nothing was lost — each tag is the exact tip the branch had:

```sh
git fetch origin 'refs/tags/archive/*:refs/tags/archive/*'
git show archive/feat-stack-integration          # the arm GHOST_GAINS.md cites
git diff main...archive/exp-139-bigint2-middle-path
```

⚠ **Retire the branch and its citation together.** These records name their arm so a number can be
traced back to the code that produced it; a citation pointing at a deleted branch is worse than no
citation, because it looks resolvable. If you archive a branch, rewrite the references in the same
commit.

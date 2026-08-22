# Segment distribution — overnight task list

Updated as work proceeds. `[x]` done, `[~]` in progress, `[ ]` not started, `[!]` blocked.

## Build
- [x] B1. Design doc (`docs/SEGMENT_DISTRIBUTION.md`)
- [~] B2. P0 — assembly split in vendored risc0: `assemble_from_segment_receipts`, with
      `prove_session` refactored to call it so both paths share assembly and cannot drift
- [ ] B3. P0 — host command `seg-distribute`: execute, prove each segment through a
      bincode round-trip (proving it survives a wire), assemble, verify vs METHOD_ID
- [ ] B4. P0 gate — distributed receipt journal must equal monolithic `prove()` journal
- [ ] B5. P1 — file-based stages: `seg-export`, `seg-prove <dir> <i>`, `seg-assemble <dir>`
- [ ] B6. P2 — local orchestrator, N worker processes over a work dir
- [ ] B7. P2 — measure distributed overhead vs monolithic

## Test (needs the GPU; near-tip e2e holds it until ~02:30)
- [ ] T1. P0 correctness on a small block (170 / 130000)
- [ ] T2. P0 correctness on block 741000 chunk
- [ ] T3. P2 multi-worker run, overhead measured
- [ ] T4. Near-tip e2e result collected + aggregate time recorded

## Cleanup
- [ ] C1. Delete 10 local branches whose upstream is gone
- [ ] C2. Update `main` (behind 7)
- [ ] C3. Close #144 — refuted, pipelining is 1.039x on the aggregate not 1.39x
- [ ] C4. Update #143 with the measured aggregate decomposition (24% recursion, not 86%)
- [ ] C5. Update #119 / #146 if the night's runs bear on them
- [ ] C6. Prune stale worktrees (keep any with unmerged work)

## Document
- [ ] D1. Results into `~/hazync-b200-results.txt`
- [ ] D2. Memory updated
- [ ] D3. Final summary for the morning

## Open risks
- **po2 is fixed per session.** Nodes need po2 18 (RAM), cards want 21-22. A
  heterogeneous fleet cannot share one session. UNRESOLVED, biggest design risk.
- One card cannot demonstrate speedup; P2 measures overhead and correctness only.
- Join tree is coordinator-side in P0-P2. Fine on GPU (~0.15 s/seg), not on CPU (14 s).

# Segment distribution — overnight task list

Updated as work proceeds. `[x]` done, `[~]` in progress, `[ ]` not started, `[!]` blocked.

## Build
- [x] B1. Design doc (`docs/SEGMENT_DISTRIBUTION.md`)
- [x] B2. P0 — assembly split in vendored risc0: `assemble_from_segment_receipts`, with
      `prove_session` refactored to call it so both paths share assembly and cannot drift
- [x] B3. P0 — host command `seg-distribute`: execute, prove each segment through a
      bincode round-trip (proving it survives a wire), assemble, verify vs METHOD_ID
- [~] B4. P0 gate — distributed receipt journal must equal monolithic `prove()` journal
- [x] B5. P1 — file-based stages: `seg-export`, `seg-prove <dir> <i>`, `seg-assemble <dir>`
- [x] B6. P2 — local orchestrator, N worker processes over a work dir
- [ ] B7. P2 — measure distributed overhead vs monolithic

## Test (needs the GPU; near-tip e2e holds it until ~02:30)
- [~] T1. P0 correctness on a small block (170 / 130000)
- [ ] T2. P0 correctness on block 741000 chunk
- [ ] T3. P2 multi-worker run, overhead measured
- [ ] T4. Near-tip e2e result collected + aggregate time recorded

## Cleanup
- [x] C1. Delete 10 local branches whose upstream is gone
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

## Progress notes

- B2/B3 built clean. `assemble_from_segment_receipts` is on the `ProverServer` trait with a
  default that refuses; `ProverImpl` overrides it and `prove_session` calls it, so monolithic
  and distributed share assembly.
- B5/B6 collapsed into two commands rather than five: `seg-coordinate` and `seg-work`.
  `Session` holds `Box<dyn SegmentRef>` and does not serialise, so the coordinator must stay
  resident to assemble. Only segment proving leaves the process -- which is the 76% worth moving.
- Claiming is an O_EXCL create of `claim_NNNN`; receipts land via write-to-tmp + rename so the
  coordinator never reads a partial file.
- C1: 10 stale branches cleared. 4 were provably safe (0 unique commits or squash-merged);
  the other 6 had unmerged work and are preserved as `archive/*` tags, NOT deleted outright.
- **VRAM scales with po2** -- ~21.5 GB at po2 21 implies ~2.7 GB at po2 18, so a dozen or more
  CUDA workers fit on one 46 GB card. A single card CAN show real parallel speedup. Demo staged
  at `/root/segdemo.sh` on the box, waiting for the near-tip e2e to release the GPU.
- My own `seg-distribute` prints progress every 50 segments, so a 44-segment run is silent
  throughout -- the same flaw as #145. Not fixed mid-run.

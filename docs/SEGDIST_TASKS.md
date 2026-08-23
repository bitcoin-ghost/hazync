# Segment distribution — overnight task list

Updated as work proceeds. `[x]` done, `[~]` in progress, `[ ]` not started, `[!]` blocked.

## Build
- [x] B1. Design doc (`docs/SEGMENT_DISTRIBUTION.md`)
- [x] B2. P0 — assembly split in vendored risc0: `assemble_from_segment_receipts`, with
      `prove_session` refactored to call it so both paths share assembly and cannot drift
- [x] B3. P0 — host command `seg-distribute`: execute, prove each segment through a
      bincode round-trip (proving it survives a wire), assemble, verify vs METHOD_ID
- [x] B4. P0 gate — distributed receipt journal must equal monolithic `prove()` journal
- [x] B5. P1 — file-based stages: `seg-export`, `seg-prove <dir> <i>`, `seg-assemble <dir>`
- [x] B6. P2 — local orchestrator, N worker processes over a work dir
- [x] B7. P2 — measure distributed overhead vs monolithic

## Test (needs the GPU; near-tip e2e holds it until ~02:30)
- [x] T1. P0 correctness on a small block (170 / 130000)
- [ ] T2. P0 correctness on block 741000 chunk
- [~] T3. P2 multi-worker run, overhead measured
- [x] T4. Near-tip e2e result collected + aggregate time recorded

## Cleanup
- [x] C1. Delete 10 local branches whose upstream is gone
- [x] C2. Update `main` (behind 7)
- [x] C3. Close #144 — refuted, pipelining is 1.039x on the aggregate not 1.39x
- [x] C4. Update #143 with the measured aggregate decomposition (24% recursion, not 86%)
- [ ] C5. Update #119 / #146 if the night's runs bear on them
- [x] C6. Prune stale worktrees (keep any with unmerged work)

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

## Cleanup outcome (00:25Z)

- C1: 10 stale branches gone. 4 provably safe; 6 preserved as `archive/*` tags.
- C2: `main` fast-forwarded to `8528bf2`.
- C3: **#144 closed as refuted** — pipelining is 1.039x on the aggregate, not 1.39x, with the
  wrong 86%-recursion explanation recorded and corrected to ~24%.
- C4: **#143 updated** with the measured aggregate split, the ~18 min floor, the prototype, and
  the po2 design risk.
- C6: worktrees KEPT, all four. Three hold unmerged work and one is the primary checkout.
  `hazync-agg` had 26 lines of uncommitted `HAZYNC_AGG_EXECUTE` instrumentation that a
  `worktree remove` would have destroyed — committed and pushed as `89129a7` instead.
  Pruning a worktree without reading its `git status` first would have lost that.

## P0 RESULT (00:55Z) — PASSED

Block 130000 chunk 0, po2 18, 44 segments, laptop CPU:

    execution 2.2 s | proving 1913.3 s | assembly 1445.6 s | TOTAL 3361.1 s
    wire out 3.02 MB (0.069/seg) | wire back 11.22 MB (0.255/seg)
    DISTRIBUTED RECEIPT VERIFIED against METHOD_ID
    digest ce5e105094d8d307b81453b6e20821cb7b1643ba8969c5f9ba81bbe9b3839406

Two corrections that fall out of it:

- **Return traffic is 3.7x outbound.** Every bandwidth figure quoted this session counted
  only the outbound segment, so they are ~4.7x too low. Conclusion survives, numbers did not.
- **Assembly is 43% of total on CPU**, against ~24% on GPU. The join tree has to distribute
  too if CPU nodes are ever workers -- coordinator-side joins are fine at 0.15 s/segment on a
  card and not at 14 s on a node.

B4 still open: the monolithic prove for the digest comparison is running (~55 min on CPU).
The first gate script extracted "ab" from the word "above" in seg-distribute's own
explanatory output, because `[0-9a-f]+` matches English. Re-run anchored to `{64}`.

## B4 GATE PASSED (01:05Z)

    distributed  ce5e105094d8d307b81453b6e20821cb7b1643ba8969c5f9ba81bbe9b3839406
    monolithic   ce5e105094d8d307b81453b6e20821cb7b1643ba8969c5f9ba81bbe9b3839406   IDENTICAL

Overhead: monolithic 3094 s vs distributed 3361 s = **8.6%**, covering serialisation of every
segment out, every receipt back, and a verify per receipt. Pays for itself at two workers.

The gate script itself said FAILED -- it was comparing the "ab" it scraped from prose. Wrong
in the safe direction, but a gate that needs a human to re-read its inputs is not a gate.

## 2-WORKER RESULT (02:20Z)

    worker wall 1569.9 s | assembly 1300.5 s | TOTAL 2872.6 s | split 22/22
    digest ce5e1050...39406 -- IDENTICAL to 1-worker and to monolithic

    monolithic 3094 s | 1 worker 3361 s (+8.6%) | 2 workers 2873 s (-7.1%)

**Two workers beat monolithic.** But worker wall is 1.22x for 2x workers, not 2x -- the two
processes share 16 cores. **CORRECTION: a single machine cannot demonstrate distribution
speedup at any po2, CPU or GPU.** Fitting in VRAM is not independent compute. My earlier
claim that one L40S could show real speedup was wrong. Expect the GPU sweep to be sub-linear.
Do NOT quote 1.22x as a scaling factor for separate machines.

## JOIN TREE GATE PASSED (07:46Z)

    tree    ce5e105094d8d307b81453b6e20821cb7b1643ba8969c5f9ba81bbe9b3839406
    linear  ce5e105094d8d307b81453b6e20821cb7b1643ba8969c5f9ba81bbe9b3839406   IDENTICAL

Cost unchanged: 3106 s tree vs 3094 s linear, 0.4% apart -- a pure restructuring, exactly as
intended. `join` is associative over the ordered sequence, which is what the change rested on.

All three steps of join-tree distribution are now BUILT:
  1. balanced tree replacing the linear fold      70b63cf  GATE PASSED
  2. worker-side lifts                            7f7fbd0  designed only
  3. distributed join levels                      10fc1a9  gate running now

Step 3 did not need step 2: the coordinator lifts locally and distributes only the joins,
which is the half with the log-depth structure.

## NEAR-TIP 16-CHUNK E2E PASSED (08:01Z)

    BLOCK 962000 AGGREGATED in 1466.2s -- succinct receipt VERIFIED
    tip_hash 4403cf83...  cum_work 547530165750508549308877  UTXO leaves 1788

Full production partition on a near-tip block (8,006 inputs). The scale gap is closed.

Per-chunk: 871 877 783 776 835 928 908 894 896 892 893 [1029 po2-21] 896 898 897 894
           = 14,167 card-seconds, mean 885 s

**The aggregate estimate was 37% low** -- ~1,070 s projected, 1,466 s measured.

    block = 14,167/N + 1,466   =>   floor 24.4 min at infinite cards (was estimated ~18)

Worse than every previous estimate, and measured rather than modelled. Ten minutes is
unreachable with cards alone, so segment distribution matters more than before, not less.
With segments distributed: ~30 workers -> 9.5 min.

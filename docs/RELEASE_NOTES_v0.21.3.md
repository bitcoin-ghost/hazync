# Hazync v0.21.3 — a board that frees itself, and work that is credited to whoever did it

Two threads. One is the frontier: a board that froze for thirteen hours, and a block no worker could
ever prove. The other is attribution: proving, folding and anchoring are three different jobs, and
until now the board could only see one of them.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. The guest, the circuit and the
> verifier logic are unchanged. Every existing proof stays valid and the board does not reset.

> ⚠ **The CUDA host is current again.** `hazync-host-x86_64-linux-gnu-cuda` had been carried forward
> from the v0.21.0 build (`af0534c`) through two releases, so it was missing five releases of host
> changes including the `.hzk` output naming. This one is built from this release's source. Both
> hosts now report the same guest and behave the same way.

---

## The frontier

- **#281 / #283: a range that can never join genesis is refused at submit.** On 2026-09-11 a
  contributor submitted 50-block chunks with *inclusive* upper bounds, so each began on the block its
  predecessor ended on: `[30000..30050]`, `[30050..30100]`, `[30100..30149]`. Every proof was valid
  and every one verified — but `_frontier_chain` needs `prev.hi + 1 == lo` exactly, so from the
  second chunk on none of them could ever link to genesis. Because `claim()` is coverage-based, block
  30,051 then *looked* proven and was never handed out again. **The board froze at 30,050 for
  thirteen hours** while `proven` kept climbing, `failed` stayed empty and `stalled_for` reported 0,
  because a VERIFIED row sat directly over the blocker. The coordinator already owned the rule that
  made those bounds illegal; it simply never ran it when *accepting* work, only when drawing the
  board. It runs at submit now, so a contributor learns their bounds are wrong while they can still
  fix them.

- **#281 / #284: the blocker is handed out, and the stall is published.** #283 kills that *shape* of
  freeze, not the failure itself — a correction to the claim that it did. The same symptom is
  reachable with no bad bounds at all: a proof of the right height against the wrong predecessor
  state, which is a fork or a stale bundle after a reorg. At width 1 that is trivial to submit and
  nothing rejects it, and nothing should — proving out of order is the entire design, and the
  coordinator cannot know which predecessor is real. So the frontier's blocker is now offered to
  claimants, and a stalled frontier says so instead of reporting zero.

- **#286: assembly is a phase, so a healthy prove is no longer killed for finishing.** Block 39,318
  could not be proved by any worker, ever. Every segment proved; then the host called
  `assemble_from_segment_receipts` — one blocking call that lifts and joins every segment receipt —
  and printed nothing while it did. The watchdog saw silence, killed a perfectly healthy prove at
  600 s, retried, killed it again, gave up and re-claimed: about two hours of GPU per cycle, forever,
  with the frontier unable to pass it.

## Attribution — who did what (#244)

- **`/api/spine/segments`: who absorbed each block into the spine.** The record was never lost. Every
  absorption writes a permanent `spine:1-<hi>` row carrying the contributor, but the only thing
  reading those rows was the 40-row activity feed, so attribution aged out within minutes of the
  work. The endpoint re-reads rows that were there all along and collapses consecutive advances into
  runs. Keyed on **pubkey, not handle**: two anonymous contributors both present as a null handle, so
  merging on the handle would hand one of them the other's blocks. One assumption is made explicit
  rather than hidden — a row says the head *reached* `<hi>`, not where it came from, so the earliest
  surviving row is taken to start at block 1, and `first_advance_hi` is published so a caller can
  check it. On the live board it is 1, so nothing is unattributed.

- **The leaderboard splits proving, folding and anchoring.** It showed one number: distinct blocks
  covered by *any* range you submitted. A fold submits a wide range, so folding credited the folder
  with blocks other people proved — measured on the live board, the columns summed to 43,868 against
  42,711 blocks actually proven, 1,157 counted twice. Anchoring counted for nothing at all, so
  thousands of absorptions were invisible.

  Telling a fold from a proof is the whole difficulty, since both are a wide row in `vranges`. The
  submit gate already draws the line — a range either covers fresh territory or is exactly tiled by
  ranges already on the board — and `fold-range` is binary, so the test is an exact seam rather than
  a heuristic.

  ⚠ The proved column still does not sum to the headline, and must not be made to. Two people can
  prove the same block: during the #281 repair, blocks `[30051..30100]` and `[30101..30149]` were
  re-proved over bounds another contributor had already covered, so the columns run 99 blocks above
  `proven`. Both did that work. Crediting a *folder* with someone else's blocks was the bug; genuine
  duplicate proving is not the same thing and is not hidden.

- **An append-only lineage of every canonical guest id.** `reproduce/LINEAGE.tsv` — 17 rows,
  2026-07-18 to 2026-09-07, each with the risc0 toolchain it was built against. It is *derived* from
  the git history of `reproduce/METHOD_ID` rather than typed, so deleting a row from the file does
  not delete it from the evidence and the gate fails. A shallow clone cannot reconstruct it and the
  check refuses to run rather than report deletions it invented — measured, not hypothetical: the
  clone it was first generated in was grafted and silently lost the two oldest rows.
  `docs/PROOF_DURABILITY.md` carries the open question behind it: whether more than one method id
  should ever be acceptable, and what that would take.

## Proofs have a name and an extension (#278)

`curl -O https://…/api/proof/1` wrote a file called `1` — no extension, nothing saying what it was.
Served proofs are now named `hazync-<lo>.hzk` / `hazync-<lo>-<hi>.hzk`, built from the parsed range
and never from the raw path. **No bytes change:** every proof on the board stays valid, every stored
hash stays correct, and a `.bin` saved last week still verifies. Files at rest keep their names, so
the retention gate needs no migration. Readers accept both extensions; only writers changed, and the
worker now *resolves* the host's output name rather than assuming it — so a worker and host from
different releases still fold.

## Release tooling (#275)

Step 4's fallback for the CUDA host sat under a *staleness* check, which is true for exactly the
fresh artifact that needs it. The branch never ran, and v0.21.1 and v0.21.2 were both published only
because the operator passed `HAZYNC_ATTEST_…` by hand. It now asks whether the binary *can run here*,
under the same timeout `check-dist.sh` uses, and distinguishes the four outcomes — including keeping
an operator-supplied attestation over anything read from the bytes, since a measured id beats an
inferred one. **This release is the first to pass step 4 unaided.**

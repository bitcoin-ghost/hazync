# Step 2 — worker-side lifts (designed, not built)

Blocked deliberately on the join-tree correctness gate. Do not build this on top of an
unvalidated tree: if the digest comes out wrong, there would be two candidate causes.

## Why it is worth doing

Lifts are per-segment and fully independent — the one part of assembly that needs no
restructuring at all to distribute. On the measured 44-segment CPU chunk they are
44 x 13.8 s = **607 s of the ~1300 s assembly**, i.e. 47% of it, sitting on the coordinator
for no reason.

## The obstacle, and it is the whole design

`assemble_from_segment_receipts` merges the session journal and assumptions into the **last**
segment receipt's claim *before* anything is lifted:

```rust
segments.last_mut()?.claim.output.merge_with(&session.journal...)
```

So the last segment cannot be lifted by a worker — the worker does not have the session and
could not do the merge. Everything else can.

## Split

```
worker, segment i < N-1     prove_segment -> lift -> write lift_NNNN.bin  (SuccinctReceipt)
worker, segment i = N-1     prove_segment         -> write rcpt_NNNN.bin  (SegmentReceipt)
coordinator                 read lifts 0..N-2
                            take rcpt N-1, merge session output into its claim, lift it
                            join tree over all N
                            resolve assumptions
                            Receipt::new, verify vs METHOD_ID
```

A worker knows whether it holds the last segment: it has the index and the count from
MANIFEST. No new coordination.

## New entry point needed

```rust
fn assemble_from_lifted(
    &self,
    ctx: &VerifierContext,
    session: &Session,
    lifted_head: Vec<SuccinctReceipt<ReceiptClaim>>,  // segments 0..N-2, session order
    last_segment: SegmentReceipt,                     // segment N-1, NOT yet merged
) -> Result<ProveInfo>
```

It performs the merge on `last_segment`, lifts it, appends, runs the join tree, resolves
assumptions, and builds the `Receipt` — reusing the tail of `assemble_from_segment_receipts`
so the three paths (monolithic, distributed-from-segments, distributed-from-lifts) keep
sharing assembly rather than growing copies.

## What is lost, and it is worth stating

`assemble_from_segment_receipts` builds a `CompositeReceipt` and calls
`verify_integrity_with_context` plus `check_claims` on it. With only lifted receipts there is
no composite to check, so those two self-consistency checks go away. What remains is:

- each returned `SuccinctReceipt` verified on arrival (the untrusted-worker defence, unchanged)
- `join` checking `a.post == b.pre` at every level, which catches an out-of-place segment
- the final `Receipt::verify(METHOD_ID)`, which is the actual gate

That is a real reduction in defence in depth against a *buggy prover*, not against a
malicious worker. Worth a flag in review, not a blocker.

## Expected effect

Coordinator assembly at 44 segments drops from ~1300 s to the joins alone, ~602 s, with the
607 s of lifts moved onto workers. Combined with distributing join levels (step 3) the
projection is ~112 s at 22 workers.

**Projection, not measurement.** One machine cannot show it.

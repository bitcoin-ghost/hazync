# Proof durability

A receipt stays verifiable indefinitely provided we keep its method id **and** can rebuild the risc0
verifier that checked it. Nothing expires on its own. What destroys a back catalogue is losing the
record of which id was current when, or losing the ability to reconstruct the verifier that checked
it.

The bytes are not the asset — a proof is a public, reproducible fact about public data, and
reproducibility is the product, so there is no scarcity in the artifact. What is scarce is the
recorded claim that a given prover proved block N first. That makes the **registry** the asset, and
it makes "does the record still verify in two years" the question that decides whether any of it is
worth anything.

⛔ **A method id cannot be made mutable.** It *is* the commitment to the guest code; that is its
entire job.

⚠ **The soft-fork analogy does not transfer, and an earlier version of this document leaned on it.**
In Bitcoin a soft fork is safe because old, permissive nodes are protected by a strict majority
enforcing the tighter rule; permissiveness is tolerable only because something else is doing the
enforcing. A proof system has no majority and no enforcement. Anyone choosing which id to prove
under simply picks the most permissive one on offer. Whatever an append-only set of accepted ids is,
it is not a soft fork, and reasoning about it as one imports a safety property that is not there.

---

## 1. Append-only lineage — **built** (#244)

`reproduce/LINEAGE.tsv` records every id that has ever been the canonical `METHOD_ID`, in order,
with the risc0 toolchain it was built against. 17 rows, 2026-07-18 to 2026-09-07.

It is **derived, not typed**. `scripts/lineage.sh` reconstructs it from the git history of
`reproduce/METHOD_ID`, and `--check` fails if the committed file disagrees. Git history is already an
append-only store and it recorded this whether or not anyone wrote it down, so deleting a row from
the file does not delete it from the evidence.

That matters because the record used to be prose, and prose is not a store:

* **The prose chain was incomplete.** Nothing in it supersedes `7a8b29e0`, superseded in fact on
  2026-07-26, so reading the "Superseded the earlier id X…" links alone loses that row entirely.
* **Nothing pinned the risc0 version beside the id**, and the id alone is not enough to rebuild a
  verifier.
* **`check-versions.sh` cannot see a deleted row.** It validates ids that *are* mentioned, so an id
  swept out by a blind re-baseline is indistinguishable from one that never existed.

⚠ **The lineage begins where the record begins.** `reproduce/METHOD_ID` was created on 2026-07-18,
41 commits into the repo. Ids used before that were never written down and are not recoverable here;
the table does not pretend otherwise.

⛔ **A shallow clone cannot reconstruct it**, and the check refuses to run rather than report
deletions it invented. This is not hypothetical — the clone this was first generated in was grafted
at 412 commits and silently lost the two oldest rows, `d1fc4065` among them. A gate that cries
deletion on a shallow clone is a gate that gets switched off.

Gates: `scripts/lineage.sh --check` and `scripts/test-lineage.sh` (+ `--control`), both in CI.

## 2. Accepting more than one method id — **an open decision, not pending code**

The shape suggested in #244 is `(method_id, risc0_version, valid_from, valid_to)`, append-only, with
a verifier accepting any listed entry rather than only the current one. Layer 1 supplies the first
three columns already.

### It could only ever cover changes that did not alter what is asserted

A proof is a claim about a block under a set of rules. If a change was purely internal — a faster
circuit computing the identical statement — the old proof is still true. If a change extended the
rules being checked, the old proof asserts *less*, and honouring it would mean treating a narrower
claim as the current one.

**Hazync's rule coverage grew as it was built**, which is what that distinction looks like here in
practice. The first three weeks added Core rules steadily:

| Rule added | Commit | Ids that predate it |
|-----|--------|------|
| P2SH sigop count guarded with `IsPayToScriptHash()` (#1) | `1ba1e10`, 2026-07-24 | 3 |
| Two `cshims.c` corrections | `d5723a4`, 2026-08-02 | 9 |
| BIP30 closed by a coinbase-only SMT (#54, #83) | `70a0fa5`, 2026-08-03 | 10 |

Those sets nest. Proofs from that period assert less than today's — not because anything was broken,
but because less had been built yet, and nothing depended on them.

**Since 2026-08-03 the rule set has been stable.** Every guest change across the 7 ids after that
date is performance or build work: chunk payload encoding, segment distribution and the join tree,
ECMULT window knobs, the coprocessor field backend, and a warning fix. So the candidate set is a
date boundary rather than a per-id verdict — **7 of 17 share today's rule set; 10 are development
era.**

### ⛔ It is not a question the top-level verifier can answer alone

Composition is homogeneous **by construction, enforced inside the circuit**. A fold verifies both
children with `env::verify(self_id, …)` and then asserts `l.self_id == self_id && rr.self_id ==
self_id` (`prover/methods/guest/src/main.rs:1502-1508`); the chain step does the same; and the
verifier asserts the final `self_id` equals `METHOD_ID` (`verifier/src/lib.rs:100`) — together
forcing every level of a chain to one id.

So widening only the standalone verifier would let an old proof still be *checked* as an individual
artifact, but it could never be folded into a chain, and the spine would restart at every re-baseline
regardless. Any version of this that actually preserves accumulated work requires **the guest** to
accept children under earlier ids — which moves the set inside the circuit and undoes the S1 pinning
that is currently verifier-asserted, and requires journal-format compatibility across the accepted
ids (broken before, at `68819a54`).

### What makes it hard

- Entries would be **permanent**: an append-only history cannot withdraw an id.
- **"Purely internal" is a finding, not an observation.** `1d6c3792` is recorded as leaving Core's
  consensus code unchanged, and it did — but it relocated three values (wtxids, coin leaves, sequence
  numbers) so the aggregate *receives* them from chunks rather than recomputing them, changing what
  it checks versus what it trusts. Establishing that this preserved the statement took two same-run
  differentials over 8,006 inputs plus a 16-chunk end-to-end on block 962,000; within the same
  change the lock check was deliberately *not* relocated, because getting it wrong would have been
  fail-open.
- Our builds are **reproducible on purpose** (`reproduce/Dockerfile`), so a listed id that turned out
  not to assert what we thought could be rebuilt by anyone and used to produce a proof that verifies.
  Nothing forged — a genuine proof of a narrower claim. The security of such a history is the
  security of its weakest entry.
- Any such list must be **compiled into the verifier and never fetched at runtime**; pulled from the
  coordinator, a compromise there would mean accepting arbitrary ids.

⚠ None of this is a live exposure: the verifier and the coordinator both pin a single id today, and
the circuit pins composition.

**The open question**, which no code should precede: can "this change did not alter the statement" be
established mechanically, rather than asserted by a human reading a diff? If it can, this becomes far
safer than the above suggests. If it cannot, the honest answer may be that one id is the only sound
answer. Recorded here rather than chosen unilaterally.

## 3. Recursion as an upgrade path — and what it cannot do

If old proofs need to appear under a new method id, one route is not to re-prove the block but to
**lift** it: prove the old verifier's execution inside the new guest. The lift and aggregate
machinery already exists.

⛔ **Correction.** An earlier version of this document claimed lifting "disposes of layer 2's hard
case" by lifting an old proof under a guest that has the fix. **That is wrong.** Lifting preserves
the *statement*. Lifting a proof from a development-era guest yields "guest X accepted this block",
not "this block satisfies the current rule set" — the missing check was never performed, so no amount
of recursion recovers it. In principle a bespoke migration circuit could lift the old receipt *and*
perform just the missing check, but for something like BIP30 that means reconstructing the
coinbase-SMT state the old run never built, which is most of the work anyway.

So lifting lands on exactly the same boundary as section 2, reached from the other direction: useful
for the stable-rule era, useless for the development era. **Blocks proved under a narrower rule set
have to be re-proved.** That is the correct outcome rather than a limitation — a proof of a narrower
claim should not count as proof of the current one.

Where lifting stays interesting is as an alternative to widening anything at all: reconciling old
proofs *to* the current id, rather than changing what the verifier accepts, would recover the work
without a permanent list of ids.

⏰ **Unmeasured, and possibly decisive:** what lifting a proven range costs relative to proving it
from scratch. If lifting is cheap, much of section 2 stops mattering. If it is expensive, re-proving
is the answer anyway. One measurement settles it.

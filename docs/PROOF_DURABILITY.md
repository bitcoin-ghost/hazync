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
entire job. The soft-fork analogue is an append-only accepted-set, not a changeable id.

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

## 2. An accepted-set of method ids — **needs a decision, not code**

The shape is `(method_id, risc0_version, valid_from, valid_to)`, append-only, with a verifier that
accepts any historically valid entry rather than only the current one. Layer 1 supplies the first
three columns already.

⛔ **"Any historically valid entry" cannot be the rule, and this is measured, not a worry.** Some
re-baselines in the lineage *are* consensus fixes, so accepting their predecessors accepts proofs
from guests with defects we have since closed:

| Fix | Commit | Lineage rows that predate it |
|-----|--------|------------------------------|
| P2SH sigop count guarded with `IsPayToScriptHash()` (#1) | `1ba1e10`, 2026-07-24 | `d1fc4065`, `c029cee4`, `601d7ca2` |
| Both `cshims.c` defects | `d5723a4`, 2026-08-02 | the 9 rows up to and including `be5e0528` |
| BIP30 closed by a coinbase-only SMT (#54, #83) | `70a0fa5`, 2026-08-03 | the 10 rows up to and including `71790584` |

**10 of the 17 rows carry at least one since-fixed consensus defect.** An accept-all set would make
those proofs verify again, under a verifier that says "valid".

So the accepted-set needs a per-entry classification — *superseded for speed* versus *superseded for
soundness* — and the default must stay canonical-only, with historical acceptance opt-in and named.
The lineage prose already distinguishes the two where it exists (`3f52baff` is recorded as "a
compile-time speed trade with no consensus change"; `1d6c3792` as "Core's consensus code is
unchanged"), but that classification has never been asserted anywhere a verifier could read.

**The decision to take before any code:** is a proof under a superseded-for-speed guest acceptable
to the board and the public verifier, or does everything reconcile to the canonical id? The answer
changes the verifier's core, the wasm bytes, and what the board is claiming when it says "verified".
Recorded here rather than chosen unilaterally.

## 3. Recursion as the upgrade path — never re-prove a block

If old proofs ever genuinely need to appear under a new method id, do not re-prove the block.
**Lift** it: prove the old verifier's execution inside the new guest. The lift and aggregate
machinery already exists. It is not free, but it is orders of magnitude cheaper than re-proving, and
it means a guest change is never a catastrophe.

This also disposes of layer 2's hard case cleanly: a proof from a superseded-for-soundness guest
should be *re-proved or lifted under a guest that has the fix*, never waved through by widening the
accepted-set.

# One `METHOD_ID`, and what we would do the day we are forced off it

> **Status: a position, not a plan of work.** Nothing here is scheduled or being built. It records
> where Hazync stands on proof durability and what we intend to carry the day something forces a new
> guest id, so that decision is argued out in advance rather than improvised under pressure.
>
> Background and the layer-by-layer detail are in
> [`PROOF_DURABILITY.md`](PROOF_DURABILITY.md) and issue #244. Two claims in that issue are corrected
> there and restated at the end of this document.

## Where we are: Method 1, one id

A Hazync proof is checked against a **`METHOD_ID`** — a hash committing to the exact guest code that
produced it. The verifier has one compiled in and accepts a proof only if it matches. The coordinator
does the same and rejects anything else as a build mismatch.

So a guest change retires the whole back catalogue at once. That has happened **17 times**
(2026-07-18 → 2026-09-07); the record is
[`reproduce/LINEAGE.tsv`](https://github.com/bitcoin-ghost/hazync/blob/main/reproduce/LINEAGE.tsv),
derived from git history rather than typed by hand, so a row cannot be quietly dropped.

**Method 1 has real virtues and we are not apologising for it.** "Verified" has exactly one meaning.
One build can produce an accepted proof, and that is the entire trust surface. Nothing has to be
classified, so nothing can be misclassified. It is easy to state to an auditor and easy to reason
about at three in the morning.

**And the canonical guest is more stable than the re-baseline count suggests.** Its rule coverage has
been unchanged since 2026-08-03; the changes since have been performance and build work. The
experimental **GHOST** build, where new speed levers land first, is deliberately separate — it does
not ship and cannot contribute to the board — so exploring it costs the board nothing.

So we are **not** proposing to add machinery speculatively. There is no re-baseline scheduled, and
building a mechanism to survive one would itself require a re-baseline — spending the exact cost we
are trying to avoid.

## The day we are forced to move

Sooner or later something will force a new `METHOD_ID`: a consensus bug, a security fix, or a speed
change worth the disruption. On that day the back catalogue is written off **whether or not** any of
this exists.

That is the moment to also carry **Method 2**, so it is the *last* time it happens.

### Method 2: an accepted set that starts at the new id and only ever tightens

Two constraints, and they do all the work:

1. **The set starts at the id we are moving to** — not at the beginning of the lineage.
2. **A new id joins only if it is stricter than or equal to those already in it**: everything the new
   guest accepts, the existing guest would also have accepted. Rules may tighten; never loosen.

That is the soft-fork shape — `new-valid ⊆ old-valid`.

**Why constraint 1 matters more than it looks.** The standing objection to any accepted set is that
its security is that of its **weakest member**, and being append-only, a bad member can never be
withdrawn. Starting at the id being adopted makes the weakest member *the guest shipping that day* —
by definition the security level already in production. The set can never be weaker than the status
quo, and every addition must be at least as strict.

This matters because the lineage is not uniformly safe. Coverage grew as the thing was built: the
first three weeks added the P2SH sigop guard (`1ba1e10`), two `cshims.c` corrections (`d5723a4`) and
BIP30 via a coinbase-only SMT (`70a0fa5`). Proofs from that period assert less than today's — not
because anything was broken, but because less had been built. **10 of the 17 ids are from that
period, and a set reaching back to them would make narrower claims verify again.** Starting at the
current id excludes them by construction rather than by anyone's judgement.

## The hard part, stated as the hard part

**Most guest changes are neither stricter nor looser — they are *equal*.** Speed work that computes
the identical statement, and exactly the kind we would want proofs to survive. Equality is the harder
claim.

The cautionary case is already ours. `1d6c3792` genuinely left Core's consensus code untouched, but
relocated three values (wtxids, coin leaves, sequence numbers) so the aggregate *receives* them from
chunks rather than recomputing them — changing what it checks versus what it trusts. Establishing
that this preserved the statement took **two same-run differentials over 8,006 inputs plus a 16-chunk
end-to-end on block 962,000**, and within the same change one relocation was deliberately *not* made
because getting it wrong would have been fail-open.

"No rule changed" was a **finding**, not an observation. A scheme that admits ids because someone
asserted the relation while reading a diff is resting on the weakest part of this.

**So the relation should be tested, not asserted** — a gate running the candidate guest and the
current one over a corpus, requiring that everything the candidate accepts, the current one accepts
too. That is differential testing, which is what `1d6c3792` did by hand, and it makes admission a
matter of evidence.

## One structural constraint, so nobody designs around the wrong thing

Composition is **homogeneous by construction, enforced inside the circuit**: a fold verifies both
children with `env::verify(self_id, …)` then asserts `l.self_id == self_id && rr.self_id ==
self_id`, and the verifier asserts the final `self_id` equals `METHOD_ID`.

So widening only the top-level verifier would leave an old proof checkable as a standalone artifact
but never foldable, and the spine would restart at every re-baseline regardless. **For accumulated
chain work to survive, the guest itself must carry the accepted set** — which puts the list inside
the thing whose correctness everything else rests on.

## Open questions

These are the questions we would most like answered, and they are
put to the community in the linked discussion rather than settled here.

1. **Is "starts at the id we are moving to, and only ever tightens" sound?** It is the load-bearing
   claim, and we would rather it failed here than in production.
2. **What would convince you that a change did not loosen anything?** Differential over what corpus,
   and how large? Random blocks are cheap and prove little; our adversarial fixtures are targeted but
   narrow. Is there a standard from elsewhere?
3. **Is there prior art** for upgrading a zk circuit without invalidating the back catalogue? We would
   rather borrow than invent.
4. **Does putting the accepted set inside the guest change your answer to (1)?** Composition forces
   it there.
5. **Is Method 1 simply the right answer?** A single meaning for "verified" is worth a great deal, and
   "your proofs may be retired when the guest improves" is an honest thing to tell contributors. We
   are not assuming Method 2 wins.

Two claims in the original issue (#244) have since been corrected in
[`docs/PROOF_DURABILITY.md`](https://github.com/bitcoin-ghost/hazync/blob/main/docs/PROOF_DURABILITY.md),
and both are worth knowing before replying: the soft-fork analogy as *originally* stated does not
transfer — Bitcoin's version is safe because permissive old nodes are covered by an enforcing
majority, which a proof system has not got, so an accept-everything set would simply invite picking
the most permissive entry. The constraints above are what answer that. And recursion cannot lift a
proof from a narrower guest into a stricter one: lifting preserves the *statement*, so it yields
"guest X accepted this block", not "this block satisfies the current rules".

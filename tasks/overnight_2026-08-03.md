# Overnight run — 2026-08-03

Ship hazync: finish audit #3, hunt the F-1 class, merge, release, deploy, stranger run.

## State at start
- `feat/coinbase-smt` @ 1315435 — audit #3 F-1/F-2/F-3 fixed, all 9 gates green, 27/27 SMT tests
- `feat/bulk-sync-and-ffi-adversarial` @ d828932 — PR #82, F-4/N-2 fixed and pushed
- `main` @ 271da1f

## Phase 1 — finish audit #3

**N-1 FIXED** (`coinbase-smt/src/lib.rs`). The `Proof` doc said siblings are "ordered leaf-to-root";
they are ascending-depth (root-to-leaf), and `compute_root` consumes from the END. Corrected, and
cross-referenced to the "NOT reversed" note in `prove` — which was right all along, so the struct
comment was contradicting the implementation in the one place a reader checks before touching it.
That file's first bug was a spurious `sibs.reverse()` folding to a well-formed but wrong root, so a
doc comment pointing the wrong way is not cosmetic here.

27/27 crate tests still pass.

## Phase 2 — the F-1 class hunt

Method: for every height-gated branch and every exception, ask *does a test drive it from the REAL
precondition, or a convenient one?*

**FIXED — `BIP34Height` was the one buried height still hand-typed** (`29a4a46`). BIP66/BIP65/CSV/
Segwit are all read from Core's compiled `Consensus::Params` and asserted by `assert_core_constants`.
BIP34's 227931 was a literal in two places with nothing checking it, while `reproduce/METHOD_ID`
claimed "nothing consensus-relevant is a hand-typed magic number now". Not a live bug — the value is
right — but an unenforced claim, which is the F-1 shape. Now read from Core in the C++ and
runtime-pinned in the Rust. Cycles 24,400,773 -> 24,401,107 on block 130000 (measured): the real cost
of a call replacing a constant. All three fixtures still VALID.

The guest image id moved, as any guest edit does. It is deliberately NOT written here: `check-versions.sh`
scans docs for tokens claimed as a guest id and fails anything that is not canonical or a documented
predecessor — and it caught this file doing exactly that. The gate is right. A pre-release local build
id in a doc is the stale-id trap the gate exists for, so the measured value lives in the commit message
(`29a4a46`) and the authoritative one comes from the release container.

**FILED #83 — no fixture drives any activation boundary.** Fixtures are at 130000/140000 (before
everything) and 741000 (after everything), so every gate in `validate_block` has one side exercised
and no boundary. Sharpest case: `witness_ok`, whose own comment records a reject-valid liveness bug in
433k–481823 that was fixed with **no test in that window** — asserted by comment, with the fixture set
unable to reproduce the precondition.

**Checked and found adequate:** script-flag activations (guest-pure-fuzz asserts OFF at h-1, ON at h
against independent constants); script-flag exception blocks (driven from the real hashes/heights —
what BIP30's test failed to do, though no end-to-end fixture); coinbase maturity (real data in 140000
plus dedicated harnesses); `assumevalid` (does not exist in the guest — nothing to get wrong).

Recorded the passes as well as the failures: a sweep that reports only problems gives no signal about
what was actually examined.

## Near-miss worth recording

The BIP34 build failed (`mainnet_params` not declared at that point in the file) and the fixtures then
ran **off a stale binary**, printing three VALIDs. Caught only because the build log said exit 101
while the run said pass. Always read the build result before trusting the run that follows it.

## Phase 3/4 prep — two self-inflicted findings worth more than the work

**A gate caught me, correctly.** `check-versions.sh` went red on *this file*: I had written a local
pre-release guest id into it, and the gate fails any token claimed as a guest id that is not canonical
or a documented predecessor. That is precisely the stale-id-in-docs trap it exists for, and the fact
that the id was mine and fresh rather than someone else's and stale makes no difference to a reader.
The measured value now lives only in the commit message; the authoritative one comes from the release
container.

**My own monitor gave a false completion.** I armed a watcher that reported "REPRO BUILD COMPLETE"
because `docker image inspect hazync-repro` succeeded — against an image built **2026-07-26**, a week
old, while the build was still in `apt-get`. A completion check that cannot distinguish "this build
finished" from "a tag with this name exists" is the same defect class as the `ffi_smoke` printf and the
hardcoded benchmark constant: a signal that cannot fail. Re-armed to wait on the build PROCESS and
require an image-write line in the log.

Recording it because an overnight run leans on these signals unattended, and a false green here would
have had me re-pin `reproduce/METHOD_ID` from a week-old image.

## SMT branch verification (pre-merge)

All 9 gates PASS. Crate tests: accumulator 24, rangestate 3, coinbase-smt 27, guest-pure-fuzz 6,
leaf-differential 6, audit-fuzz 2 — all green. Fixtures 130000 / 140000 / 741000 all VALID on the
current guest.

## Blocked

#83 needs archive-node access to pull the 11 boundary fixtures (`getblockhash` + `getblock`,
read-only). `fetch_block_rpc.py` now takes `HAZYNC_RPC_{HOST,PORT,COOKIE,AUTH}` so it is one command
once reachable. Synthetic substitutes were assessed and rejected — mode 1 does not journal the
individual gate flags, so a synthetic boundary test could not say WHICH gate fired.

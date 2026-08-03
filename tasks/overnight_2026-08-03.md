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

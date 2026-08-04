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

## #83 — boundary fixtures pulled (the loop prompt authorises coordinator access for phase 5,
## which makes a read-only getblock well inside scope)

Coordinator's archive node is at height 960833. Pulled 11 fixtures read-only via
`fetch_block_rpc.py` (`getblockhash` + `getblock` only, nothing written, no service touched):

    227930 227931   BIP34 + block-version v2 gate
    363724 363725   BIP66 / v3
    388380 388381   BIP65 / v4
    419327 419328   BIP113 locktime -> MTP
    481823 481824   segwit / witness_ok
    434499          THE sharp one

**434499 was found by scanning, not guessed.** It is the first block in 433k–481823 whose coinbase
carries a BIP141 witness commitment (`6a24aa21a9ed`) with NO witness data anywhere in the block —
exactly the shape the `witness_ok` comment says the pre-fix code rejected. Two more at 434504 and
434535, so the shape is not a one-off. This is the one case that cannot be synthesised, which is why
the synthetic interim was rejected.

### Results so far

| height | result |
|---|---|
| 227930 / 227931 | VALID / VALID |
| 363724 / 363725 | VALID / VALID |
| 388380 / 388381 | VALID / VALID |
| 419327 / 419328 | running |
| 481823 / 481824 | running |
| 434499 | running |

The later blocks are 2.7 MB with large input counts and take well over ten minutes each to execute,
so the remaining five run in the background. No boundary has failed so far — which is the expected
outcome, since #83 was filed as a COVERAGE gap and not a known bug. The value is that "expected" is
now measured rather than assumed.

### 434499 VALID — the finding this was all for

The pre-segwit block carrying a BIP141 witness commitment with no witness data validates against the
current guest. That is the shape `witness_ok`'s own comment says the pre-fix code REJECTED, and until
now the fix was asserted by that comment with nothing driving it. It is now measured.

Seven of eleven boundaries confirmed VALID: 227930/227931, 363724/363725, 388380/388381, 434499.

### CI cost, measured rather than estimated

Block 741000 (670 prevouts) takes ~45s. Prevout count is the cost driver:

| set | fixtures | prevouts | est. CI |
|---|---|---|---|
| cheap | 227930, 227931, 363724, 363725, 388381 | ~2,760 | ~3 min |
| heavy | 388380, 419327/8, 434499, 481823/4 | ~30,700 | ~34 min |

So the fixtures go in per-push only for the cheap set; the heavy six belong on a schedule or manual
dispatch. Adding 34 minutes to every push to re-prove blocks that do not change is the kind of cost
that gets a suite disabled six months later.

Storage measured too, not assumed: 18 MB raw compresses to **6.9 MB** in git against a 23 MB repo.
Worth it to close a consensus coverage gap permanently.

Note `block_363725.json` is 678 bytes — a coinbase-only block. A boundary fixture that costs nothing.

## The retarget-boundary finding — #83's real discovery

481824 first came back **`block_valid=true retarget_ok=false`**. Reading that carefully mattered: the
block passed every consensus flag. What failed was the *harness*.

`build_full` fabricated two in-boundary values — `prev_time` as "this block's time minus 600s" and
`epoch_start` as "minus 1000 blocks". Harmless at a non-retarget height, where the guest carries nbits
through unchanged and never reads them. **Fatal at a retarget height**, where `calc_next_bits` consumes
`epoch_start` and the expected target is derived from a timestamp that never existed.

And this is not a corner: **BIP9 soft forks activate ON retarget boundaries by design.** 419328 (CSV)
and 481824 (segwit) are both exactly 2016·k. So the two activation heights that most needed a fixture
were precisely the two the fixture format could not express — the coverage gap had a second floor under
it.

Fixed: `fetch_block_rpc.py` now emits real `epoch_start` (first block of the PREVIOUS epoch, what
Core's `CalculateNextWorkRequired` takes as `nFirstBlockTime`), `prev_time` and `prev_bits`. The host
uses them when present and keeps the synthetic fallback for pre-#83 fixtures, which is correct for them
because none is a retarget height.

**481824 now VALID** — 5,192 inputs, 15.7B cycles, 446s.

## A harness bug of my own, worth recording

The first `ci_boundary_tests.sh` reported 227931 and 363725 as consensus failures. They were fine. The
cause: `set -o pipefail` with `| grep -q`. grep exits on first match, closes the pipe, host takes
SIGPIPE, pipefail turns that into a failed pipeline — so a block that printed VALID was reported as
REJECTED. It was a RACE: fast blocks finish writing before grep leaves and "pass", slow ones do not.
Capture first, then match.

A test that reports valid mainnet blocks as consensus failures is worse than no test — it is the
inverse of the F-1 problem and would have blocked a release on nothing.

### Boundary results — 10 of 11 confirmed

| height | gate | result |
|---|---|---|
| 227930 / 227931 | BIP34 + block-version v2 | VALID / VALID |
| 363724 / 363725 | BIP66 / v3 | VALID / VALID |
| 388380 / 388381 | BIP65 / v4 | VALID / VALID |
| 419327 | BIP113 (pre) | VALID |
| 419328 | BIP113 (activation, retarget) | re-running on the fixed fixture |
| 434499 | pre-segwit block with an early commitment output | **VALID** |
| 481823 / 481824 | segwit / witness_ok | VALID / **VALID** |

419328's earlier FAILED was against the pre-fix fixture — the background job started before
`epoch_start` was added. Same cause as 481824, which now passes.

No consensus defect found in any boundary, which is the expected result: #83 was filed as a coverage
gap, not a suspected bug. What the exercise actually produced was two harness defects that would each
have mattered later — a fixture format that could not express a retarget block (so the two BIP9
activation heights were untestable), and a test harness that reported valid mainnet blocks as
consensus failures.

## STOPPED at phase 4 — container METHOD_ID, and why I did not proceed

Canonical at the time, since superseded (container, fixed paths):  b161735a13d120a29aaf1e3c910bc6cbb486467bef40c04fe839aa4044170b3d
Local build (this box):              1bed31ef0cb83c0dcabe0baaed1a4eff676c838569ffa07e8b96056ec9f32507
reproduce/METHOD_ID still pins:      71790584… (pre-#54, as expected mid-flight)

The loop's stop condition is "the container METHOD_ID disagrees with your local build". It does. But
that condition is **mis-specified**, and the difference is expected:

  reproduce/Dockerfile: "the id ... also depends on absolute build paths baked into the ELF
  ($HOME/.cargo, $HAZYNC_BASE). This container removes that last variable by FIXING every path"

A local build at /home/defenwycke and a container build at /root therefore produce different ids by
construction. The documented reproducibility test is "two independent builds (different machines) that
print the same id" — container vs container, never container vs local.

Verified before drawing that conclusion: NO guest input changed between the container's build context
and now. `git log --since` over `prover/methods/guest/` and `coinbase-smt/` (the only guest path
dependency, per check-guest-inputs.sh) is empty. Everything committed since is fixtures, host code and
docs.

**Not proceeding anyway.** The condition's INTENT is "do not release a guest you have not verified",
and while the source is identical, the eleven boundary fixtures and three regression fixtures were all
executed against the LOCAL guest, not the container one. Same source at different paths should behave
identically, but "should" is doing the work there, and the next step re-pins the canonical id and
resets the live board. Cost of waiting: a few hours. Cost of being wrong: a release that invalidates
every proof and does not verify.

For the operator, the two ways forward:
  a) accept the reasoning above and re-pin to dfc9eeda…, or
  b) run check-full against the CONTAINER guest first, which closes the gap properly.

(b) is cheap — the container already exists.

## Third false-signal bug of the night, also mine

`pgrep -f "docker build -t hazync-repro"` MATCHED THE MONITOR'S OWN COMMAND LINE, because that exact
string appears in the monitor script. So the monitor waited for itself, forever, and every status
check I ran reported "still building" for over an hour after the image was actually finished at 08:22.

Three for three tonight, all self-inflicted, all the same shape — a signal that cannot distinguish the
states it claims to:
  1. `docker image inspect <tag>` matched a week-old image and reported a running build COMPLETE
  2. `pipefail` + `grep -q` reported valid mainnet blocks as consensus failures
  3. `pgrep -f <pattern>` matched the checker itself and never reported completion

Worth stating plainly: the checks I wrote to supervise unattended work failed more often tonight than
the code they were supervising.

## #87 VERIFIED — split Dockerfile, measured

The id from the split build is `dfc9eeda…`, identical to the single-stage build. Confirmed twice, by
two independent monitors, because the whole artifact is worthless if the split moved a path.

Measured, not claimed:

| | single-stage | split, ordinary repo change |
|---|---|---|
| dependency layer | 3506s (~58 min) | **CACHED** |
| guest build | (included) | 772s (~13 min) |

`#10 CACHED` against a touched README — an ordinary change no longer re-runs the RISC0 toolchain
install or the Core clone. Verifying the canonical id after a code change costs ~13 minutes instead of
~an hour.

**The caveat, stated because it will surprise someone:** editing `provision-vps.sh`, `patches/` or
`coreshim/` invalidates the base layer and costs the full hour again. That is correct — they are
inputs to it — but it means the split speeds up code changes, not provisioning changes. It bit twice
during this very session while fixing the `GPU_FEATURES` bug.

**And #87's own bug was caught by building, not reading.** The first split failed with
`GPU_FEATURES: unbound variable`: it is initialised in phase 7, which `HAZYNC_PROVISION=build` skips,
and `set -u` aborts. That was in a commit already pushed, and it failed twice more the same way on the
coordinator before the fix propagated.

## The canonical-paths discovery

Regenerating the SNARK fixtures nearly produced worthless files. The same tree gives THREE ids:

| built at | id |
|---|---|
| dev box `/home/…` | `1bed31ef…` |
| coordinator scratch `/root/hazync-rebuild` | `1112670d…` |
| container `/hazync-zkvm` | `dfc9eeda…` |

Only the third can produce a publishable proof, and nothing about the wrong ones looks wrong — the
build succeeds, the binary runs, the proofs verify against themselves. Fixed by staging the
coordinator checkout at `/hazync-zkvm`, where `HOME=/root` and `HAZYNC_BASE=/root/hazync-build`
already match. Written into the RUNBOOK with the check to run BEFORE proving anything.

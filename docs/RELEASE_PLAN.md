# Hazync — release plan

**Objective: ship a product that lets strangers push compute at the board, so the proof generation
completes by brute force.**

The bottlenecks are fixed. Hazync proves Bitcoin Core's real consensus code inside RISC0, so the cost
of a proof is Core's cost plus RISC0's overhead, and neither is ours to optimise. Prototypes against
both were measured and rejected (see `ACCELERATION.md`, and the ruled-out items in `GOALS.md` §G2).

That settles the strategy. We are not going to out-engineer the constraint, so the product's job is to
**recruit compute** — and everything below serves that, or serves the people who consume the result.

This document is the finishing plan. `GOALS.md` says what "done" means for the protocol; this says what
must be built and shipped to get there. Every task traces to a goal.

---

## The shipping gate

"Ready for compute" is a specific claim, not a feeling. It means all three of:

1. **A stranger can contribute in one command** — otherwise the fleet never grows, and fleet size is
   the only lever left on G2.
2. **Work already paid for cannot be lost** — otherwise money is spent twice.
3. **Nothing in flight can be retroactively invalidated** — a soundness bug or a guest re-baseline
   found at 40% of the chain costs the entire board.

Spending before (2) and (3) hold is spending at risk. Spending before (1) holds is spending on a fleet
of one.

---

## Workstream 1 — Proof party works out of the box

**The critical path.** No other workstream matters if contributors cannot join. This is the only thing
that converts strangers into throughput, and throughput is the only route to G2.

> **STATUS 2026-08-01 — Workstream 1 is COMPLETE and this section describes the v0.11.0 state it was
> written against.** The worker ships as a signed release artifact (1.1), `selftest` proves a real
> block before joining (1.3), failures name the variable to change (1.4), and the done-condition below
> was demonstrated end to end: a fresh user went from the release page to a verified proof credited on
> the public board, with no repo checkout and no help. Also complete since: **2.3** (`/api/proof/<id>`
> and `/api/spine/proof`), **3.x** (G3 met, #47), **6.3** (G1 retention gated in CI), **6.4** (#30,
> the spine), **6.5** (#40, the bridge). Left live in this plan: **6.1** (proof-store backups),
> **6.2** (accumulator audit), **6.6** (bridge checkpoints), and Workstream 4, which is ghostd work
> tracked in #42/#31/#46.

**Today a contributor cannot do this.** v0.11.0 ships four binaries — two host/prover, two verifier —
and **the worker is not among them**. `coordinator/hazync` is the claim → prove → submit loop, and it
exists only inside the repo. The documented path is therefore: clone, read `CONTRIBUTING.md`, install
CUDA, discover the `HAZYNC_*` environment variables, and point a script at a coordinator by hand. That
is a path for someone who already knows how it works.

| | task | notes |
|---|---|---|
| 1.1 | **Ship the worker as a release artifact** | Covered by the signed `SHA256SUMS`, like every other binary. Without this there is no onboarding to fix. |
| 1.2 | **One-command join** | `hazync-worker --coordinator <url>` → claims, proves, submits, repeats. Env vars become flags with defaults; no repo checkout required. |
| 1.3 | **Self-check before joining** | Prove one known block with a known-good answer and compare. Catches a bad CUDA install, a non-canonical guest, and insufficient VRAM *before* the contributor burns hours and submits garbage. |
| 1.4 | **Self-diagnosing failures** | OOM → name `HAZYNC_SEG_PO2` and the value to try. Wrong guest id → say the build was non-canonical and point at the container recipe. The host already classifies environment-vs-consensus failures (exit 2); extend that discipline to the worker. |
| 1.5 | **Resume after crash or reboot** | A claim must not be orphaned by a power cut. The worker already releases its claim on SIGTERM; it must also recover one it still holds on restart. |
| 1.6 | **State the hardware requirements plainly** | What GPU, how much VRAM, expected blocks/hour, expected cost. Peak VRAM is ~22.9 GB per process at `HAZYNC_SEG_PO2=21` and is driven by po2, **not** by inputs per chunk — contributors will otherwise size hardware against the wrong variable. |
| 1.7 | **A contributor quickstart** | Download → verify signature → self-check → join. One page, no prior knowledge assumed. |

**Done when:** someone who has never seen the repo goes from a release page to submitting a valid
proof, without reading source and without help.

---

## Workstream 2 — Tools and binaries

Users fall into three groups and today they are handed one undifferentiated asset list.

| | task | notes |
|---|---|---|
| 2.1 | **"Which binary do I need"** | Three paths: *verify a proof* → `hazync-verify`; *contribute compute* → `hazync-worker`; *run a node on a proof* → ghostd. Currently a reader must infer this from filenames. |
| 2.2 | **Keep every artifact signed** | Already true; must stay true as the worker and any WASM build are added. |
| 2.3 | **Publish proofs as a fetchable artifact** | The board links ranges to proofs, but there is no stable way to fetch "the proof for block N" for scripting or for a small device. |

---

## Workstream 3 — Small-device validation (G3)

**An API would defeat the point.** If a phone asks a server whether a proof is valid, the phone trusts
the server, and the entire trust argument collapses. The verifier is **1.7 MB**, needs only glibc 2.34
and has no libstdc++ dependency — small devices should verify **locally**. The right shape is to make
local verification effortless, not to centralise it.

| | task | notes |
|---|---|---|
| 3.1 | **Measure peak RSS and wall-clock on real aarch64** (#41) | The binding unknown. The published ARM64 binary has only ever run under `qemu-aarch64-static`, which says nothing about a RAM ceiling. If RSS is small, phone-class is real; if it is 4 GB, it is not. Everything else here depends on the answer. |
| 3.2 | **WASM build of the verifier** | A browser verifies a proof in-page — no install, no toolchain, works on any phone. This is the demonstration that makes utility self-evident to a sceptic, and it is the cheapest credibility in the plan. |
| 3.3 | **Serve the small artifact** | The SNARK wrap is **2,033 bytes**; the STARK receipt is ~224 KB. Small devices must be offered the former. Note the docs' "~200–300 B" refers to the proof, not the receipt (#21, #22). |
| 3.4 | **Record the evidence** | Real-hardware numbers into `prover/evidence/`, replacing the qemu run. Until then G3 is an inference. |

**Done when:** a proof verifies on a phone-class device, and the numbers are committed rather than
asserted.

---

## Workstream 4 — Ghost integration

Ghost is developed separately; this workstream is the Hazync side of the interface.

| | task | notes |
|---|---|---|
| 4.1 | **Native hazed binding** (G5, #31) | Today `prover/hazed-chain-verify.py` does this externally. It must be ghostd calling the existing C ABI (`verifier-ffi`), exposed as RPC, and it must hold at arbitrary heights rather than only 1..1000. A second implementation of the anchoring rules is a second place for them to be wrong. |
| 4.2 | **Instant IBD from a proof** (G4, #42) | Adopt the proof's committed UTXO set at height N and start at N+1. Emit a snapshot from the bridge, load it through Core's existing `assumeutxo` path, and verify it against the proof's Utreexo roots — **proven assumeutxo instead of trusted assumeutxo**, reusing machinery Core already ships. Needs a height with real transaction volume; blocks 1..1000 hold ~1,020 transactions and demonstrate nothing about speed. |
| 4.3 | **Archive-node challenge** | A hazed node proves its retained chain to a peer on demand. Falls out of 4.1 — identity from what the node keeps, validity from the proof. |

**Note on scope:** what exists today is *validate-with-elision* — the node still downloads and connects
every block, and merely skips script verification. That is demonstrated and sound, but it is not
"sync in seconds", and the distinction must not blur in the docs.

---

## Workstream 5 — Docs and onboarding

Reviewers judge the project by this surface before they judge the cryptography.

| | task | notes |
|---|---|---|
| 5.1 | **Walk the full path end to end** | Website → docs → download → verify signature → run. Fix what breaks. Repeat after every release; it has broken before. |
| 5.2 | **Three guides for three audiences** | Contributor (Workstream 1), verifier (Workstream 3), node operator (Workstream 4). |
| 5.3 | **The specification** (#36) | The single document a reviewer can be pointed at. Gates external review, which gates the audit. |
| 5.4 | **Keep the honest-gaps list current** | `GOALS.md` states measured status including what is *not* met. That candour is an asset with reviewers — do not let releases quietly outrun it. |

---

## Workstream 6 — Ops readiness (**gates spending**)

These are the items where being wrong costs money already spent.

| | task | why it gates spending |
|---|---|---|
| 6.1 | **Back up the proofs and bundles** | **8.3 GB of proofs (38,507 files) and 23 GB of bundles (182,537) exist on one disk.** `BACKUP_REMOTE_DB_ONLY=1` sends only the 38 MB ledger offsite — the offsite total is 114 MB. The proofs *are* the purchased product. The bundles are worse: regenerating them means re-walking the bridge at ~291 blocks/hr ≈ **26 days**. The current backup target has 14 GB free against a 31 GB payload, so this needs storage, not just a config change. |
| 6.2 | **Audit the accumulator** | The **only** failure mode that invalidates work retroactively. A soundness bug in `Forest`/`Stump` found at 40% of the chain costs the whole board and forces a re-baseline. Weeks of work, cannot be parallelised — so if it is happening it should start early and run alongside everything else. |
| 6.3 | **Gate per-block retention** (G1) | `CLAIM_WIDTH=1` is now live, so new claims produce per-block receipts. Nothing prevents regression, and blocks 38,500–39,299 already have none — they were proven at width 100 and their leaves discarded. Needs a check that fails when a proven height has no receipt, plus a decision on backfilling the 800. |
| 6.4 | **Incremental fold** (#30) | Fold `[1..N]` forward instead of re-folding from scratch as the board grows. Without it the fold cost recurs, and it grows with the board. Folding the current board is ~27 GPU-hours and ready to run. |
| 6.5 | **Fix the bridge** (#40) | Provers idle once the 143,238-block runway is exhausted, which at fleet scale is days. Buying GPUs against a 291 blocks/hr witness supply is buying idle cards. `Forest::prove` is O(subtree), not O(log n) — ~810,000 hashes to collect ~20 siblings, twice per input. Host-side only, so **no re-baseline**. |
| 6.6 | **Retain bridge checkpoints** | Cheap insurance. The witness format changed once already (v0.9.0). If it changes again, checkpoints make re-emission parallel instead of another 26-day serial walk. |

---

## Re-baseline: what must move together

The canonical guest is `dfc9eeda…` (BIP30 closed by a coinbase-only SMT #54, audit #3's 91842/91880
grandfather, BIP34Height read from Core), and the live board must serve it — the two cut over together.

This section was written when `85dc0b56…` was staged *ahead of* a board still serving `3f52baff`, and
it describes that hazard, which is the general one: while the two disagree, deploying either half
early makes the public surface reject the public board:

| artifact | state | deploy when |
|---|---|---|
| `hazync-verify.wasm` on bitcoinghost.org | built, parity-green, **held** | the board re-baselines |
| `hazync-verify` / `-aarch64` release binaries | rebuilt, **unreleased** | same |
| coordinator `hazync-host` | still `3f52baff` | same |
| the board itself | `3f52baff`, 39,299 proven | genesis restart on new hardware |

They are one atomic step, not four. A verifier pinned to `85dc0b56` refuses a `3f52baff` proof — that
is the cross-id isolation working, and it is exactly what a visitor would experience as "the site is
broken" if the verifier moves before the board does.

Verified held: the live `.wasm` still hashes to the old build, and `/hazync/api/meta` still reports
`3f52baff`.

Also expiring at the re-baseline: the **220,000 bundles** on the coordinator (the witness format is
unchanged but the leaf values are not — see #44), and the 38,507 receipts. Neither needs migrating;
both regenerate.

## Sequencing

```
6.1 backups ─────────┐
6.2 audit (start now,│ runs for weeks alongside everything)
                     ├──> SPENDING GATE ──> scale compute
1.x proof party ─────┘

3.x small-device   ─┐
4.x ghost          ─┤ parallel, none blocked by compute
5.x docs + spec    ─┘
6.3–6.6 ops        ─┘
```

- **6.1 and 6.2 gate spending.** 6.1 is procurement plus a script. 6.2 is weeks and unparallelisable,
  so it starts first even though it finishes last.
- **Workstream 1 gates whether spending works.** Compute bought against an unusable onboarding path
  buys a fleet of one.
- **Workstreams 3, 4, 5 need no compute and no bridge.** G3, G4 and G5 are all reachable without ever
  finishing the chain — they should not wait behind G2.
- **6.5 pairs with any serious fleet growth**, not before.

---

## Steady state after completion

Once the board reaches tip the ongoing obligation is small and permanent:

| | requirement | status |
|---|---|---|
| **Coordinator** | Serve claims, accept proofs, verify on receipt | Running; `VERIFY_MODE=real` |
| **Backup** | The proofs, offsite | **Not in place** — see 6.1 |
| **Tip prover** | Prove faster than blocks arrive | **Not met.** One block measured at 55 min of GPU against a 10-min interval — 5.5× too slow on one card, ~6 L40S-equivalents to hold position |
| **Tip bridge** | One witness per block within 10 min | **Unmeasured at tip-era input counts.** ~12.4 s/block at h≈182,000, but cost grows with inputs and it is single-threaded. A CPU problem GPUs do not solve |

Both tip requirements must hold **together**: a prover fleet fast enough to follow the tip is useless if
the bridge cannot feed it. See `GOALS.md` §G6.

---

## What this plan deliberately excludes

- **Optimising Core or RISC0.** Measured and rejected. Converting data formats to suit the guest is
  still touching consensus code adjacently, for gains too small to justify the risk.
- **Any change requiring a guest re-baseline**, unless taken deliberately and as a batch. A new
  `METHOD_ID` invalidates every existing proof and restarts the board. Host-side work (#40, the bridge,
  the coordinator, the worker) is free of this constraint — which is precisely why it is where the
  effort goes.
- **Proving anything but mainnet.** The guest compiles `CChainParams::Main()`.

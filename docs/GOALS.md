# Hazync — goals

Six technical goals. Every task (once inventoried in `history/ROADMAP.md`) is in service of one of these; if a task cannot be
traced to a goal here, it is not on the critical path.

Each goal states what it means, where it actually stands (**measured, not asserted**), and what would
count as done. Numbers are from committed evidence under `prover/evidence/`, not estimates, unless
marked otherwise.

---

## G1 — A zk proof per block

**Every block has its own receipt, permanently retained.**

Not an intermediate to be folded and deleted. A sceptic must be able to be handed one block and check
it alone, rather than being told "we proved a range".

**Status: achieved, and at risk.** A receipt exists and is served for each proven height, and a receipt
of a mid-chain range can be checked on its own. That check lives in CI rather than in a transcript
here: `prover/testdata/snark/neg500.snark` is a Groth16-wrapped mid-chain range, regenerated under the
current guest on 2026-09-07 (`prover/testdata/snark/README.md`), and `.github/workflows/adversarial.yml`
asserts that `hazync-verify` exits exactly `2` on it — a valid SNARK that is not genesis-anchored. That
is the verdict every per-block receipt above block 1 gets, and checking one alone is what G1 asks for.

(An earlier revision printed a transcript of this check "with the released v0.16.0 binaries". Its
guest-id line was later rewritten to each new id at re-baselines, so it stopped describing the run it
claimed to be; it was removed rather than relabelled.)

The count of retained receipts is not restated here, because it is a property of the coordinator's
proof store rather than of this document, and quoting it goes stale on every re-baseline.
`coordinator/check-retention.py` reports it against the ledger, and CI now proves that checker can
still fail (`coordinator/test-check-retention.sh`).

The risk is that folding deletes them. `coordinator/hazync` proved `range_{h}.hzk` per height and
discarded the leaves after folding, so at any claim width above 1 the per-block proofs were produced
and thrown away. Since v0.13.0 folding is a separate task over already-submitted receipts
(`hazync fold`), so the leaves it consumes are retained ones — but nothing prevents a future scheme
reintroducing the old behaviour, which is why the gate exists. Retention is a **storage decision, not
a compute one** — ~215 GB at tip.

**Done when:** per-block receipts are retained and served for every proven block, and this survives
whatever work-distribution scheme is in force (#37).

---

## G2 — A zk proof from genesis to tip

**One receipt attesting that every block from genesis to the chain tip is valid under Core consensus.**

**Status: mechanism proven, scale is the entire problem.** `fold_1000` is a genesis-anchored proof of
blocks 1..1000 in **3,441 bytes**, verified in CI and on ARM64. A genesis-anchored **spine** is also
live and extending — one receipt at `/api/spine`, downloadable and verifiable against the released
verifier with no node and no chain data.

No board figure is written here any more. Every one that has been has gone stale within days, usually
because a re-baseline reset the board rather than because the number moved — and a stale figure in a
goals document reads as a claim rather than as a snapshot. The
[live board](https://hazync.org/) is the only honest source, and `/api/state` is the
same numbers as JSON.

> **Re-baselines reset the board to genesis**, because receipts made against a superseded guest do not
> verify under the new one. The current guest is `37987b85`. Every id that has ever been canonical is
> in [`reproduce/LINEAGE.tsv`](../reproduce/LINEAGE.tsv) — one row per id, oldest first, with the date
> it became canonical, the commit that pinned it and the risc0 toolchain it was built with — and
> `reproduce/METHOD_ID` records why each was superseded.
>
> The rule that history established: **any guest edit that moves line numbers changes the id,
> including comments.**

That gap is the important part and it has been consistently understated. Measured on the **stock**
guest, before CORE:

| | measured |
|---|---|
| block 741,000 (post-taproot, 670 inputs) | **3,275 s = 55 min** of GPU time, 16 chunks |
| **block 962,000 (near tip, ~7,200 inputs)** | **17,340 s = 4.8 hours** of L40S time at po2 22 (2026-08-24) |
| blocks below the frontier | nearly empty — block 20,000 holds 19,023 UTXOs in total (re-confirmed 2026-08-01) |
| historical board rate | 2,220 blocks/hr — **measured on those empty blocks only** |

Extrapolating the 2,220 blocks/hr figure to the remaining 931,664 blocks gives ~17 GPU-days and is
wrong by orders of magnitude.

⚠ **The ~17 GPU-years figure that replaced it was also too low, by roughly an order of magnitude.** It
assumed a 10-minute average per block; a near-tip block measured on the stock guest on 2026-08-24 took
**289 minutes** of card time on its own.

**Cost the work by INPUTS, not blocks.** Early blocks are nearly empty, so a per-block average is
meaningless; script verification dominates and it scales with inputs.

| guest | block | card-seconds per input | source |
|---|---|---|---|
| stock | 962,000 (~7,200 inputs), L40S, po2 22 | **2.41** (17,340 s) — MEASURED | 2026-08-24 |
| **CORE** | 966,108 (8,562 prevouts), 8 × L40S, po2 21 | **~0.77** (5,075 chunk + ~1,500 aggregate card-s) — INFERRED | `docs/history/BENCH_8xL40S_2026-09-08.md` |

Bitcoin's history is somewhere around 1.8–3.0 billion inputs, an estimate. At CORE's ~0.77 card-seconds
per input that is roughly **44–73 L40S card-years**, against the stock guest's 138–229 — INFERRED from one
near-tip block. Per-input cost varies with script type and era, and the input count is itself a range,
so read the order of magnitude, not the digits. The stock guest's figures were also priced in money;
that has not been re-derived for CORE.

**Keeping up with the tip** at a bounded lag needs about **11 L40S** on that block, and a sub-10-minute
block about **13** (`TOPOLOGY_AND_SETTINGS.md` §1) — both INFERRED from an 8-card fleet that measured
15m13s. On rented RTX 4090s, block 966,256 has been proved end to end in **8.09 minutes on 26 cards**
and **9.07 minutes on 27** (runs 1 and 4,
`docs/history/MILESTONE_966256_RUN4_2026-09-10.md`).

⚠ **Name the framing, the card and the po2 with any fleet size.** Throughput (consecutive blocks
overlap, so a bounded lag) and latency (one block inside 600 s) are different questions; the stock
guest's ~29 here and `history/FLEET_SIZING.md`'s ~32 answered them for a guest that no longer ships. 24 GB cards
run the CUDA default po2 21 (peak 22,478 MiB on a 4090); only po2 22, which peaked at 40.6 GB on the
stock guest, is out of their reach.

Two consequences:

- **Guest performance carries enormous leverage.** At the CORE figure above, a 1% cycle saving is worth
  roughly half a card-year of backfill. `docs/history/ACCELERATION.md` records how the stock guest's
  levers were found. (⚠ The often-quoted 1.8–2.3% for `ECMULT_WINDOW_SIZE` is the **15 → 19** move
  against libsecp's default; beyond 19, window 21 measured −1.245% — see `TOPOLOGY_AND_SETTINGS.md`
  §4.1.)
- **This is a fleet problem, not a procurement problem.** It is why the proof party exists.

### The bridge WAS the binding constraint. It is not any more.

Provers consume witnesses; the archive bridge produces them, single-threaded. On 2026-07-30 it ran at
~291 blocks/hr, projecting **~881 days (2.4 years)** of serial walking to reach the tip — which made it
the critical path for G2 and G6 regardless of how many GPUs existed.

**Fixed, and measured on real blocks.** A/B over the same 100 blocks from the same production
checkpoint at h=182,310, binaries built from one tree differing only in the accumulator, with the
emitted bundles compared byte for byte:

| arm | per block | blocks/hr | |
|---|---|---|---|
| original | 12.455 s | 289 | matches the independently measured 291 — the harness checks out |
| cached internal nodes | 0.697 s | 5,163 | **17.9x** |
| + leaf-position index | 0.067 s | 53,492 | **185.1x** |

All three emit **byte-identical bundles**. That is the assertion that matters: a faster bridge that
emits different witnesses is a bug, and every proof built on them would fail against the guest.

Two things, and the second only became visible once the first was done:

- **`Forest` stored only leaves.** So a sibling at level `k` cost hashing the `2^k` leaves beneath it,
  and a proof cost `2^h - 1` hashes — the information-theoretic minimum *for that storage*. The walk
  was already optimal; the structure was the bug. `roots()` had the same disease and the bridge calls
  it twice per block. Caching internal nodes costs one extra hash per leaf added and n hashes of
  memory. This was **94.1%** of bridge time.
- **The coin-position linear scan**, previously and correctly *ruled out*: an A/B gave 291 vs 285
  blocks/hr because it was ~4% of a 12.4 s block. After the above, a `perf` profile put **71%** of what
  remained in it. Nothing about the scan changed — everything around it did. A component's worth is a
  fraction of the whole, so it moves whenever anything else does.

The rate still falls as blocks fatten — it tracks **inputs per block**, not accumulator size — so the
tip-era figure needs its own measurement. But the bridge no longer bounds the project, and the 881-day
projection is void.

**Done when:** a single genesis-anchored receipt covers block 1 to a current tip, and verifies against
the canonical `METHOD_ID`.

---

## G3 — Validate any proof on small compute

**A Raspberry-Pi-class machine can check a proof. No node, no peers, no chain data, no prover.**

**Status: met via WebAssembly; demonstrated in a browser on 2026-08-01.**

The binding unknown for this goal was always **memory** — whether a small device can hold what
verification needs. For the WASM build that is now measured, and the measurement transfers:

| | |
|---|---|
| peak linear memory | **1.9 MiB** (27 pages at instantiate, 30 after 20 consecutive verifies) |
| verify wall-clock | 21–46 ms (x86-64, `[1..8]` proof of 1,841 bytes) |
| download | **290,527 bytes gzipped** (1,063,570 raw) |
| imports | **none** — requires nothing from the host: no WASI, no JS callbacks, no threads |
| wasm version | 1 (MVP) — no post-MVP feature dependency |

**Why an x86 measurement settles a phone question here.** WebAssembly linear memory is
architecture-independent: the module grows the same 64 KiB pages on ARM as on x86. Native RSS is not
portable that way, which is precisely why the aarch64 binary could not answer this. A module with
zero imports, MVP-only features and a 1.9 MiB ceiling runs in any spec-compliant WASM runtime — which
every current browser is — and 1.9 MiB is below any plausible device ceiling.

Deployed at <https://hazync.org/verify/> and linked from the Proof Party page.

**Demonstrated (2026-08-01).** The operator confirmed the page working in a browser, and the
deployed module was independently checked: it is byte-identical to the signed release asset, imports
nothing, reports the canonical guest id, and — driven through the page's own `hazync-verify.js` —
returns `verified` for the live genesis-anchored spine (71 ms) and `not_anchored` for a mid-chain
segment (46 ms). Both verdicts correct; evidence in `prover/evidence/wasm_verifier_live.txt`.

The native `hazync-verify-aarch64` binary's peak RSS on real ARM silicon was never measured — it has only
run under `qemu-aarch64-static` — and #41, which asked for it, was closed on 2026-08-01 as superseded by
the WASM measurement above, which answers the memory ceiling portably.

**Done when** — met: the verifier runs in a browser with no host imports, and the deployed module was
checked against the signed release (`prover/evidence/wasm_verifier_live.txt`). A run on a physical
phone or Pi has not been recorded.

Related: the artifact a small device should fetch is the SNARK wrap, measured at **1,841 bytes** for
`[1..8]`, not the ~200–300 B quoted in older docs (#21, #22). CUDA Groth16 crashes (#20), so wrapping
is CPU-only.

---

## G4 — Rapid IBD: sync in seconds

**A node reaches height N from a proof, without downloading or validating blocks 1..N.**

**Status: MET as a mechanism, demonstrated end to end on mainnet. Not yet demonstrated at scale.**
Updated 2026-08-05.

A ghostd node has loaded a UTXO set it never validated, on the authority of a genesis-anchored proof,
and continued validating from the proven height. Verified against the real mainnet header chain:
adoption loads exactly the proven coin count and bases the chainstate on the proven tip; a coin inside
the proven range is present and the first coin past it is absent; background validation is disabled,
since re-downloading the chain below the base is the work the proof replaces; a restart with the proof
returns to the adopted chainstate; and a restart *without* it is refused, because the exemption is
re-derived from the proof on every start and never read back from disk as a settled fact.

Implementation: bitcoin-ghost/ghost#543, with the set-binding half in #101.

**What is not yet shown** is that an adopted chainstate is byte-identical to one built by validating
every block, at a height with real transaction volume. That needs a proof at 200k–400k and is a
question of GPU-seconds, not of code. Adoption itself needs only the base block in the node's
**headers** chain — not a synced chain — so it is demonstrable at any height.

What exists is two separate things, and they should not be confused:

*Validate-with-elision*, which is **not** this goal: the node still downloads and connects every
block and merely skips script verification. Demonstrated at height 1000 — 1000 blocks elided, UTXO set
byte-identical to full validation, 8/8 adversarial inputs refused.

*Proven assumeutxo*, which is the first half of this goal and now works end to end. A node can be
handed a UTXO set and establish that it is exactly what a proven chain produces, with **no
developer-chosen hash anywhere in the trust chain** — which is what separates this from Core's
`assumeutxo`, where the snapshot is checked against a hash the developers picked:

Run against a height-8 proof (`ghostd -hazyncproof=fold_8.snark -hazyncutxo=dump_h8.bin`), the node
reports the proof verified, genesis-anchored through height 8, and the UTXO dump matching the proven
set of 8 coins. (The transcript that stood here was recorded under an earlier guest, `4722cec8`, and
its id line was later rewritten at re-baselines; it was removed rather than left misattributed.)

Driven on real data: the dump is emitted by the archive bridge (`host dump-snapshot`) and checked by
rebuilding the accumulator and comparing its roots against the ones the proof commits to. A single
flipped byte in one coin's value is refused — *"UTXO SET DOES NOT MATCH THE PROOF — rebuilt
accumulator roots differ from the proven ones"*. `getblockchaininfo.hazync.utxodumpmatched` reports
the verdict on a running node.

Demonstrated at height 8, which proves the *mechanism* and nothing about the *saving*: blocks 1..1000
hold ~1,020 transactions in total, so a meaningful measurement needs a proof somewhere in 200k–400k,
and that is GPU time rather than engineering.

"Seconds" requires adopting the proof's **committed UTXO set** at height N and beginning at N+1. The
proof already carries everything needed: tip, cumulative work, UTXO roots and leaf count, and the
difficulty / median-time context (`hazync-verify --json`).

**This goal does not need G2.** Rapid IBD to a checkpoint height demonstrates the capability
completely. It does need a height with real transaction volume — blocks 1..1000 contain ~1,020
transactions, so there is no signature load to skip and no speed to measure.

**Done when:** a node syncs to a checkpoint height from a proof in seconds, and its chainstate is
byte-identical to a node that validated there conventionally.

---

## G5 — Validate hazed blocks against a proof

**A node holding only stripped blocks can establish that its chain is real and valid.**

Hazed blocks are never re-proven — they were proven valid when accepted. What a hazed node needs is to
*bind* what it retains to a proof that already exists.

**Status: mechanism achieved, demonstrated on real hazed storage.** Run against an actual archive
(ghostd v1.10.16, `-hazemode=hazed`, 1,255 stripped blocks in `gsb00000.dat`):

- txids read **from the hazed archive itself** — witnesses and scriptSigs permanently destroyed
- all 1,000 merkle roots recompute from those txids alone
- all 1,000 headers link, every PoW meets its target
- the hazed tip equals the tip the proof commits to

> A node holding ONLY hazed blocks established that blocks 1..1000 are the real chain, and a 3,441-byte
> proof established that every transaction in them was valid. No signature was available to check.

**Identity** comes from what the hazed node keeps; **validity** comes from the proof. A hazed node needs
no GPU.

**MET 2026-08-05.** Both conditions are now satisfied. ghostd performs the binding natively via the
`hazyncverifychain` RPC — `prover/hazed-chain-verify.py` is no longer the mechanism — and it runs at
whatever height the node's proof reaches rather than only 1..1000. An optional `from_height` allows a
cheaper suffix run, and the result records whether the whole chain or only a suffix was established,
so a partial check cannot be read as the stronger claim.

Everything is checked against the header the **archive** stores, not the one in the node's block
index: the index's linkage is structural, built from `hashPrevBlock` when the header was accepted, so
checking it against itself would establish nothing.

**It refuses when the proof commits to a different tip than the node holds**, and says the proof is
not about this chain — that refusal is the point of the check rather than an error path, and it is
made before the walk, so a proof about another chain costs one comparison rather than thousands of
merkle roots.

Implementation: bitcoin-ghost/ghost#627. Demonstrated on real mainnet blocks (8/8 from genesis, the
archive's tip equal to the proven tip) and against a real hazed mainnet node, whose stripped storage
is served with an empty coinbase scriptSig — the payload genuinely destroyed, not withheld.

---

## G6 — Tip-following

**Prove blocks faster than they arrive, sustained.**

Without this G2 is unreachable by definition: a genesis→tip proof is stale the moment it is made, and
the frontier never converges on a moving tip.

**Status: not met — sustained throughput above one block per 10 minutes has not been measured — but
sized.** On the CORE guest, on near-tip blocks:

| framing | fleet | label |
|---|---|---|
| one block inside 600 s | **~13 L40S** | INFERRED from 8 × L40S measuring 15m13s on block 966,108 |
| keep pace at a bounded lag | **~11 L40S** | INFERRED, ~6,570 card-seconds per block on 966,108 |
| one block on rented RTX 4090s | 26 cards **8.09 min**; 27 cards **9.07 min** | MEASURED on block 966,256 |

The derivations are in `TOPOLOGY_AND_SETTINGS.md` §1. The stock guest needed ~29 L40S on the same
throughput framing; that figure is superseded.

⇒ **The throughput figure is BREAK-EVEN.** It matches the chain's growth rate and **burns down none of
the backlog**. Proving history is a separate purchase on top of it — see "Two operating modes" below.

This constrains only the **proving fleet**. Nodes consuming proofs (G3, G4, G5) are unaffected.

**The bridge no longer caps this.** Tip-following needs a witness per block within 10 minutes. At
h~182,000 the bridge now produces one per **0.067 s** (was 12.4 s — see G2), so it clears the interval
by four orders of magnitude at this era. Its cost still grows with inputs and it is still
single-threaded, so the tip-era figure needs measuring — but the margin is no longer in question.

**Done when:** sustained proving throughput exceeds one block per 10 minutes across the fleet, measured
over a period long enough to include large blocks. The bridge half of this is now met with room to
spare at h~182,000 and needs re-checking at tip-era input counts.

---

## What is deliberately not a goal

- **Beating a full node on trust.** The claim is a *smaller* trust assumption than the status quo, not
  zero: Core already skips signature verification for most of the chain via `assumevalid`, on the
  authority of a developer-chosen hash. Hazync replaces that anchor with a proven one.
- **Proving anything other than mainnet.** The guest compiles `CChainParams::Main()`. A regtest or
  testnet proof requires a different guest and therefore a different image id.

## Two operating modes, and why they are sized differently

The project has **one goal and two workloads**, and they are not variants of each other. G2 is one;
G6 is the other. Quoting a fleet size, a deadline or a fidelity posture without saying which mode it
belongs to produces an argument rather than an answer.

| | **Backfill** (G2) | **Tip-following** (G6) |
|---|---|---|
| what it is | prove genesis → frontier | prove each new block as it arrives |
| the work | **~44–73 L40S card-years** on CORE, INFERRED (1.8–3.0bn inputs at ~0.77 card-s/input; 138–229 on the stock guest) | **~11 L40S continuously**, INFERRED |
| what matters | throughput per pound; calendar | keeping pace |
| per-block latency | **irrelevant** — the whole chain is queued | the only thing that matters, *eventually* |
| sized by | budget and how long you will wait | the block interval |

**The two add, they do not overlap.** ~11 cards holds position and closes nothing. Catching up is
whatever you buy on top — INFERRED from the CORE card-years above (G2), so no more precise than they are:

| L40S above break-even | time to backfill |
|---|---|
| +50 | ~0.9–1.5 years |
| +100 | ~0.4–0.7 years |

⚠ **Consequence for planning: tip latency is not a live question yet.** While backfilling you are by
definition nowhere near the tip, so "is this block proved within 600 s of appearing?" cannot be
answered until catch-up is in sight — which is years out at any plausible fleet. Until then the fleet
is sized by **throughput alone**, and a bounded lag behind the tip costs nothing that matters.
`TOPOLOGY_AND_SETTINGS.md` §1 carries the fleet arithmetic for both.

⚖ **And the two modes do not carry the same fidelity risk — which is the part that is easy to miss.**
Backfill proves a **closed, enumerable** set of blocks: every signature it will ever see already
exists and can be differential-tested exhaustively against libsecp. Tip proving cannot be, because
its inputs have not been written yet.

⇒ **An acceleration that is unacceptable at the tip may be perfectly testable for history.** Backfill
is where the card-years live and tip-following is ~11 cards, so that asymmetry points at taking a
fidelity trade on the expensive workload while keeping Core semantics on the cheap one. hazync#139
(bigint2 ECDSA, 13.78x per verify) was exactly such a decision. It is closed (2026-08-23), and v0.21.0
settled it for the shipped guest: CORE ships without #139, and the #139 middle path lives only in the
experimental Ghost build (`docs/BUILDS.md`).

## Dependencies between goals

```
G1 (per-block proofs) ──┬── G2 (genesis→tip) ── needs G6 to converge
                        └── G5 (hazed binding) ── needs G1's proofs to exist
G3 (small-compute verify) ── independent
G4 (rapid IBD) ── needs a proof at a useful height, NOT the tip
```

Only G2 depends on G6. G3, G4 and G5 are all reachable without ever finishing the chain.

**G2 and G6 sat behind the bridge** — single-threaded and ~2.4 years from tip. That is fixed: 185x on
real blocks with byte-identical output (see G2), so witness supply is no longer the critical path and
the constraint is back to GPU-seconds, which is what the proof party exists to gather.

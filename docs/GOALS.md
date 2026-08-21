# Hazync — goals

Six technical goals. Everything in `ROADMAP.md` is in service of one of these; if a task cannot be
traced to a goal here, it is not on the critical path.

Each goal states what it means, where it actually stands (**measured, not asserted**), and what would
count as done. Numbers are from committed evidence under `prover/evidence/`, not estimates, unless
marked otherwise.

---

## G1 — A zk proof per block

**Every block has its own receipt, permanently retained.**

Not an intermediate to be folded and deleted. A sceptic must be able to be handed one block and check
it alone, rather than being told "we proved a range".

**Status: achieved, and at risk.** A receipt exists and is served for each proven height. Verified on a
real artifact — a single-block receipt proved from a coordinator witness bundle, then Groth16-wrapped,
and checked with the released v0.16.0 binaries (2026-08-04):

```
$ hazync-verify neg500.snark
NOT A GENESIS-ANCHORED CHAIN PROOF

  The SNARK is VALID and was produced by guest b62d2a60.
  It proves blocks 500..500 — a mid-chain SEGMENT, not a chain from genesis.
$ host verify-any range_500.bin
RANGE-OK lo=500 hi=500 out_leaves=503 range_work=4295032833 anchored=no
```

(`hazync-verify` exits 2 here: the SNARK is valid but this is a mid-chain **segment**, not a
genesis-anchored proof. That is the expected result for a per-block receipt, and is what G1 asks for
— one block, checkable alone.)

The count of retained receipts is not restated here, because it is a property of the coordinator's
proof store rather than of this document, and quoting it goes stale on every re-baseline.
`coordinator/check-retention.py` reports it against the ledger, and CI now proves that checker can
still fail (`coordinator/test-check-retention.sh`).

The risk is that folding deletes them. `coordinator/hazync` proved `range_{h}.bin` per height and
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
[live board](https://bitcoinghost.org/hazync.html) is the only honest source, and `/api/state` is the
same numbers as JSON.

> **Re-baselines reset the board to genesis**, because receipts made against a superseded guest do not
> verify under the new one. The current guest `b62d2a60` was pinned on **2026-08-04** (audit #5,
> shipped in v0.17.0). It followed `b161735a` (v0.16.0, #88 — the id no longer depends on where the
> repo is checked out) and `dfc9eeda` (2026-08-03, BIP30 closed by a coinbase-only SMT, #54, with
> audit #3's 91842/91880 grandfather), which in turn followed `71790584`.
>
> Four resets in three days is the real cost of changing the guest at all. `reproduce/METHOD_ID`
> carries the full history and the rule it established: **any guest edit that moves line numbers
> changes the id, including comments.**

That gap is the important part and it has been consistently understated:

| | measured |
|---|---|
| block 741,000 (post-taproot, 670 inputs) | **3,275 s = 55 min** of GPU time, 16 chunks |
| blocks below the frontier | nearly empty — block 20,000 holds 19,023 UTXOs in total (re-confirmed 2026-08-01) |
| historical board rate | 2,220 blocks/hr — **measured on those empty blocks only** |

Extrapolating the 2,220 blocks/hr figure to the remaining 931,664 blocks gives ~17 GPU-days and is
wrong by orders of magnitude. At even a 10-minute average — generous, given 55 min for a *modest*
modern block — the remainder is **~17 GPU-years**.

Two consequences:

- **Guest performance carries enormous leverage.** At this scale the `ECMULT_WINDOW_SIZE` change that
  bought 1.8–2.3% is worth roughly four months of GPU time across the chain. `ACCELERATION.md` is not
  a nice-to-have.
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

Deployed at <https://bitcoinghost.org/hazync/verify/> and linked from the Proof Party page.

**Demonstrated (2026-08-01).** The operator confirmed the page working in a browser, and the
deployed module was independently checked: it is byte-identical to the signed release asset, imports
nothing, reports the canonical guest id, and — driven through the page's own `hazync-verify.js` —
returns `verified` for the live genesis-anchored spine (71 ms) and `not_anchored` for a mid-chain
segment (46 ms). Both verdicts correct; evidence in `prover/evidence/wasm_verifier_live.txt`.

Still open, and the weaker half of the same question: the native `hazync-verify-aarch64` binary's peak
RSS on real silicon (#41). It has only ever run under `qemu-aarch64-static`. The WASM path already
answers the memory ceiling, so this is confirmation rather than an unknown.

**Done when:** the verifier is shown running on a real small device — a browser on a phone is
sufficient and is now the cheapest route — with the result recorded in `prover/evidence/`.

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

```
$ ghostd -hazyncproof=fold_8.snark -hazyncutxo=dump_h8.bin
[hazync] proof VERIFIED against guest b62d2a60…
[hazync]   genesis-anchored through height 8
[hazync]   UTXO dump … MATCHES the proven set (8 coins)
```

Driven on real data: the dump is emitted by the archive bridge (`host dump-snapshot`) and checked by
rebuilding the accumulator and comparing its roots against the ones the proof commits to. A single
flipped byte in one coin's value is refused — *"UTXO SET DOES NOT MATCH THE PROOF — rebuilt
accumulator roots differ from the proven ones"*. `getblockchaininfo.hazync.utxodumpmatched` reports
the verdict on a running node.

**What is still missing is adoption itself:** nothing is loaded into a chainstate. The node verifies
the set and then validates every block from genesis anyway, so there is no speed to report yet. That
is deliberate — a change that alters how a node validates should not land before the verification
path it rests on has been reviewed.

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

**Status: not met, by a wide margin.** One block measured at **55 minutes** of GPU time against a
**10-minute** block interval — a single card is **5.5× too slow to stand still**. Roughly six
L40S-equivalents are needed to hold position before proving one block of history.

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

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

**Status: achieved, and at risk.** 38,499 per-block receipts exist. Verified on a live artifact:

```
$ hazync-verify proof_20000.bin
  The SNARK is VALID and was produced by guest 3f52baff
  It proves blocks 20000..20000
$ host verify-any proof_20000.bin
RANGE-OK lo=20000 hi=20000 out_leaves=19023 range_work=4295032833
```

The risk is that folding deletes them. `coordinator/hazync` proves `range_{h}.bin` per height and
discards the leaves after folding, so at any claim width above 1 the per-block proofs are produced and
thrown away. Retention is a **storage decision, not a compute one** — ~215 GB at tip.

**Done when:** per-block receipts are retained and served for every proven block, and this survives
whatever work-distribution scheme is in force (#37).

---

## G2 — A zk proof from genesis to tip

**One receipt attesting that every block from genesis to the chain tip is valid under Core consensus.**

**Status: mechanism proven, scale is the entire problem.** `fold_1000` is a genesis-anchored proof of
blocks 1..1000 in **3,441 bytes**, verified in CI and on ARM64. The board stands at 39,299 of 958,301
blocks — **4.1% of blocks, and well under 0.1% of the work.**

That gap is the important part and it has been consistently understated:

| | measured |
|---|---|
| block 741,000 (post-taproot, 670 inputs) | **3,275 s = 55 min** of GPU time, 16 chunks |
| blocks below 39,299 | nearly empty — block 20,000 holds 19,023 UTXOs in total |
| historical board rate | 2,220 blocks/hr — **measured on those empty blocks only** |

Extrapolating the 2,220 blocks/hr figure to the remaining 919,002 blocks gives ~17 GPU-days and is
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

**Status: built and published; NOT demonstrated on the target.** `hazync-verify` is **1.7 MB**, needs
only glibc 2.34, has no libstdc++ dependency, and both x86-64 and aarch64 builds ship in v0.11.0
covered by the signed `SHA256SUMS`.

The honest gap: `prover/evidence/verifier_aarch64.txt` records the ARM64 binary as run under
**`qemu-aarch64-static`**, cross-compiled on an x86-64 box. It has never executed on real ARM hardware.
Emulation says nothing about RAM ceiling, cache behaviour or wall-clock on a Pi — which is precisely
what this goal claims.

**Done when:** the aarch64 verifier is run on physical Pi-class hardware against a real proof, with
peak RSS and wall-clock recorded in `prover/evidence/`. Until then the claim is an inference.

Related: the artifact a small device should fetch is the SNARK wrap, measured at **2,033 bytes**, not
the ~200–300 B quoted in the docs (#21, #22). CUDA Groth16 currently crashes (#20), so wrapping is
CPU-only at 825 s.

---

## G4 — Rapid IBD: sync in seconds

**A node reaches height N from a proof, without downloading or validating blocks 1..N.**

**Status: not started.** What exists is *validate-with-elision*, which is a different thing: the node
still downloads and connects every block and merely skips script verification. Demonstrated at height
1000 — 1000 blocks elided, UTXO set byte-identical to full validation, 8/8 adversarial inputs refused.

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

**Done when:** ghostd performs this binding natively — today it is `prover/hazed-chain-verify.py`, run
externally — and it holds at arbitrary heights rather than only 1..1000.

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

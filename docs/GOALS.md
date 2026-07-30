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

### The binding constraint is the BRIDGE, not GPU supply

Measured on the coordinator, 2026-07-30. Provers consume witnesses; the archive bridge produces them,
and it is **one single-threaded process on one core** of a 16-core box — 94.8% of one CPU, load 1.21,
zero iowait. No amount of GPU touches it.

| | |
|---|---|
| bundles emitted | 182,537 (22 GB) |
| proving frontier | 39,299 |
| runway ahead of provers | 143,238 blocks |
| rate at h~182,000 | **~291 blocks/hr** (A/B: 100 blocks in 1,239 s) |
| **projected serial time to tip** | **~881 days (2.4 years)** |

The rate falls as blocks fatten — measured 950 → 254 blocks/hr across 176,310→182,310 while the UTXO
count moved only 3%, so it tracks **inputs per block**, not accumulator size. Projection above uses real
per-era input counts (600 at h=250k rising to 4,200 at tip).

Today's 143k-block runway hides this. At any serious fleet size it is exhausted in days, after which
provers idle waiting for witnesses.

**Known candidate, not yet confirmed:** `Forest::prove` is **O(subtree), not O(log n)** — it copies the
containing subtree and recomputes every parent hash to collect ~20 siblings, and is called twice per
input. At 1.6M leaves that is ~810,000 hashes per proof where ~20 would do. A benchmark with an even
probe distribution put this at 33 s/block against a real 12.4 s block, i.e. the distribution is
pessimistic — but a pessimistic estimate exceeding the whole block time means it is certainly a major
component. Confirming the real share needs a profile, not another microbenchmark.

**Ruled out:** the coin-position lookup. It IS a full linear scan (O(inputs x leaves), ~22 GB scanned
per block), and indexing it makes no difference — A/B on 100 identical blocks gave 291 vs 285
blocks/hr. The scan is ~4% of block time. Recorded because the microbenchmark said 2,366x and that was
measuring something off the critical path.

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

**The bridge caps this too.** Tip-following needs a witness per block within 10 minutes. At h~182,000
the bridge produces one per ~12.4 s, which is comfortable — but its cost grows with inputs, and it is
single-threaded. Whether it still clears 10 min/block at tip-era input counts is **unmeasured**, and it
is a CPU problem that adding GPUs does not solve. See G2.

**Done when:** sustained proving throughput exceeds one block per 10 minutes across the fleet, measured
over a period long enough to include large blocks — AND the bridge sustains one witness per block in
the same window.

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

**Both G2 and G6 sit behind the bridge**, which is single-threaded and ~2.4 years from tip at the
current rate. That is the critical path for anything requiring proofs at scale, and it is a CPU
problem, not a GPU one.

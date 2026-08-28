# Recommended topology and settings

**As of 2026-08-28.** One page for "what should we actually run, and why" — fleet shape, card, per-box
settings, guest build flags, provisioning. Everything else in `docs/` is an investigation; this is the
conclusion those investigations currently support.

⇒ **For the evidence behind these numbers — how the fleet question was answered, and what was got
wrong on the way — read `TEN_MINUTE_BLOCK.md`.** This page is deliberately short and states
conclusions; that one carries the measurements, the cross-checks and the post-mortems. If a number
here ever looks surprising, its working is there.

**Every row is labelled.** MEASURED means it exists in this repo's evidence. INFERRED means arithmetic
over measured inputs. UNKNOWN means nobody has measured it and the row says so. **A setting with no
label is a bug in this page.**

⚠ **Two rules this project keeps re-learning:**

1. **Never quote a fleet size without saying which framing and which po2 it assumes.** Latency and
   throughput differ by ~10% of the fleet; po2 22 and po2 20 differ by 1.42x.
2. **Measure before quoting.** Every superseded number in §8 was, at the time, someone's confident
   summary of a real measurement.

---

## 0.5 ✅ MEASURED 2026-08-28 — #139 proves out, and the aggregate distributes

Two GPU results move every number below.

| | result |
|---|---|
| **#139 bigint2, GPU proving wall** | **8.00x** (middle path) / **9.10x** (wholesale) — the coprocessor takes ~13%, not the win |
| **distributed aggregate, 2 workers** | **1.81x** — scenario (c), where 10 min is unreachable at any N, is **dead** |

Per-verify ECDSA drops **1,723,407 → 140,044 cycles (12.31x)**, and block 962,000 is only **1.8%
taproot by input**, so #139 accelerates essentially the whole block.

⇒ **With #139, a near-tip block needs ~7-9 cards rather than 32.**

⛔ **But the aggregate does NOT scale past 2 workers, and that decides the fleet.**

The 16-chunk test settled it: an 8-wide join tree still saturates at N=2 (165 → 94 → **95 s**), and
the **remote** worker starves worse as workers are added (26% → 36% idle) while the local one does
not. **`seg_serve_cmd` is single-threaded and caps the aggregate at ~1.76x**, so it floors at
**897 s — above the whole 600 s budget at any fleet size.**

| fix | fleet |
|---|---|
| neither | ⛔ **IMPOSSIBLE at any N** |
| **`seg-serve` dispatch** (host-side, **no re-baseline**) | **7 cards** |
| aggregate witness read (§7.5, guest — rides the #139 re-baseline) | 8-15 cards |
| **both** | **5-6 cards** |

⇒ **Do `seg-serve` first.** It is host code: no `METHOD_ID` move, no board reset, no waiting on the
fidelity decision — and it alone takes the target from unreachable to 7 cards.

⚠ **hazync#190 must land too**, or the post-#139 straggler goes to **2.45x** and roughly halves the
win.

### ⚖ LEANING (not decided) 2026-08-28: the MIDDLE path over wholesale

The wholesale arm is 15% faster (9.10x vs 8.00x) but buys **at most one card**, and only in the
pessimistic aggregate case:

| | aggregate 767 s | aggregate 497 s |
|---|---|---|
| middle path | **6 cards** | 5 cards |
| wholesale | 5 cards | 5 cards |

⚠ **This is a leaning, not a decision** — the operator has explicitly not committed. Do not treat it
as settled, and do not let downstream work assume it.

⇒ **One card looks like a cheap price for keeping Core's ECDSA logic** — DER parsing, low-S handling, the r/s
checks, the inversion and the final `r == x(R) mod n` comparison all stay libsecp's literal code, and
only the group arithmetic moves.

⇒ **If that leaning holds, it would retire `HELIX_DUAL_BACKEND.md`.** Helix exists to run wholesale for backfill and
Core at the tip. If the middle path runs **everywhere**, there is no second backend — no height gate,
no committed cutover constant, no doubled audit surface, and none of the silent-divergence risk of two
implementations disagreeing across a cutover.

⚠ The middle path is **not** zero-surface: `double_scalar_mul` is Shamir's trick, so it does not
preserve libsecp's wNAF/GLV. Much smaller than wholesale, not nil — differential testing across chain
history remains the load-bearing work.

→ `TEN_MINUTE_BLOCK.md` §8.14 for the full curve and the diagnostic.

## 1. Fleet size — first decide which question you are answering

The two framings are not variants of one number. They have different answers, different risks, and
different open blockers.

| framing | what it means | fleet | status |
|---|---|---|---|
| **Throughput (bounded lag)** | keep up with the chain, always ~N blocks behind | **~29 L40S** | INFERRED from measured card-seconds |
| **Latency (a), aggregate distributes** | one block, tip to receipt, inside 600 s | **~32 L40S** | INFERRED — **assumes an unexercised claim** |
| **Latency (b), only segments distribute** | as above, resolution stays serial | **~48 L40S** | INFERRED |
| **Latency (c), nothing distributes** | as above | **fails at any size** (34 min at 32) | INFERRED |

**The arithmetic**, on measured block-962,000 figures — 14,926 chunk card-seconds, 1.05x straggler,
1,575 s aggregate (1,379 s segment proving + 196 s resolution):

```
per block, all in:   14,926 x 1.05  +  1,575   =  17,247 card-seconds
throughput:          17,247 / 600                =  28.7   =>  ~29 cards
latency (a):         14,926 x 1.05 / 32 + 1,575 / 32  =  539 s  =  9.0 min
```

### Recommendation: ~29 cards on the throughput framing, unless the product needs tip latency

⇒ It is the cheapest fleet, and — more importantly — **it is the only one whose card count does not
depend on an unexercised claim.** Under bounded lag the aggregate never has to distribute across
cards, because concurrency comes from running *different blocks* at once: `prove_chunk` takes no
previous receipt, and per-block aggregates are independent of one another. Only the **chain fold**
carries `add_assumption(prev)`.

⇒ **Cost: lag, and only lag.** Splitting 29 cards by workload share (chunks are 90.9% of card-seconds)
gives ~26 on chunks and ~3 on aggregates: ~603 s of chunk time plus a serial ~1,575 s aggregate,
so **≈36 min — about 3.5 blocks behind the tip** — in exchange for one block per 10 min.

⛔ **The one term that is NOT priced: the chain fold.** It is the actual sequential step and its
per-block cost has never been measured. If it exceeds 600 s the throughput framing fails at any fleet
size. It is small by construction — one join against the previous receipt, not a re-proof — but that
is a construction argument, not a measurement. **Measure it on the next box that is up.**

⚠ If you take the latency framing instead, the **distributed-aggregate check (#153/#157/#161) becomes
the blocking measurement**, and it needs >= 2 boxes. It is what separates 32 from 48 from "cannot get
there".

---

## 2. The card: L40S

| card | verdict | evidence |
|---|---|---|
| **L40S 46 GB** | ✅ **use this** | the baseline every other card is measured against |
| H100 80 GB | ✗ **0.95x** | 3.9x the memory bandwidth returned *less* throughput |
| B200 | ✗ **0.91x** | ~8.7% slower at ~3x the power, even after native `sm_100` removed the JIT |
| L4 | ✗ **37% more expensive per proof** | assembly scales with segment count; the VRAM ladder hides it |
| 4090 / 3090 (24 GB) | ✗ for po2 22 | a full chunk peaks at **40.6 GB**; they must drop to po2 20, measured **1.42x** slower |

**Three architectures within 9% across a 4x bandwidth difference is a fact about our kernels, not about
cards.** The card axis is closed — do not re-open it without a new kernel result.

---

## 3. Per-box settings

| setting | value | label | why |
|---|---|---|---|
| `HAZYNC_SEG_PO2` | **22** | MEASURED | the CUDA default; peaks ~40.6 GB, fits 46 GB at 88% |
| GPU concurrency | **1** | MEASURED | rejected **three times** at 0.95-1.03x, including on an H100 with 47 GB of 80 free |
| worker processes / card | 1 today; ceiling **≤1.09x** | MEASURED | the "1.20x" ceiling was corrected downward once the GPU was measured at 91.5% busy |
| disk per segment work dir | ~**1.6 GB** | MEASURED | boxes have run tight; watch `df` |
| po2 23 | **do not** | MEASURED | needs ~79 GB (B200-only) **and** two code changes — see §7 |

⚠ **`nvidia-smi utilization.gpu` is kernel RESIDENCY, not useful work.** It read 100% at concurrency 1
and 2 alike while throughput *fell*. Never tune from it.

⛔ **The GPU is ~91.5% busy, not 65% idle.** The older "65% idle" framing came from `vmstat` showing
the *host* single-threaded, which is a different claim. Anything budgeting for "filling the idle" is
sized against a gap that is not there.

---

## 4. Guest build settings

⛔ **Every row here moves `METHOD_ID`, and a guest re-baseline resets the board to genesis.** They are
individually small and collectively worth taking — **batch them all onto the next change that is
already paying for a re-baseline.** Do not land any of them alone.

| setting | shipped | best known | gain | label |
|---|---|---|---|---|
| C/C++ opt level | `-O2` | **`-O3`** | −0.264% | MEASURED |
| Rust `lto` | default | **`"fat"`** | −0.486% | MEASURED |
| Rust `codegen-units` | 16 | **1** | −0.361% | MEASURED |
| `NDEBUG` | absent | leave absent | −0.0018% | MEASURED — not worth the fidelity question |
| `ECMULT_WINDOW_SIZE` | 19 | **21** | **−1.245%** | MEASURED — see §4.1 |
| `ECMULT_GEN_KB` | 22 | **2** | 0% cycles, frees ~10/11 of the table | MEASURED |

The three codegen arms are **additive to within 0.001%** (naive sum −1.159%, combined arm −1.160%), and
the combined arm is gate-validated: byte-identical journal digest, `ChunkOut` unchanged, and the two
`METHOD_ID`s differ — proving both arms genuinely rebuilt rather than one answering from a stale binary.

**`ECMULT_GEN_KB` is inert** for this workload: 2, 22 and 86 give bit-identical cycles. It sizes the
`ecmult_gen` table for computing `k·G` when *signing*; verification uses `secp256k1_ecmult` against
`pre_g`, sized by `ECMULT_WINDOW_SIZE`, and Hazync only ever verifies. Set it to 2 and reclaim the
memory for free, during the same re-baseline.

### 4.1 `ECMULT_WINDOW_SIZE` — **21**, settled 2026-08-28

**MEASURED on block 140,000, 212 inputs** — the same workload, harness and metric TIER0 used, so the
arms are directly comparable:

| window | guest cycles | vs shipped 19 |
|---|---|---|
| **19 (shipped)** | 376,662,184 | — |
| 20 | 375,914,975 | −0.198% |
| **21** | **371,971,773** | **−1.245%** |

✅ **This run reproduces `TIER0_RESULTS_2026-08-26.md` exactly** — its control (376,662,184), its
window-20 figure (375,914,975) and its journal digest
`607f4a7e259b5570e0acbd74ff649ed5991f1552fef270faf03b3883e8f15fea` all match bit-for-bit, and the
digest is identical across all three arms here (`all_valid=1 binds=212`). The new arm is therefore a
clean extension of the existing sweep, not a separate experiment that happens to agree.

⇒ **Ship window 21.** It is worth **−1.245%**, roughly **6x** the −0.198% that window 20 was going to
buy, and it is the arm E4 specified and never ran.

### The "local bump at 20" story was a small-workload artefact

An earlier sweep on block 130,000 with **10 inputs** put window 20 *above* 19 (+0.29%) and read that as
a local bump that hill-climbing would get stuck behind. At 212 inputs there is **no bump** — the curve
falls monotonically 19 → 20 → 21. The bump was real at 10 inputs and irrelevant to production, where
chunks carry **64-180 inputs**.

⚠ **The transferable lesson is about workload size, not about windows.** Ten inputs is too little EC
work to amortise the `pre_g` table, so a small-workload sweep systematically under-rates larger
windows. Any guest-codegen arm measured on a toy block should be re-run at a realistic input count
before it is believed — in either direction.

### Consequences

- **The Tier 0 bundle roughly doubles.** TIER0's combined arm measured **−1.160%** using window 20.
  Swapping in 21 adds ~1.05 percentage points, for an estimated **~−2.2%** — subject to the additivity
  TIER0 demonstrated (naive sum −1.159% vs combined −1.160%), which has not been re-verified with 21 in
  the bundle. ⚠ **Re-run the combined arm before quoting −2.2% as measured.**
- At `GOALS.md`'s scale that is worth roughly **twice** what the bundle was worth — TIER0 priced 1.16%
  at ~€1,950 across the chain.
- ⚠ **The `pre_g` table doubles with each window step** (~16 MB at 19 → ~64 MB at 21). The cycle figure
  above is *net* of the paging that costs, so the win is real as measured — but the guest's memory
  footprint grows, and nobody has checked that against segment sizing. Check before landing.

⛔ **Do not land it alone.** It moves `METHOD_ID` like every other row in §4; it rides the next
re-baseline with the bundle.

⛔ **Windows ≤15 cannot be swept naively.** `build.rs` regenerates `precomputed_ecmult.c` only above
window 15; at ≤15 it reuses whatever table is on disk, which after any >15 arm is the wrong one.
⚠ **And any sweep mutates the shared source tree at `$HAZYNC_BASE`** — back up
`secp256k1/src/precomputed_ecmult.c` and restore it afterwards, or the canonical build inputs are left
carrying the last arm's table.

---

## 5. Provisioning

| setting | value | why |
|---|---|---|
| CUDA | **12.8** | stock images ship 13.2; RISC0 3.0.5's kernels do not build on 13.x |
| `SKIP_GROTH16=1` | on any profiling/benchmark box | `risc0-groth16 0.1.0` hangs on download and costs 3 x 300 s ≈ **15 min of paid GPU time**; only `host snark-wrap` needs it |
| split phases | `HAZYNC_PROVISION=deps` then `build` | requires the `CUDA_VER` fix — see §7 |

⚠ **Check the per-box evidence directory before commissioning a run.** A B200 was provisioned to
re-test po2 23 before anyone read `~/hazync-b200-evidence-2026-08-25/README.md`, which has the result
table at the top.

---

## 6. Chunking

| setting | value | label |
|---|---|---|
| chunks per block, `N` | **fan-out is free** — pick N for latency, not cost | MEASURED |
| packer | cost-packing worth **1.18x on the slowest chunk** for +0.06% total cycles | MEASURED |

**Fan-out is free.** N=4 vs N=8 on one box, one card, one binary, only N moving: the aggregate moved
**115.8 s → 116.6 s, +0.7%**, while chunk card-seconds stayed within 1% (1,511 vs 1,525). The
aggregate's cost is a function of the **block**, not of how many pieces it is split into. An earlier
`N^1.79` reading was an artefact — its two points changed the block as well as N, from ~670 inputs to
8,006 — and acting on it would have argued against buying cards, which was exactly backwards.

⚠ **The packer's straggler metric is computed from PREDICTIONS.** It prints 1.00x for a partition that
measures 1.059x. Do not read it as a measurement.

---

## 7. What is NOT settled

Ranked by what changes a decision, not by expected payoff.

1. **Latency or bounded lag?** No GPU, no code, no measurement — and it decides whether items 2 and 3
   matter. **Do this first.**
2. **The chain fold's per-block cost.** UNKNOWN. The only thing that could invalidate the bounded-lag
   framing outright. Minutes of work on any box.
3. **Does the aggregate distribute?** (#153/#157/#161.) UNKNOWN, needs >= 2 boxes. Decision-critical
   *only* under the latency framing.
4. **#139** — a fidelity decision, ~1.1x on chunks. Nobody has asked what the *smallest* concession
   worth the bar looks like; the board only prices the two variants that happen to exist.
5. **po2 23.** Needs **two** changes, not one: raise `DEFAULT_MAX_PO2`, **and** recompute and ship
   `allowed_control_root("poseidon2", 23)` — raising the cap alone changes the computed root away from
   the baked `ALLOWED_CONTROL_ROOT` and every proof fails verification. That is what the 2026-08-25
   B200 run hit. B200-only (~79 GB). Lowest priority: unquantified, paid, and gated on a compatibility
   decision nobody has taken.

**Closed — do not re-open without new evidence:** the card axis (§2); GPU concurrency (rejected 3x);
the CUDA kernel lever at the compiler level (5 arms, stock is best); coordinator egress (~18 Mbps
measured against a ~320 Mbps estimate); `NDEBUG`; C/C++ LTO (`rust-lld` cannot read GCC LTO bytecode);
a newer risc0.

---

## 8. Superseded numbers — do not quote these

| number | status |
|---|---|
| aggregate ">3,300 s" | ⛔ **STALE.** Measured 115.8-117.3 s at N=4 on block 741,000; 1,575 s on block 962,000 |
| "the aggregate is the binding constraint / impossible at any N" | ⛔ **FALSE.** The chunk side binds |
| `N^1.79` aggregate scaling | ⛔ **ARTEFACT.** The block changed, not N |
| "83 s floor, inferred from a two-worker run" | ⛔ Wrong in size **and shape** — it is the whole aggregate, and it is constant in N |
| "the GPU is 65% idle" | ⛔ **REFUTED.** 91.5% busy; the `vmstat` evidence showed the *host* single-threaded |
| "worker processes are worth 1.20x" | ⚠ ceiling corrected to **≤1.09x** |
| "the guest field-mul is ~50 instructions, 5x52 limbs" | ⛔ It is `field_10x26`, ~200-400 instructions |
| "14.34 G chunk cycles" (modelled) | ⚠ **measured 14.057 G**, +2.0% high |
| "nothing above N=2 has ever been run" | ⛔ N=4 and N=8 run 2026-08-27 |
| "the kernels have never been profiled" | ⛔ Profiled 2026-08-27 |

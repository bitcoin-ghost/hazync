# Performance investigation — 2026-08-26

A pass over the whole tree for speed, memory and cost, and a plan of experiments to settle each idea.

**Every number here is labelled.** MEASURED means it exists in this repo's evidence or was produced by a
command in it. FROM CODE means it was read out of the tree today and is a fact about configuration, not
about performance. ESTIMATE means arithmetic over measured inputs. UNKNOWN means nobody has measured it
and the experiment below is how to find out. Nothing here is a recommendation until its experiment runs
— this file exists because `ACCELERATION.md` spent three days advertising a lever that had already been
removed.

---

## 1. Where the money actually goes

The cost of the chain is GPU-hours, and GPU-hours decompose into exactly three factors:

```
cost  =  cycles proven  ×  cost per cycle  ×  waste multiplier
         ─────────────     ──────────────     ────────────────
         guest work        prover efficiency  work done twice,
         (§3)              (§4)               or not at all (§5)
```

Everything below is filed under whichever factor it moves. This matters because the three are not
interchangeable, and the board has historically conflated two of them:

- **Cycles** are the only factor that reduces *total compute*. A cycle removed is a cycle nobody ever
  pays for, on any card, forever.
- **Prover efficiency** converts the same cycles into fewer GPU-seconds. Ceiling is bounded — the GPU is
  already 65% busy, so perfect scheduling is worth at most ~1.5x.
- **Parallelism is not on this list.** More cards reduce *latency*, not GPU-hours. It is what makes a
  10-minute block reachable; it does not make the chain cheaper. Conflating the two is how "17
  GPU-years" and "under 10 minutes" ended up sounding like the same claim.

**The headline for anyone reading only this section:** the largest *unexplored* class is guest codegen
(§3.2), which has never been touched and can be measured for free. The largest remaining ACTIONABLE
lever is worker processes per card (§4.1).

⛔ **This section originally said the largest win was "already written and sitting unmerged", meaning
#136/#137. That was wrong — they were merged five days earlier. See §3.1.**

---

## 2. Ranked summary

| # | Finding | Factor | Size | Status | Cost to test |
|---|---|---|---|---|---|
| 3.1 | ~~#136/#137 payload encoding~~ — **MERGED in #142**, gain is BANKED | cycles | 2.00x MEASURED | ✅ shipped 2026-08-21 | — |
| 3.2 | Guest C/C++ compiled at `-O2`, **no LTO** | cycles | UNKNOWN | never tried | free (execute mode) |
| 3.3 | Guest Rust gets Cargo defaults: `lto=false`, `codegen-units=16` | cycles | UNKNOWN | never tried | free |
| 3.4 | `NDEBUG` never defined — 31 live `assert()`s in `interpreter.cpp` | cycles | UNKNOWN | never considered | free (but see fidelity) |
| 3.5 | `ECMULT_WINDOW_SIZE=19` chosen against paging, optimum unverified | cycles | UNKNOWN | one point sampled | free |
| 4.1 | Worker **processes** per card: 1.20x at one point, never swept | efficiency | ≥1.20x MEASURED | not swept | one card |
| 4.2 | Preflight serial section — upstream has a **locked** solution | efficiency | ≤1.39x contested | see `ACCELERATION.md` | one card |
| 5.1 | Coordinator egress may cap the fleet before the GPUs do | waste/scale | ESTIMATE | never costed | free to measure |
| 5.2 | `write_frame` emits 2 tiny packets before every payload | efficiency | small, FROM CODE | trivially fixable | free |

---

## 3. Cycles — the factor that actually makes it cheaper

### 3.1 #136/#137 are MERGED — a correction, not an opportunity

⛔ **They landed in `cf04525` (#142) on 2026-08-21T19:51:06Z**, and the guest was re-baselined to
`b62d2a60` for them. `read_slice` is in the guest at `main.rs:1606`; `verify_inputs_batch` is at
`verify_input.cpp:705`. **The 2.00x is banked, not available.**

The first draft of this document said the opposite -- "1.96x, already written, unmerged, the best trade
on the board" -- and ranked it as the single most important action. It was taken from
`ACCELERATION.md`'s section headed "Built and measured, not merged", which pointed at
`bench/135-on-main`, a branch that no longer exists on origin.

That is the same failure this document opens by warning about, made in the opposite direction and in
the same evening. It is recorded here rather than quietly edited out, because the interesting part is
not the error: it is that a stale heading survived five days, was read by someone who had spent the
night removing stale headings from the same file, and still won.

What they bought, MEASURED on block 741000 chunk 1:

| stage | chunk cycles | block |
|---|---|---|
| before | 221,538,730 | 2,923 M |
| + #136 `read_slice` | 109,113,751 (**2.03x**) | 1,706 M (**1.71x**) |
| + #137 group-by-tx | 86,495,630 (**2.56x**) | 1,458 M (**2.00x**) |

Both byte-identical: same journal digest, same `ChunkOut`, same binds.

**Why it still matters to read this section.** #136 found that `env::read` of the payload was **50.9%
of a chunk's entire cycle count before any Bitcoin logic ran** -- serde walking risc0's word stream one
byte at a time at ~147 cycles per byte. #137 found the payload shipping the spending transaction once
per INPUT rather than once per transaction, a factor of 56.5 in bytes on block 741000. Neither was in
the consensus code at all. **Both were in the plumbing around it**, and both were invisible until
somebody profiled the read rather than the compute. That is the strongest available argument for
running the cheap experiments in §6: this system has form for hiding large wins in unglamorous places.

### 3.2 The guest's C and C++ are built at `-O2`, with no LTO

FROM CODE, `prover/methods/guest/build.rs`:

| what | flag | line |
|---|---|---|
| libsecp256k1 (`secp256k1.c` unity build) | `.opt_level(2)` | 127 |
| Bitcoin Core consensus TUs | `.opt_level(2)` | 158 |
| host-side ecmult table generator | `-O2` | 113 |
| the two own TUs (warning re-compile) | `-O2` | 222 |

There is **no LTO anywhere**, and secp and Core are two separate `cc::Build` invocations, so there is
no cross-archive inlining between them either.

This matters more than it would in ordinary software. ECDSA verification is **95.4%** of the block's
cycles (MEASURED, `ACCELERATION.md`), and of that **89.2%** is group arithmetic — all of it C inside
one libsecp translation unit. The dominant term in the whole system is the software modular multiply,
described in the board as a **1,141-instruction** sequence. `-O3` changes unrolling and scheduling
decisions on exactly that kind of code.

**Why nobody has looked:** the board's framing is "cycles come from *what* we prove", so every lever
considered so far changes the *source* (precompiles, payload encoding, window sizes). Codegen changes
the *instruction count for the same source* — which is the one axis that costs no fidelity at all.

⚠ Two real risks, both of which the experiment must check rather than assume:
- **`-O3` can expose latent UB.** The guest compiles with `warnings(false)` (i.e. `-w`), so UB has been
  shipping silently by construction. `-O3` is exactly where that surfaces.
- **This changes `METHOD_ID`.** Same source, different binary. So it must be batched with any other
  re-baselining change, not landed on its own.

### 3.3 The guest's Rust half gets Cargo's plain defaults

FROM CODE. `prover/methods/build.rs` calls `risc0_build::embed_methods()` with no options, and
risc0-build 3.0.5's `encode_rust_flags` sets only `lower-atomic` and the link address — verified by
reading `~/.cargo/registry/.../risc0-build-3.0.5/src/lib.rs:455`. It sets **no** `opt-level`, **no**
`lto`, **no** `codegen-units`. `prover/methods/guest/Cargo.toml` declares no `[profile]` either.

So the guest's Rust is built with `lto = false` and `codegen-units = 16`.

`codegen-units = 16` is the more interesting one: the guest is effectively a single large crate that
`#[path]`-includes `utreexo.rs`, `script_flags.rs` and `coinbase-smt/src/roots.rs`, and splitting that
into 16 codegen units blocks inlining across the split. This is a smaller lever than §3.2 — the Rust
half is the minority of cycles — but it is free to test in the same run.

### 3.4 `NDEBUG` is never defined, so Core's assertions are live in the guest

FROM CODE, and never discussed anywhere in the repo (`grep -rn NDEBUG` outside `vendor/` returns
nothing). The `cc` crate does not define `NDEBUG` on its own — confirmed by reading cc-1.4.4's source —
and `build.rs` never adds it. So `assert()` compiles in:

| TU | live `assert()` |
|---|---|
| `script/interpreter.cpp` | **31** |
| `pubkey.cpp` | **9** |
| `script/script.cpp` | 1 |
| `primitives/transaction.cpp` | 1 |

The first two are the hottest TUs in the guest.

⚠ **This one is not free, and I would not touch it without a decision.** Two reasons to be careful:

1. **Fidelity.** Hazync's claim is that it proves Core's real code. If Core ships with assertions live —
   and Core's own position is that assertions are part of consensus safety, not debug scaffolding — then
   compiling them out means the guest no longer executes what Core executes. That belongs in the
   "Core code no longer proven" column, not the free column.
2. **Soundness direction.** An assertion that fires makes the guest abort, and an aborted guest produces
   no proof. Remove it and the same input instead produces *a proof of a computation that violated an
   invariant Core relies on*. That is the wrong direction to trade in for a few percent.

So the experiment here is to **measure the cost first** and price it, exactly as the board prices
precompiles. If it is worth 0.5%, the question closes itself.

### 3.5 `ECMULT_WINDOW_SIZE = 19` has a paging tension nobody has tested

FROM CODE: `ECMULT_WINDOW = 19`, `ECMULT_GEN_KB = 22` (`build.rs:101,130`). MEASURED, from the board:
moving 15 → 19 bought only **−1.8% to −2.3%**.

That gain is suspiciously small. A four-step window increase should cut EC additions substantially, and
it barely moved. The likely reason is a genuine tension specific to the zkVM:

- A **bigger window** means fewer EC operations — the intended win.
- A bigger window also means a **much larger precomputed table**, and in RISC0 memory is paged. Touching
  more distinct pages costs cycles that a native build never pays. The board's own profile names
  `paged_map::PagedMap::insert` among the host-side costs, so paging is demonstrably not free here.

If paging is eating the win, then **19 may be past the optimum**, and a smaller window could be *both*
faster and lower-memory. Nobody has sampled the curve — only the two endpoints, and only in one
direction.

This is the highest-value-per-minute experiment in the document: it is a `#define`, it needs no GPU, and
it could plausibly move in the *unexpected* direction.

---

## 4. Prover efficiency — same cycles, fewer GPU-seconds

### 4.1 Worker processes per card: measured once, never swept

MEASURED (`prover_impl.rs:376`): running more worker **processes**, which do not share a CUDA context,
is worth **1.20x on one card** — against the ~6% the in-process pipeline delivered.

That is a single data point. Nobody has answered the obvious follow-up: **is 1.20x the ceiling, or the
first step of a curve?** The board's framing calls this "the zero-fidelity lever that remains", which
makes it strange that it has been sampled once.

The counter-evidence that must be handled: GPU concurrency was **rejected three times** at 0.95–1.03x,
including on an H100 with 47 GB of 80 GB free. But those tests ran concurrent **chunk proves** — each
one a full execute + segments + lift, peaking ~22 GB. `seg-connect` **segment** workers are much
lighter. The retraction in #183 flags precisely this as the unmeasured reconciliation. Settling it is
the single cheapest thing on this list.

### 4.2 Preflight — covered in full by #183

The serial section is risc0 preflight, the GPU is **65% idle waiting on one host thread**
(`vmstat`: 0.86 and 0.82 busy cores across H100 and B200 — doubling cores changed nothing).

Our in-process attempt deadlocked CUDA and was removed (#147, #148). Upstream **risc0#3201** does the
same overlap successfully in the actor worker — 800 ms → 400 ms at po2 20 — and records that **GPU
locking adjustments were required**, which is the piece we lacked. Full account in `ACCELERATION.md`
as amended by #183; not repeated here.

Priority: **below §4.1**, because §4.1 buys the same utilisation with no new failure mode.

---

## 5. Waste and scale

### 5.1 Coordinator egress — MEASURED, and it does NOT cap the fleet

✅ **RESOLVED 2026-08-26. The estimate below was wrong by ~20x and is kept only as the record of how.**

Measured on block 962,000 with no GPU at all — `seg-serve` runs `ExecutorImpl::run()` plus
`bincode::serialize` and prints its totals before it listens, so filing this as Tier 1 was a mistake:

| po2 | segments/chunk | MB/chunk | KB/segment |
|---|---|---|---|
| 20 | 844–913 | 118.7–135.8 | 141–149 |
| **22** | **202–218** | **55.3–62.0** | **273–284** |

**16 chunks ≈ 949 MB MEASURED, plus ~390 MB ESTIMATED for the aggregate ≈ 1.34 GB per block ≈ 18 Mbps**
sustained for a 600 s block. That caps nothing. Full working in `FLEET_SIZING.md` §6.

⛔ **The compression proposal below is therefore CLOSED.** It was conditional on egress binding. It does
not bind, and compression would trade coordinator CPU -- which IS the serial bottleneck -- for
bandwidth that is not scarce. Do not implement it.

The original estimate multiplied two errors: ~4 MB/segment inferred from the push-budget default (real
chunk segments are 273–284 KB; ~4 MB belongs to the AGGREGATE, whose segments carry chunk receipts as
assumptions, measured at 1.41 MB/segment even at po2 23), and 415 segments/chunk, which is the
**pre-#136/#137** count — those changes halved the cycles and so halved the segments to ~213.

<details><summary>The original (wrong) estimate, kept as the record</summary>

### 5.1-original Coordinator egress may cap the fleet before the GPUs do

This is the finding I would most like to be wrong about, because it constrains the "~28 cards gets a
tip block under 10 minutes" plan.

The coordinator executes the block and **pushes every segment** to a worker. Segments are not small:

- chunk 9 at po2 22 = **415 segments** (MEASURED)
- 16 chunks per block → **~6,600 segments per block** (ESTIMATE, cost-packed so roughly even)
- the push-budget code sizes itself against a `maxseg` of just under **4 MB** (FROM CODE, and the
  `4*1024*1024` default that clamped depth to 1 in #175 is direct evidence of that ceiling)

**ESTIMATE: up to ~26 GB of egress per block, from one machine.** To sustain that in a 600-second
window is **~40 MB/s ≈ 320 Mbps sustained**, plus receipts flowing back.

That is not obviously fatal — but it is uncosted, it lands on a single host, and it scales with fleet
size rather than being amortised by it. If the coordinator's link saturates at ~15 workers, the fleet
plan has a ceiling nobody has written down.

⚠ The 4 MB figure is a **maximum**, not a mean; the true average could be far lower. That is exactly
why this is an estimate with an experiment attached rather than a claim.

**And there is no compression anywhere on the wire** — `prover/host/Cargo.toml` carries no compression
dependency at all. Segment data is largely memory images, which typically compress well. If E5 shows
egress is a real ceiling, this is the obvious lever, and it trades coordinator CPU (which is *already*
the serial bottleneck, §4.2) against bandwidth — so it is not free and must be measured both ways.

</details>

### 5.2 `write_frame` sends two tiny packets ahead of every payload

FROM CODE, `prover/host/src/main.rs:4639`:

```rust
s.write_all(&idx.to_le_bytes())?;              // 4 bytes
s.write_all(&(body.len() as u32).to_le_bytes())?;  // 4 bytes
s.write_all(body)?;                            // up to ~4 MB
s.flush()
```

The stream has `set_nodelay(true)` (deliberately, and correctly — the comment explains Nagle would add
40 ms). But with Nagle off, those two 4-byte writes go out as **two separate packets** before the
payload. At ~6,600 segments per block that is ~13,000 needless packets per block, plus two extra
syscalls per frame. `read_frame` is likewise unbuffered.

Small, certain, and trivially fixed by building the 8-byte header into one buffer or using
`write_vectored`. Worth doing whenever this file is next touched; not worth a dedicated experiment.

---

## 6. The experiment plan

Ordered by **cost to run**, not by expected payoff — every Tier 0 experiment can run tonight on this
laptop with no GPU, and they collectively cover the entire cycles factor.

### Tier 0 — free, no GPU, execute mode only

All of these measure **cycles**, via `chunk-profile` with `HAZYNC_PROFILE_EXEC=1` (which the code
already documents as "the measurement #132 asks for before any GPU time is bought"), or
`HAZYNC_AGG_EXECUTE=1` for the aggregate. Both need only wall-clock and patience.

**Common protocol.** Fix block 962,000, `HAZYNC_CHUNKS=16`, cost-packed, one chunk (chunk 9) for the
inner loop and the full block for confirmation. Report **cycles**, not wall-clock — execute mode's wall
time is irrelevant and will mislead. Record `METHOD_ID` for every arm: any arm whose id does not move
has not actually rebuilt, which is the failure mode `build.rs` exists to prevent and which has bitten
this repo before.

| id | experiment | arms | decides |
|---|---|---|---|
| **E1** | C/C++ opt level | `-O2` (control) vs `-O3` vs `-O2 -flto` vs `-O3 -flto` | §3.2 — is codegen worth anything on the dominant term |
| **E2** | Rust guest profile | default vs `lto="fat"` vs `codegen-units=1` vs both | §3.3 |
| **E3** | `NDEBUG` | absent (control) vs `-DNDEBUG` | §3.4 — **price only**, no landing decision implied |
| **E4** | `ECMULT_WINDOW_SIZE` sweep | 15, 17, 18, **19** (control), 20, 21 | §3.5 — find the real optimum, including *below* 19 |

**E1 gate — mandatory.** `-O3` on `-w` code can activate latent UB. Before any `-O3` arm is believed,
it must pass the differential suite *and* reproduce a known-good block's journal digest bit-identically
against the `-O2` control. A cycle win that changes the digest is not a win, it is a bug. If the digest
moves, stop and treat it as a UB find, which is independently valuable.

**E4 note.** Sweep *downward* as well as up. The hypothesis is that 19 is past the optimum because of
paging, and a sweep that only goes up cannot falsify it. Record peak guest memory alongside cycles.

**E3 note.** This experiment produces a *price*, not a decision. If it is under ~1%, close §3.4
permanently and write down that it was measured — that is worth more than the cycles.

#### Harness validated on the laptop, 2026-08-26

Tier 0 was checked as runnable rather than assumed, because a plan whose experiments cannot execute is
worth much less than it looks.

| precondition | state |
|---|---|
| riscv cpp toolchain | `v2024.1.5-cpp` — **matches the pin** |
| rust toolchain | `v1.94.1-rust` — **matches the pin** |
| Bitcoin Core source | `v28.0` — **matches the pin** |
| secp256k1 source | `v0.5.1` — **matches the pin** |
| block witness | `prover/block_962000.json`, 4.3 MB, real |
| control build | `cargo build --release -j2`, **3m26s**, exit 0 |
| disk | 209 GB free — the 2026-08 wedge condition is absent |

⚠ **A local build yields a guest id that is NOT the canonical one, and that is EXPECTED — not a
defect, and not something to chase.** `reproduce/METHOD_ID` states the canonical id is produced only by
`docker build -f reproduce/Dockerfile .` at fixed `/root` paths; a local checkout builds at a different
absolute path, and hazync#88 explains why that alone moves the id (panic metadata).

The local id is deliberately not quoted here. `scripts/check-versions.sh` fails any doc that names a
guest id which is neither canonical nor listed in `reproduce/METHOD_ID`, and it is right to: a
one-off build id written into a document is indistinguishable, later, from a claim about what the
project ships. Read it with `host method-id` when you need it, and do not paste it anywhere.

What matters for these experiments is that **every pinned input matches**, so the guest SOURCE and
toolchain here are identical to the fleet's. A/B cycle comparisons taken on this laptop are therefore
valid and transfer; only the absolute id does not. Any arm must still be compared against a control
built on the same machine in the same session, never against a number carried from a box.

⚠ **Reading the id needs the `method-id` subcommand, not `strings`.** Scraping the first 64-hex string
out of the binary returns `000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f` — the
Bitcoin genesis hash — which looks exactly like a plausible id and is not one. Verified by control: a
nonsense subcommand prints no hex at all.

### Tier 1 — one card, hours

| id | experiment | arms | decides |
|---|---|---|---|
| **E5** | segment egress | instrument the coordinator: bytes/segment distribution, total per block, sustained MB/s at N workers | §5.1 — is there a fleet ceiling |
| **E6** | **worker processes × po2 (2D)** | processes 1,2,3,4 × po2 20,21,22 — every cell, VRAM recorded | §4.1 — and whether po2 22 admits a second process AT ALL |

#### E6 must be TWO-DIMENSIONAL — the constraint is VRAM, not scheduling

⛔ **Correction to this section's original framing.** It specified a 1-D sweep of processes at fixed
po2. That cannot answer the question, because processes and po2 trade directly against each other
through VRAM:

**L40S at po2 22 peaks at ~41 GB of a 46 GB card — 89%.** There is very likely no room for a second
worker process at po2 22 at all. So "just run more processes" may be capped at **N=1** on the exact
card the fleet would be built from.

And the removal note carries a detail that was skimmed: removing the in-process pipeline *"also
unblocked po2 22, worth **1.15x on chunks and 1.42x on the aggregate**"*. So po2 22 is worth more than
either scheduling fix, and anything that costs po2 22 to gain parallelism is probably a bad trade.

| approach | VRAM | keeps po2 22? | fills the 65% idle? |
|---|---|---|---|
| 1 process, po2 22 | 41 GB (89%) | ✓ | ✗ |
| N processes, po2 20 | ~11–14 GB each | ✗ (loses 1.15–1.42x) | ✓ |
| in-process pipelining | one allocation | it BLOCKED po2 22 last time | partially |

⚠ **The 1.20x figure has no recorded conditions.** Neither `ACCELERATION.md` nor the removal note in
`prover_impl.rs` says at what po2 or on which card it was measured. If it was taken at po2 20 it is not
comparable to a po2 22 baseline, and the "1.20x beats the pipeline's ~6%" comparison may be between
different workloads. **Record po2, card and peak VRAM for every cell of E6**, including cells that OOM
— the failures are the data that distinguishes "contention" from "memory".

**Why this now gates E9.** If the throughput optimum is 3 processes at po2 20, processes already fill
the idle time and E9 is dead. If it is 1 process at po2 22 — VRAM-bound, GPU still 65% idle — then
pipelining is the ONLY thing that can fill that idle time, because it shares one context and one
allocation. E9's value is entirely conditional on which, and E6 is cheap.

### E10 — a newer risc0: CLOSED, there is nothing to upgrade to

Checked 2026-08-26 rather than assumed. The result is negative and worth recording so nobody re-asks.

| | |
|---|---|
| pinned | `risc0-zkvm =3.0.5` (circuit crates 4.0.x, `risc0-sys` 1.5.0) |
| latest stable | **3.0.6** |
| 3.0.6's entire content | *"Repair Rust 1.97.0 guest build with `heap-embedded-alloc` feature"* |
| do we use `heap-embedded-alloc`? | **No** — guest features are `['std','unstable']` |
| do we use Rust 1.97? | **No** — pinned at **1.94.1** |

So **3.0.6 is a literal no-op for this build**: it fixes a feature we do not enable, on a toolchain we
do not use.

`5.0.0-rc.1` looks newer by version number and is not. The crates.io publish order is
`… 3.0.4, 5.0.0-rc.1, 3.0.5, 3.0.6` — **the RC predates our own pin** and the 3.0.x line superseded it.

⛔ **And upstream is dormant.** Five commits in three months, all housekeeping (Rust 1.97 bump, docker
image, sccache, Metal skip). risc0#3781 — the CUDA `u32` overflow fix that blocks po2 23 — is **still
open and unmerged**. Our own risc0#3798 and #3799 have **zero comments**.

**Three consequences, and they reverse earlier advice in this repo:**

1. **#182's hand-vendored #3773 fix is permanent, not a stopgap.** Nobody is going to merge #3781.
2. **The vendor policy's premise is gone.** `vendor/risc0-zkvm` is kept verbatim "so the diff stays
   re-appliable when risc0 moves". It is not moving. Divergence costs less than that policy assumes.
3. **E9 has no upstream route.** Adopting risc0#3201's GPU locking would be a fork, not an upgrade —
   which raises its cost and makes E6 settling the question first more valuable, not less.

**E6 is the highest-value Tier 1 experiment** and should run first. It is pure configuration, it
directly tests the lever the board calls the remaining zero-fidelity one, and its result determines
whether §4.2 (which carries real deadlock risk) is worth attempting at all. Watch peak VRAM per arm —
the 22 GB-per-chunk-prove figure is what killed concurrency three times, and the hypothesis is that
segment workers are light enough to escape it. **Record VRAM even on arms that fail**; that is the data
which distinguishes "contention" from "memory" and closes the retraction in #183.

### Tier 2 — needs the B200, and already queued for tomorrow

| id | experiment | decides |
|---|---|---|
| **E7** | po2 23 with the #3773 patch (#182) | whether po2 23 is reachable; it currently produces **invalid proofs** |
| **E8** | po2 22 on a newer driver | whether the B200's 8.7% deficit is software maturity, not silicon |
| **E9** | preflight overlap with a GPU lock, per risc0#3201 | §4.2 — only if E6 shows worker processes have plateaued |

**E9 is explicitly gated on E6.** If E6 reaches 1.4x with four processes, E9 is not worth the deadlock
risk and §4.2 can be closed by measurement instead of by argument.

---

## 7. What is already closed — do not re-propose

Recorded so this file does not become the next stale document.

| Closed | Why | Evidence |
|---|---|---|
| A faster card | **0.95x** — 3.9x HBM bandwidth bought nothing; three architectures agree | `ACCELERATION.md` |
| Bigger card (B200) | 8.7% *slower* than L40S, even with native `sm_100` | #178, #180 |
| Cheap-card tier (L4) | **37% more expensive** per proof | #180 |
| In-process preflight pipelining | **Deadlocked CUDA**, removed | #147, #148, #183 |
| GPU concurrency (chunk proves) | Rejected 3x at 0.95–1.03x — but see §4.1, the *segment*-worker case is untested | `ACCELERATION.md` |
| `sys_bigint` field backend | 1.67x for ~69% fidelity — bad trade | #129, #130 |
| Host-side tree-fold | Not the serial section; a profile names preflight | `ACCELERATION.md` |
| po2 23 | Produces **invalid proofs** (risc0#3773), not a config cap | #180, #182 |

---

## 8. If only three things get done

1. **Run E6** (§4.1). 1.2–1.45x, one card, a few hours, pure config — the largest actionable lever now
   that #136/#137 are banked, and it gates whether the deadlock-prone preflight work is worth touching.
2. **Run E4** (§3.5). Free, and the only one likely to produce a surprise: the 15 → 19 window move
   bought far too little, and the sweep must go DOWNWARD as well as up to find out why.
3. **Run E1, E2 and E3 together** (§3.2–§3.4). Free, one rebuild each, and they close the entire
   untouched codegen axis — including permanently retiring the `NDEBUG` question.

E5 (§5.1) is the one to run before promising anyone a 28-card fleet.

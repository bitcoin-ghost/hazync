# Tier 0 results — the guest codegen axis, measured

Answers E1–E4 of `PERF_INVESTIGATION_2026-08-26.md`. Every arm was rebuilt from source and measured on
the same machine in the same session.

**Headline: `-1.160%` is available, validated, and costs no fidelity. Two of the four questions close
as "the current setting is already correct", which is the more useful outcome.**

⛔ **This is a finding, NOT a recommendation to land it today.** See §5 — the change moves `METHOD_ID`,
and a re-baseline right now would reset a board that a first external contributor has just started
adding to.

## 1. Method

| | |
|---|---|
| workload | block 140,000, 212 inputs, `HAZYNC_CHUNKS=1`, execute mode |
| why that block | 2011, so almost entirely P2PKH ECDSA — it exercises the libsecp path every arm targets |
| metric | **exact cycle counts**, not wall-clock — deterministic integers, so a 0.2% difference is signal, not noise |
| per-arm cost | ~19 s measurement, 2–4 min build |
| control | `-O2`, no LTO, `ECMULT_WINDOW_SIZE=19`, no `NDEBUG` (i.e. `main`) = **376,662,184 cycles** |

Two guards, both from failures earlier the same day:

- **Every arm's `METHOD_ID` was recorded and compared to the previous arm's.** A rebuild that silently
  does not happen returns identical cycles and reads as "this change had no effect" — a false negative
  indistinguishable from a real result. Every arm below moved the id.
- **`$HAZYNC_BASE` was backed up and restored.** Changing the window regenerates a 38 MB
  `precomputed_ecmult.c` in the shared source tree, and since the table compiles into the ELF that
  would change `METHOD_ID` *even at window 19* — quietly corrupting the canonical build inputs.

## 2. Results

| arm | cycles | delta | % |
|---|---|---|---|
| **control** (`-O2`, w19) | 376,662,184 | — | — |
| E1 `-O3` | 375,666,655 | −995,529 | **−0.264%** |
| E1 `-O2 -flto` | — | — | **INFEASIBLE**, see §3 |
| E1 `-O3 -flto` | — | — | **INFEASIBLE**, see §3 |
| E2 rust `lto="fat"` | 374,830,719 | −1,831,465 | −0.486% |
| E2 rust `codegen-units=1` | 375,304,245 | −1,357,939 | −0.361% |
| **E2 rust both** | 374,038,635 | −2,623,549 | **−0.697%** |
| **E3 `-DNDEBUG`** | 376,655,352 | **−6,832** | **−0.0018%** |
| E4 window 15 | 386,224,584 | +9,562,400 | **+2.539%** |
| E4 window 17 | 381,425,808 | +4,763,624 | +1.265% |
| E4 window 18 | 377,046,090 | +383,906 | +0.102% |
| E4 window 20 | 375,914,975 | −747,209 | **−0.198%** |
| **COMBINED** | **372,293,302** | **−4,368,882** | **−1.160%** |

**The parts are additive to within 0.001%** — the naive sum of the three best independent arms is
−1.159% and the combined arm measured −1.160%. C codegen, Rust codegen and the ECMULT table do not
overlap.

### The gate: the combined arm produces an identical proof

A cycle win that changes the output is a bug wearing a win's clothing, and the guest compiles with
`-w`, so `-O3` is precisely where latent UB would surface. Both arms were re-run capturing the journal
digest `chunk-profile` prints for exactly this purpose (added by #136):

```
control   digest=607f4a7e259b5570e0acbd74ff649ed5991f1552fef270faf03b3883e8f15fea kind=0xc4a10004 binds=212
combined  digest=607f4a7e259b5570e0acbd74ff649ed5991f1552fef270faf03b3883e8f15fea kind=0xc4a10004 binds=212
```

Byte-identical journal, identical `ChunkOut`, and the two `METHOD_ID`s differ (`916cde9e` vs
`19d181a3`) proving both genuinely rebuilt. **No UB surfaced.**

## 3. E1: `-O3` is worth 0.26%, and LTO is not merely unmeasured but impossible

`-O3` moving only 0.26% is consistent with what libsecp is: hand-optimised C whose inner loops are
already unrolled by hand, on a target (rv32im) with **no vector unit**, which is most of what `-O3`
adds over `-O2`.

⛔ **C/C++ LTO cannot work with the current guest link, at all.** GCC's `-flto` emits LTO bytecode into
the `.o` files, and `rust-lld` — which links the guest — cannot read it:

```
rust-lld: error: undefined symbol: tx_full_sigops
```

Making it work needs a GCC-driven final link with the LTO plugin, or `-ffat-lto-objects`, which links
fine and performs **no** cross-TU optimisation (rust-lld takes the native code and ignores the
bytecode). Neither is worth doing: the hot path is inside `secp256k1.c`, which libsecp already builds
as a unity TU, so intra-secp inlining happens regardless. LTO would only reach the secp↔Core boundary,
which is not hot. **Closed.**

## 4. E3 and E4 close as "already correct"

### `NDEBUG` costs 0.0018%, so keep the assertions

Removing all 31 live `assert()`s in `script/interpreter.cpp` and 9 in `pubkey.cpp` saves **6,832 cycles
out of 376 million.**

This was filed as PRICE ONLY, because removing Core's assertions spends fidelity and swaps an abort for
*a proof of a computation that violated an invariant Core relies on*. The price turns out to be
nothing, which dissolves the trade-off rather than resolving it: **we keep the assertions and pay
essentially zero for them.**

The mechanism is clear in hindsight — the assertions are in Core's interpreter, on the ~4.6% side of
the 95.4/4.6 cycle split, and they are predictable branches the compiler largely hoists.

### Window 19 sits at the knee

| step | delta |
|---|---|
| 15 → 17 | −4,798,776 (−2,399,388/step) |
| 17 → 18 | **−4,379,718** ← steep |
| 18 → 19 | **−383,906** ← knee |
| 19 → 20 | −747,209 |

The investigation predicted 19 might be **past** the optimum, on the theory that a larger table costs
page-ins in RISC0's paged memory. **That was wrong.** Smaller is strictly worse — window 15 costs
+2.5% — and larger buys very little.

It also explains the original anomaly that motivated the experiment. 15 → 19 bought only −1.8% to
−2.3% not because paging ate the gain, but because **most of it is already spent by window 18**; the
curve flattens, so four steps buy barely more than three.

Window 20's −0.198% is real but doubles the table in the ELF. It is included in the combined arm above;
whether it is worth the size is a judgement, and the honest summary is that **19 was a good choice.**

## 5. Why this should not be landed on its own

The change is four lines:

```
build.rs     .opt_level(3)              (both cc::Build calls)
build.rs     ECMULT_WINDOW = 20
Cargo.toml   [profile.release] lto = "fat", codegen-units = 1
```

It moves `METHOD_ID`, and **a guest re-baseline resets the board to genesis.** As of 2026-08-26 the
board carries the first external contribution in the project's history — two verified ranges from
`jon`, one of which (block 1879) *is* the current frontier. Re-baselining now discards that.

⚖ **1.16% is worth about 2.4 GPU-months against a 17 GPU-year chain — roughly €1,950, earned once.**
That is real money and it should be taken. It is not worth spending a board reset on by itself, and it
does not expire. **Batch it with the next guest change that is already paying for a re-baseline** —
#139 being the obvious candidate.

## 6. What this says about where cost actually lives

Tier 0 covered the entire guest codegen axis and found **~1.16%**. For contrast, #136 and #137 — both
in the *plumbing*, neither in consensus code — were worth **2.00x**:

- `env::read` was **50.9% of a chunk's cycles before any Bitcoin logic ran**, at ~147 cycles/byte,
  because serde walks risc0's word stream one byte at a time.
- The payload shipped the spending transaction once per **input** rather than once per transaction —
  6,995,621 bytes of a distinct 123,883 on block 741,000, a factor of **56.5**.

Compiler flags are worth about one percent. Looking at what the program actually does was worth a
hundred. That is the argument for E5 (coordinator egress) and E6 (worker processes × po2) over any
further codegen work.

# Overnight 2026-08-01 — v0.13.0 release + Phase 3

Running under `/loop`, self-paced. Plan order: (1) build binaries → (2) verify spine/fold against the
REAL board → (3) cut and publish v0.13.0 → (4) stranger path → (5) Phase 3 G1 retention gate.

**Hard rules in force:** no ghostd, no guest change / no re-baseline, no coordinator deploy, no
competing for the GPU box's VRAM or disturbing the live coordinator. Open decisions go into the issue
and get skipped, not guessed.

---

## Going in

Committed on `main`, unpushed (5 ahead of origin at start):

| commit | what |
|---|---|
| `93b9bff` | `host extend-spine` — spine advances, never re-folds (#30) |
| `019c860` | coordinator `/api/spine` — store, verify, serve the head (#30) |
| `bc3798a` | `hazync spine` — drive it from retained receipts (#30) |
| `8918325` | `/api/foldable` + `hazync fold` — folding as its own task (#37) |
| `1d55e4d` | build-release: install r0vm where a non-root user can use it |

**METHOD_ID must NOT move.** Verified before starting: everything changed since v0.12.2 is host,
worker, coordinator and evidence — no guest source, no `coreshim`, no `patches/`, no `Cargo.lock`, no
`reproduce/`. `extend-spine` reuses guest mode 7 (the existing fold) rather than adding a mode. So
both binaries must come out `be5e0528`, and `build-release.sh` gates on exactly that. If either build
reports otherwise: STOP, write it up, ship nothing.

**Known caveat being closed tonight:** everything built so far was only ever exercised under the local
non-canonical guest `72fb6608`. Item (2) is what proves it against real `be5e0528` board receipts.

---

## Log

### 22:5x — item (1) started

GPU box `212.147.241.135` pre-flight: 0 MiB VRAM in use, 0% util, 18 GB free, 2 worker processes
cycling. Safe to build — the CUDA build compiles kernels (nvcc, CPU work) and its smoke tests are
execute-mode, so it does not take VRAM. Same combination ran fine alongside the workers earlier today.

Synced to `/root/hz`: `prover/host/src/main.rs`, `prover/build-release.sh`, `coordinator/hazync`,
`coordinator/server.py`. Confirmed `extend_spine_cmd` and `cmd_fold` present on the box.

Both builds launched with `SKIP_GROTH16=1 SKIP_GROTH16_PULL=1`:
- **CPU** — container on this laptop (`ubuntu:22.04`, `HOME=/root`), cold target, ~25 min expected.
- **CUDA** — GPU box, warm `prover/target` (4.9 GB), so only the host crate recompiles.

Awaiting both.

### 23:57 — CPU binary built, gate HELD

```
wrote dist/hazync-host-x86_64-linux-gnu
  METHOD_ID : be5e05280e108bdeeb6747b231df375f946a8530300525b5d4c523abe5b7c246
  canonical : be5e05280e108bdeeb6747b231df375f946a8530300525b5d4c523abe5b7c246
  seg-po2   : 20  (expected 20)
```

`be5e0528` as predicted — the guest really was untouched, so the id did not move and every proof on
the board stays valid. GLIBC_2.34, bundle-roundtrip PASS, regression PASS. 183,985,800 bytes (v0.12.2
was 183,953,944; the delta is `extend-spine`).

CUDA still re-provisioning its toolchain on the box (nsight-compute etc.) — the container is `--rm`,
so it reinstalls each run regardless of the warm `prover/target`.

### 00:0x — item (2) started early, using the canonical CPU binary

No reason to wait for CUDA: item (2) is about the GUEST ID, and the CPU binary is canonical.

Real board receipts fetched (`/api/proof/{1,2,3}`) and verified against the canonical binary — they
verify, which they did NOT under the local `72fb6608` build. Seams chain as they should:

```
p1  lo=1 hi=1  out_tip=4860eb18…
p2  lo=2 hi=2   in_tip=4860eb18…  out_tip=bddd99cc…
p3  lo=3 hi=3   in_tip=bddd99cc…  out_tip=4944469562ae…
```

Running the negative (non-adjacent) case plus two absorptions on these real receipts now.

### 00:2x — item (1) COMPLETE: both binaries canonical

CUDA built on the box:

```
wrote /root/hz/dist/hazync-host-x86_64-linux-gnu-cuda
  METHOD_ID : be5e05280e108bdeeb6747b231df375f946a8530300525b5d4c523abe5b7c246
  canonical : be5e05280e108bdeeb6747b231df375f946a8530300525b5d4c523abe5b7c246
  seg-po2   : 21  (expected 21)
  SASS      : sm_80 sm_86 sm_89 sm_90     GLIBC_2.34
```

Both smoke tests pass. Fetched to `dist/`, sha256 compared against the box: match
(`2925dead2584e493`), 327,277,136 bytes.

**Caught while assembling the assets:** `dist/hazync-worker` was still the **v0.12.2** copy — the one
without `fold` and `spine`. Shipping that would have released a worker missing both new commands,
while the release notes advertised them. Re-packaged; the shipped worker now has them:

```
$ ./dist/hazync-worker --help
  hazync fold [n]    # fold n adjacent proven ranges into wider ones (default 1)
  hazync spine [n]   # advance the genesis-anchored spine by absorbing the next n chunks
```

`hazync-verify.wasm` re-built bit-identical (`6c72dcd667b25da5`), and the two verify binaries are
unchanged since the verifier source is untouched — carried forward.

**Real-receipt progress (item 2):** negative case refused correctly; `[1..1] + [2..2] -> [1..2]` in
**115.4s** on genuine `be5e0528` receipts (the local-guest run was 105.9s — consistent). Second
absorption still running.

### 00:4x — item (2) passed on real receipts, then found a REAL BUG in my own seam check

Full real-receipt run against the canonical binary: negative refused, `[1..2]` in 115.4s, `[1..3]` in
200.4s, and `verify-range` accepted `[1..3]` as genesis-anchored with `out_tip` = real mainnet block 3.

Then I pushed one absorption further — block 4 — to settle the flat-vs-growing fold-cost question,
and it **failed the seam**:

```
assertion failed: chunk in_roots != spine out_roots
  left  [Some(A), Some(B), None]
  right [Some(A), Some(B)]
```

**Same roots. Different trailing padding.** The accumulator's root vector carries empty slots for
absent levels, so one UTXO set has several Vec representations. The guest normalizes before
comparing; my host-side pre-check did a raw `assert_eq!` and was therefore **stricter than the check
it exists to pre-empt** — the worst way for a fail-fast guard to be wrong, because it converts a cheap
early warning into a false rejection of valid work. It would have blocked the spine at the first
absorption where the accumulator gains a level.

`normalize_host()` already existed for exactly this (line 193, "mirrors the guest `normalize`") and is
used at two other comparison sites; extend-spine simply didn't call it. Fixed to compare normalized.

**This is why the binaries in `dist/` cannot ship as built** — both contain the buggy check. Both are
being rebuilt: CUDA on the box, CPU here after the fix is validated.

Validating the fix by reproducing the same structural case (4th leaf) under the local guest, since the
canonical binary in `dist/` is the buggy one.

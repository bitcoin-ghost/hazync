# v0.21.7 pre-release trial — rented pods, 2026-09-18

Everything below is measured on rented hardware from `main`, not projected. Raw logs and
receipts: `~/hazync-v0217-trial/` (18 files from the server pod, 6 from the worker pod,
3 receipts; archives hash-verified at both ends before the pods were released).

## What this was for

To answer one question before cutting a release: **does the prover work fully, including
the paths a single machine cannot exercise?** Specifically the two topologies asked for —
several cards in one unit, and several units with one card each.

## Hardware

| role | pod | GPU | $/hr |
|---|---|---|---|
| server + 2 local workers | `hz-trial-v0217` (US) | 2 × RTX PRO 6000 Blackwell | 1.18 |
| remote worker | `hz-trial-unit3` (EU) | 1 × L40S, 46,068 MiB | 1.09 |

Both pods built from source and produced the **canonical** METHOD_ID
`37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. A RISC0 image id
absorbs absolute build paths, so this only reproduces under `HOME=/root`,
`HAZYNC_BASE=/root/hazync-build`, `REPO_DIR=/hazync-zkvm`.

## Single unit, every mode

| gate | result |
|---|---|
| method-id (GATE A) | PASS |
| regress (block 170 consensus) | exit 0 |
| adversarial soundness suite | exit 0 |
| prove-block, real STARK | exit 0, **PROVED in 16.2 s**, VERIFIED |
| mode 6 across both MIG devices | receipt verified against METHOD_ID |
| `verify-any` (the coordinator's gate) | exit 0 |
| `ci_verify_any.sh` (own fixtures) | exit 0 |

## Mode 6 distributed — block 230,000

693,287-byte bundle, 72 segments, 71 joins, `po2 21`. Block 230,000 rather than 418,268
because this tests **topology**; the 16 MB block took ~60 min on two slices.

| | 1 card | 3 cards | |
|---|---|---|---|
| execution | 9.4 s | 9.0 s | |
| worker wall | 296.9 s | **108.2 s** | 2.74× |
| assembly | 25.1 s | 13.7 s | |
| **TOTAL** | **331.5 s** | **130.9 s** | **2.53×** |

Work divided across all three cards, 72 segments and 71 joins accounted for exactly:

| worker | card | segments | joins |
|---|---|---|---|
| local1 | Blackwell 0 | 24 | 16 |
| local2 | Blackwell 1 | 25 | 36 |
| unit3 | L40S, over an SSH tunnel from the EU | 23 | 19 |

Both requested topologies are covered by this single run: two cards in one unit, and a
second unit contributing one card.

## The receipts are equivalent — and not byte-identical

The 1-card and 3-card runs both produced claim digest
`cbf388ff06cae4298ec9db57534cc7a8cdd1a62d1962a9705eca0400da9a6655`, and both pass
`verify-any` with `RANGE-OK lo=230000 hi=230000 … anchored=no`.

⛔ The two `.hzk` files differ in **220,126 bytes**. That is expected, not a fault:
`rand_z` is drawn per segment from the OS RNG, so seal bytes vary between runs. **The claim
digest is the equivalence test; the file hash is not.** `anchored=no` is likewise correct —
a mid-chain range proves a transition between its stated boundaries, and anchoring comes
from the connected chain or from `verify-range` / `verify-chain`.

## The four gaps above were closed the same day

Every limitation this document originally listed was then measured. Raw logs and receipts per gap
live under `~/hazync-v0217-trial/<gap>/RESULT.md`.

| gap | result |
|---|---|
| **live submission** | block **92,864** proved and submitted to the public board as `G H O S T`, 9 segments, 44.9 s. Confirmed from the board, not the exit code: `/api/block/92864` → `status=proved`, `unbroken=true`, and `/api/proof/92864` downloads 229,002 B — exactly the receipt size on the pod |
| **more than 3 cards** | block 230,000 on **4 × RTX PRO 6000 Blackwell**: TOTAL **96.0 s** vs 331.5 s on one card (**3.45x**), 18 segments per worker |
| **the 16 MB block** | block 418,268 (15,979,389 B, 1,650 segments) on 4 cards: **1454.9 s (24.2 min)** vs **3665.5 s (61.1 min)** on 2 MIG slices — **2.52x** |
| **genesis anchoring** | `/api/proof/1-2048` → `RANGE PROOF [1..2048] VERIFIED — genesis-anchored`. ⛔ With a CONTROL: the same binary REFUSED a mid-chain proof on the genesis pin, exit 101. A verifier that accepts everything passes a positive-only test |
| **Groth16 proving** | `ci_snark_prove.sh` on a CPU host: **226,946 B → 4,217 B in 49 s**, wrapped proof verifies, and `out_tip_hash` / `range_work` / `total_cum_work` all preserved across the wrap |

Segment count was predicted to scale linearly with bundle size (693,287 B → 72 segments, so 23.0x
should give ~1,656). Measured **1,650** — linear to within 0.4%. That was a prediction worth testing
rather than assuming.

## What is STILL not established

- **Nothing above 4 cards.** 6x and 8x configurations were refused for lack of capacity, and the REST
  API's `gpuTypeIds` is a closed enum with no MIG entries, so the GraphQL ids that advertise 8-way are
  not creatable. ⚠ `lowestPrice` reports a PRICE, not availability — it still lists configurations the
  create call refuses. Only the POST tells the truth.
- **The 16 MB block was proven on one machine's four cards, not across units.** Multi-unit was shown
  only on the 693 KB block.
- ⛔ **Groth16 wrapping is CPU-only.** It crashes in `sppark` on every CUDA build (#20, closed
  won't-fix upstream), so the CUDA wrap path remains unexercised and is expected to fail.
- **The live submission ran on `3f6b5c8`**, before #402/#403 merged — neither is on the `hazync run`
  path, but the final release candidate was not itself the binary that submitted.
- **One block, once.** No sustained multi-block run against the live board.
- `anchored=no` still applies to the mid-chain trial receipts, and correctly so; anchoring was
  exercised separately, on a folded `lo=1` range.

## Traps this run walked into (so the next one doesn't)

1. **`HAZYNC_BIND` must be set for a remote worker.** `seg-serve` binds `127.0.0.1` by
   default (#365) because the wire is unauthenticated. PR #401 now says so in the shell.
2. **A RunPod pod exposes only the ports declared at creation.** This pod had `22/tcp`
   only, so the first remote worker died with
   `connect <public-ip>:9111: Connection refused` — the fix is an SSH tunnel and dialling
   `127.0.0.1:9111`, not the public IP. seg-serve was listening correctly the whole time.
3. **Pin the working directory.** The first run logged `receipt written to
   range_230000.hzk` and the file was not where anyone looked — it landed in `/root`,
   seg-serve's inherited CWD.
4. **Process counts over SSH lie.** A pattern appearing anywhere in the invoking command's
   argv — including an unrelated `echo` — is matched by `ps`, and `ps | grep -F "$PAT"`
   also catches grep's own argv. Judge by `ss -ltn` and an actual connect attempt.
5. **`grep -c` prints `0` and exits non-zero**, so `$(grep -c x || echo 0)` yields `"0\n0"`
   and every `= "0"` test fails for ever. Use `grep -q` inside an `if`.

## Cost

$2.27/hr for the pair. Both were released once the evidence above was on disk and
hash-verified; the trial's marginal cost was the wall-clock of the runs, not the day.

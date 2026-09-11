# Hazync v0.21.2 — workers that fail loudly, a spine that keeps up, an aggregate you can time

A reliability release. Every change comes from something that went wrong on the live board or a
rented fleet on 2026-09-11. Each section below names the incident behind it.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. The guest, the circuit and
> the verifier logic are unchanged; only the host (`prover/host/src/main.rs`) changed on the prover
> side. Every existing proof stays valid and the board does not reset.

---

## Worker (`hazync-worker`, `hazync-run-workers.sh`)

- **#261: a box with no usable GPU stops instead of eating the frontier.** A card whose CUDA device
  was unusable passed boot (`method-id` never touches the GPU). It then claimed and abandoned
  14 blocks, each held for an hour. Now:
  - a no-device, driver or init error exits `EX_CONFIG` (78) after one attempt, without walking the
    segment ladder;
  - `run-workers.sh` runs a real GPU prove (`prove-block`) before it starts any worker loop
    (`SKIP_GPU_SMOKE=1` to opt out).
- **#256: a stuck prove is caught by missing *progress*, not by a 90-minute timeout.** The host
  prints `executed, N segments at po2 P -- proving`, then one line per segment. The worker kills a
  prove that shows no progress for `HAZYNC_STALL_MIN` (600 s), or none at all within
  `HAZYNC_FIRST_PROGRESS` (1800 s). It retries once at the same size. A stall no longer walks the
  segment ladder: smaller segments only make a slow prove slower. The outer ceiling is now
  `HAZYNC_PROVE_TIMEOUT` = 6 h for watched proves, so a big honest block isn't killed.
  Heartbeats are only sent while progress is being made.
- **#268: a lost claim response no longer orphans a block for an hour.** The worker sends one
  nonce per claim and reuses it on every retry, and the coordinator hands a retry back the block it
  already gave. On 2026-09-11 a coordinator stall orphaned 19 blocks under the frontier this way.
- **#272: the spine absorbs the widest verified chunk at each step, not one block.**
  `extend-spine` folds `[1..N] + [N+1..M]` at the same cost for any `M`, but `hazync spine` fetched
  one block per step. So the one serial job in the system ran at one fold per block: ~92 blocks/hr
  on a shared card and ~230/hr on its own, against ~2,500/hr proven. Each step now takes the widest
  range in `/api/vranges` that starts at `N+1`. It falls back to narrower ones, down to the single
  block, if a chunk won't absorb or its head is refused. `hazync spine n` still means n steps.

## Coordinator

- **#273: fold above the spine, not at or behind it.** `/api/foldable` offered the lowest unfolded
  pairs first, and with a spine running those sit at or below its head, where the spine can never
  absorb them. Measured with two cards folding: the spine gained nothing (228 blocks/hr against
  230), a 128-block node was finished 85 blocks behind it, and on 92 of 93 steps the spine reached
  N+1 before `(N+1, N+2)` was folded. Nothing at or below the spine's head is offered now.
- **#265: the board API can't jam under load.** It now uses WAL, and a single-flight cache for
  `/api/state` and the frontier. Already live since 2026-09-11 17:52 UTC.
- **#265 follow-up (#271): `SIGUSR1` dumps every thread's stack** to the journal, and the coordinator
  keeps serving. The next wedge can be diagnosed before the restart that clears it.
- **#268 server side:** the claim nonce above. Workers from before this release send none and behave
  exactly as before.

## Prover host (`hazync-host-*`)

- **#254: `seg-connect` logs every task with a wall-clock timestamp.** Every line leads with epoch
  ms, and every segment, join (with its level and position), resolve and lift gets one line with
  `recv_ms`, `send_ms`, `wait_s` (time idle waiting for work), `compute_s` and frame sizes.
  `HAZYNC_SEG_QUIET=1` restores the old output.
- **#252: `HAZYNC_RESOLVE_LOCAL=1`** (opt-in) runs the aggregate's 27-step resolve chain on the
  segment coordinator's own prover, instead of making 27 round trips to workers. The measured effect
  is under *Validation*. The default is unchanged.

## Verifier

- **#249: `hazync-verify.wasm` is byte-reproducible.** It used to embed the builder's absolute
  paths. Now it builds from a fixed staging directory with remapped path prefixes, and any checkout
  gives the same bytes. ⚠ Its hash changes from v0.21.1, so the hosted verify page is redeployed
  with this release.

## Release tooling and docs

- #247 `release.sh` no longer prints "wasm packaged" when the wasm was skipped. #248
  `check-dist.sh` no longer prints FAIL for a CUDA host it then accepts by attestation.
- #255: the milestone run 4 write-up is corrected by the #235 N-sweep.

## Validation

GPU validation of this host on an RTX A6000 (sm_86, RunPod), 2026-09-11. The host source
(`prover/host/src/main.rs`) validated there is byte-identical to this release's.

| check | result |
|---|---|
| `method-id` | `37987b85…` ✅ |
| `prove-block` smoke | proved in 17.0 s, receipt VERIFIED ✅ |
| `hazync prove 170` through the real worker CLI (#256) | exec line + 3/3 segment progress lines, proved in 18.2 s ✅ |
| loopback aggregate of block 966,256, 585 segments, `HAZYNC_RESOLVE_LOCAL=1` | VERIFIED; resolves **8.4 s**, total 2,378.1 s ✅ |
| same, resolves pushed to the worker | VERIFIED; resolves **10.9 s**, total 2,397.0 s ✅ |
| `seg-connect` task log (#254) | 1,170 / 1,197 task lines, 0 without a timestamp ✅ |

- **Local resolve:** the aggregates ran on one card (loopback), one run each. The 18.9 s gap in
  total time is mostly run-to-run noise: worker wall time alone moved 18.4 s. Local resolve saved
  2.5 s here, where a pushed resolve crosses no network. The networked case, where #235 measured
  ~25 s for the 27 resolves, is not measured.
- **#272 on the live board:** the spine card ran the release worker. The spine head went
  3376 → 3392 → 3424 → 3428 → 3430 → 3432 → 3440 → 3444, and the coordinator verified each head.
  That is the first multi-block `extend-spine` on a GPU, at ~1,760 blocks/hr while the fold tree
  lasted.
- **#273** is covered by unit tests and controls only. Its effect on the live spine gets measured
  after this release is deployed.

## Upgrading

Replace `hazync-worker`, `hazync-run-workers.sh` and the host binary. Identity (`~/.hazync`),
bundles and the board are untouched. Check the downloads against the signed `SHA256SUMS.txt`, and
confirm `hazync-host method-id` prints `37987b85…`.

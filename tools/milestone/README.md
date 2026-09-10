# Milestone runner — one block, many rented GPUs, one clock

The orchestration that produced `docs/history/MILESTONE_966256_RUN4_2026-09-10.md`: block 966,256
proved end to end in 544.0 s across 27 rented RTX 4090s, T0 to VERIFIED, no manual steps.

It lived in a scratch directory through four runs and is committed here because it encodes a night's
worth of failures that are not obvious from the outside, and every one of them **produced a
plausible wrong answer rather than an error**.

## Use

```sh
export HAZYNC_RUNDIR=~/run5            # working directory; every script reads $S from this
mkdir -p $HAZYNC_RUNDIR
# put the fleet in $HAZYNC_RUNDIR/assign_opt.txt, one card per line:
#   <pod-id> <ip> <ssh-port> <site> <chunk-index> <segments> <s-per-segment>
./run_continuous.sh                    # clears + verifies the fleet, then T0 → VERIFIED
```

`run_continuous.sh` is the driver. `pod-prove.sh` runs on each card (prove + 1 Hz telemetry),
`bootstrap2.sh` prepares a pod, `remote_clear.sh` empties one, `probe3.sh` is a read-only fleet
probe, `gather4.sh` pulls all evidence off **before teardown**, and `autoreceipt.sh` / `regen4.sh`
rebuild the final receipt with `agg-chunks`.

⛔ **Gather before you terminate.** `gpu_samples.csv` and `aggw.log` exist only on the pod. Once it
is gone they cannot be reconstructed, and they are what the infographic and the energy figure are
made of. → `.claude/skills/benchmark-infographic/`

## What the checks in here are defending against

Each of these silently produced a wrong result in a real run:

| failure | what it looked like | the check |
|---|---|---|
| stale receipts | a chunk "completed" in 63 s | pods are cleared **and verified empty** before T0 |
| silent `scp` drop | coordinator got 21/22, seg-serve panicked | every copy verified, retried, count asserted |
| sequential ssh | 12-minute wedge | every ssh parallel and under `timeout` |
| remote self-kill | the reaper killed its own shell | kills go via a script file, never `pkill -f` inline |
| #147 wedge | live process, 0 % GPU | watchdog on a **direct death signal**, never log growth |
| reassign onto a busy card | two proves, one GPU, `rc=101` (#97) | requires no process **and** VRAM released |

⛔ **The stall detector must never infer.** Three runs were wrecked by proxies for "stalled": a
byte-count threshold that fired during the CPU-only execute phase, then a tighter limit keyed on
"proving has begun" — which fires during the first segment, where CUDA context creation and kernel
JIT legitimately produce ~2 minutes of silence on a cold card. It shot chunk 1 at +2:07 while 22
other cards were at segment 70-of-80. It now kills only when the card is demonstrably idle: no prove
process, **or** GPU under 5 % with VRAM released.

⛔ **Remote commands in double quotes expand `$( )` locally.** The probe was written that way and
returned a constant, so every healthy card looked frozen and was shot at exactly the grace limit.
Single-quote the body; pass in only the variables the remote side needs.

⛔ **`ps -eo comm` truncates at 15 characters**, so `^hazync-host-cuda$` matches nothing and a live
aggregate reads as dead. Acting on that relaunched `seg-serve` on top of a working one; the new
process died on `bind()` while the original kept going with its log already unlinked, and its
verified digest was lost. Match `^hazync-host-cud`, or use `ps -eo args`.

## Two things fixed after run 4

- **The poll fans out.** It used to make 27 ssh round-trips one after another, so a tick cost ~20 s
  and the last chunk of run 4 sat finished and unnoticed for 24.9 s — the largest recoverable waste
  in the run. Probes now run in parallel into `$S/_probe/` and a tick is ~1 s, with the sleep cut
  from 8 s to 3 s.
- **The join tree is recorded.** `seg-serve` prints `joins N/584` throughout assembly and nothing
  captured it, so the 128.7 s fold had no timing anywhere. The aggregate poll now greps it and
  appends `<offset> <n>/<total>` to `$S/joins.tsv`.

## What the run leaves behind

```
$S/continuous.log     T0, phase offsets, every intervention, the VERIFIED block
$S/joins.tsv          join-tree progress over time  (new after run 4)
$S/rc/chunk_N.bin     the chunk receipts as they land
```

Per card, pulled by `gather4.sh`: `prove.log`, `result.json`, `gpu_samples.csv` (1 Hz utilisation,
VRAM, temperature, power, clocks), `host.txt`, and `aggw.log` — the worker's own record of its
aggregate work.

⚠ `facts.json` has a field-shifting defect: its `gpu` object was built by splitting `nvidia-smi` CSV
on spaces rather than commas, so `name`/`uuid`/`driver`/`vram_total_mib` are offset by one.
`host.txt` carries the same data correctly.

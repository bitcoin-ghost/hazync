# Bridge memory against the UTXO set — the measurement behind #435, and what it cannot answer

**Evidence:** `docs/history/bridge-mem-2026-09.txt`, 494 samples, 2026-09-15 to 2026-09-20,
`hazync-proof` walking history from block ~146,000 to ~818,000. Collected by
`hazync-bridge-mem-sampler` (hazync#532). 451 of those samples carry a checkpoint line and so have a
UTXO count beside the memory figure; those are what everything below uses.

⚠ **This log is the whole record.** The sampler that produced it existed on one box and in no repo,
and for the seven days after 2026-09-20 it wrote 1,188 further samples that measured nothing at all,
because the bridge had moved to `hazync-coord` and it was still watching `hazync-proof`. It is
committed here so a sizing decision is not resting on a file in `/root` on one machine.

## What was measured

| | UTXOs | RSS | VmHWM |
|---|---|---|---|
| start of the walk (h≈160,000) | 1,266,361 | 541 MiB | 682 MiB |
| end of the walk (h≈816,257) | 128,822,673 | 43,424 MiB | 47,495 MiB |

Least squares over all 451 points:

```
rss_MiB  ≈  887  +  335.7 × (coins / 1e6)        R² = 0.872
```

⛔ **R² of 0.872 is not as good as it sounds here, and the per-sample ratio is worse.** RSS moves in
*steps* — the allocator takes a new arena and holds it — while the coin count moves smoothly, so
"MiB per million coins" computed at an arbitrary sample lands anywhere between **287 and 492**
depending only on where in a step you happened to look. Worst residual against the fit is
**±14,845 MiB**. Treat the slope as an order of magnitude, not a coefficient.

**Five restarts are visible in the data** and must not be read as the process shrinking. `VmHWM` is a
high-water mark and cannot fall within one process, so every fall is a new one:

```
2026-09-16T22:08:52Z  peak 19,191 -> 14,653 MiB
2026-09-18T08:48:33Z  peak 23,850 -> 23,849 MiB
2026-09-18T18:41:57Z  peak 44,863 -> 29,986 MiB
2026-09-18T23:33:39Z  peak 44,890 -> 42,611 MiB
2026-09-20T04:23:55Z  peak 44,209 -> 33,587 MiB
```

## ⛔ What it cannot answer: where the ceiling is

The walk stopped at 128.8M coins. Everything past that is extrapolation, and the one point beyond it
that can be checked says the extrapolation runs high:

| coins | fit predicts | measured |
|---|---|---|
| 128,822,673 (walk end) | 44,129 MiB | **43,424 MiB** |
| 165,192,074 (tip bridge, 2026-09-27) | 56,337 MiB | **43,112 MiB** |
| 200,000,000 | 68,021 MiB | — |

At the tip the bridge sits **13 GB below** what the fit expects. So the 200M row — which would exceed
the tip box's `MemoryMax` of 61,440 MiB — is **not** a prediction this data supports. It is the fit
running past its evidence in exactly the region where it is least constrained.

⚠ **The number that is actually close to the cap is the PEAK, not the RSS.** On `hazync-coord`,
2026-09-27:

```
VmRSS       43,112 MiB
VmHWM       59,278 MiB      <- 114 MiB under MemoryHigh
MemoryHigh  59,392 MiB
MemoryMax   61,440 MiB
```

`memory.events:high` is **2,720**, so it is being throttled. But `memory.pressure` totals **8.7
seconds** over the service's entire life with avg10/avg60/avg300 all `0.00`, so that throttling is
costing nothing measurable — which is what `memory.high` is for. Judge this by `memory.pressure`, not
by the `high` counter → the #413 incident is the case where the pressure figure was the one that
mattered.

## For #350 (coin metadata to disk)

The honest position: memory clearly scales with the coin count, the slope is roughly 300–350 MiB per
million coins, and the tip bridge currently fits in 62 GB with its peak 114 MiB under the throttle
point. Whether it still fits at 200M coins is **not** answered here, and the only way to answer it is
to keep sampling — which is now happening on the right box.

⚠ **The tip bridge cannot extend this curve.** It starts at `HAZYNC_BRIDGE_EMIT_FROM=967500` with the
whole set already loaded, so it produces one point, repeatedly. Extending the curve needs another
history walk. What the tip sampler *does* answer is whether the footprint stays under #435's caps.

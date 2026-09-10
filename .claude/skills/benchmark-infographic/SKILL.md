---
name: benchmark-infographic
description: Turn a hazync distributed-proof run's evidence directory into an infographic set — an interactive HTML trace page, an animated GIF, and a static poster (PNG + SVG). Use when asked to visualise, chart, or make a graphic from a block-proof benchmark run.
---

# Benchmark infographic

Builds the visual set for one distributed block-proof run from its evidence directory. Everything is
drawn from measured telemetry — no figure on the output is typed in by hand.

## Inputs it needs

An evidence directory produced by the milestone runner:

```
<evidence>/summary.csv          one row per chunk (built by the run's summary step)
<evidence>/summary.json         run-level phase timings and digests
<evidence>/cards/chunk_N/gpu_samples.csv    1 Hz: epoch,util_pct,mem_used_mib,temp_c,power_w,sm_clock_mhz,mem_clock_mhz
```

⛔ **Gather telemetry BEFORE tearing pods down.** `gpu_samples.csv` exists only on the pod. Once the
pod is terminated there is no way to reconstruct it, and the graphic loses its whole basis.

## Run it

```sh
cd <skill dir>
python3 build_data.py <evidence-dir> run_data.json   # bundle + energy integral

# full version -- every card labelled, phase markers, fleet and chunk stats
python3 render.py      run_data.json                 # poster.png + animated .gif
python3 render_svg.py  run_data.json                 # poster.svg (vector)

# minimal version -- for social; traces recede to texture, stats cut to three figures
python3 make_min.py                                  # run4-min.png + run4-min.gif
python3 render_credits.py                            # run4-credits.png

python3 -c "d=open('run_data.json').read();t=open('artifact_template.html').read();\
open('trace.html','w').write(t.replace('__DATA__',d))"
```

Then publish `trace.html` with the Artifact tool as the interactive master.

### The published design

`render_min.py` + `min_template.html` are the settled design (run 4, 2026-09-10):

- 16:9, 1600×900 logical — PNG at 3200×1800, GIF at 1280×720
- one centred title line, name and headline figures split by interpuncts, regular weight, 75% opacity
- **one row per card carrying its whole run**: GPU power in its site colour while proving, then that
  card's aggregate work in green, on a single 0→wall axis
- the ring sits *in front* of the traces, dead centre, at 75% opacity over a soft ground disc
- after assembly begins the rows **converge to a single root** — the join tree folding every worker's
  work into one receipt
- the four digests sit below the traces, two per line, left and right justified

⚠ The convergence curve's *shape* is the tree's structure. Its two ends are measured (26 workers at
assembly start, 1 receipt at VERIFIED); the pace between them is not logged. Do not add tick marks or
intermediate labels to it — that would assert timing nobody recorded.

### Which version to reach for

- **Full** (`render.py`): for the docs and anyone who wants to read it. Per-card rows are labelled
  with chunk, site and wall time; phase markers on the axis; fleet and chunk-phase stats.
- **Minimal** (`make_min.py`): for social. Title, subtitle, three figures, traces dropped to 25%
  opacity as background texture, one condensed aggregate bar, one ring. No legend, no per-card
  labels, no stats block.
- **Credits** (`render_credits.py`): the four values that let a stranger check the claim — journal
  digest, guest METHOD_ID, receipt sha256 with its byte count, chain tip — plus the two commands
  that verify the receipt without proving anything. The minimal GIF ends by resting on this card,
  so the loop finishes on the evidence rather than the headline.

⚠ **The minimal poster's traces span the CHUNK PHASE only** (0 → 287.9 s), not the full run. Drawing
both phases on one 544 s axis left the right half of the poster empty, because the aggregate has no
per-card telemetry to plot. The aggregate becomes the bar underneath, and the two read as the
near-equal halves they are — 247.9 s against 241.2 s. Label both or the axis lies.

⚠ `build_data.py` has `T0` hard-coded — the run's clock start, from the `### T0=… ###` line in the
driver log. Set it per run or the start/end offsets are meaningless.

## What the design encodes, and why

- **One row per chunk, ordered by finish time.** The ragged right edge *is* the straggler ratio.
  Ordering by chunk index instead produces noise that carries no information.
- **Power, not utilisation, as the default trace.** At 1 Hz, utilisation is a per-segment sawtooth
  that reads as visual noise across 27 stacked rows; power is smooth and shows the ramp, plateau and
  fall-off cleanly. Utilisation is a toggle on the HTML master.
- **The aggregate window carries its own breakdown.** The chunk traces stop well before the run does
  — on run 4 at +263 s of 544 s — and the aggregate is the *larger* half. Left as blank axis it reads
  as nothing happening. It gets a shaded band with its three sub-phases and a note that the workers
  were attached but the per-card samplers had already exited, so there is genuinely nothing to plot.
- **Colour by site**, from the house palette. Six tonally-matched hues, with the accent going to the
  largest cohort so the boldness sits in one place.

## House style (from `~/hazync-social`)

```
ground #0f1012   panel #16181b   rule #2e3135   soft #212427
text   #e6e4de   dim   #8b8a84   faint #55544f
accent #f7931a   verified #5cc77e
sites  US #f7931a  CA #e8a94f  IS #6fb3c4  TW #d4694f  NO #78c4a0  FR #b8879b
type   IBM Plex Mono (data, labels, digests) + IBM Plex Sans (headline only)
```

The HTML master links IBM Plex from Google Fonts. The raster and vector renders fall back to
DejaVu Sans Mono, which is what is installed locally — metrics differ slightly from the HTML.

## Honesty rules — do not drop these

The graphic states both of these, and it should keep stating them:

1. **The wall clock is attested, not proved.** It comes from driver logs and 1 Hz telemetry. Sitting
   a stopwatch next to a cryptographic digest does not make the stopwatch cryptographic.
2. **The energy figure is a floor.** It integrates GPU-rail power only — no host CPU, RAM, PSU loss
   or cooling — and the samplers exit as each card finishes, so the aggregate phase is not in it.

Check the country count against datacenter IPs rather than pod labels. A "spare" swapped in
mid-run may share an IP — and therefore a site — with a card already counted; that mistake put a
seventh country into the run 4 write-up before it was caught.

## What is still missing

⛔ **The join tree has no timing.** `seg-serve` prints `joins N/584` progress lines throughout
assembly, but the run driver's poll only greps `VERIFIED|digest|TOTAL|execution|worker wall|assembly`,
so they are never captured and the pod is gone by the time anyone wants them. Add `joins` to that
grep and a real fold curve becomes drawable — it is the most interesting line the graphic could
carry, and the only reason assembly is shown as a shape rather than a trace.

## Gotchas hit while building this

- `ps -eo comm` truncates at 15 characters, so `hazync-host-cuda` never matches an anchored grep.
- `pgrep -f <pattern>` matches the invoking shell when the pattern appears in its own command line.
- Remote commands in double quotes expand `$(…)` **locally**. Single-quote the body and pass only
  the variables the remote side needs.

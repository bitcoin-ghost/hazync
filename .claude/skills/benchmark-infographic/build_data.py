#!/usr/bin/env python3
"""Build one data bundle from a run's evidence directory. Every deliverable reads this."""
import csv, json, os, re, sys, glob, statistics as st

EV  = sys.argv[1] if len(sys.argv) > 1 else '/home/defenwycke/hazync-milestone-966256-run4'
OUT = sys.argv[2] if len(sys.argv) > 2 else 'run4_data.json'
T0  = 1789008303.354643063
STRIDE = 2   # 1 Hz -> one sample every 2 s; the traces are smooth enough that this is lossless to the eye

# the spare is physically in the same datacenter as a Canadian card -- do not invent a country for it
SITE = {'SP': 'CA'}
NAME = {'US':'United States','CA':'Canada','IS':'Iceland','TW':'Taiwan','NO':'Norway','FR':'France'}

cards, energy_j = [], 0.0
for r in csv.DictReader(open(f'{EV}/summary.csv')):
    ch  = int(r['chunk']); loc = SITE.get(r['loc'], r['loc'])
    p   = f'{EV}/cards/chunk_{ch}/gpu_samples.csv'
    t, w, u, vr = [], [], [], []
    if os.path.exists(p):
        rows = list(csv.DictReader(open(p)))
        for i in range(1, len(rows)):                       # trapezoidal integral at full resolution
            dt = float(rows[i]['epoch']) - float(rows[i-1]['epoch'])
            if 0 < dt < 5:
                energy_j += (float(rows[i]['power_w']) + float(rows[i-1]['power_w'])) / 2 * dt
        for s in rows[::STRIDE]:
            t.append(round(float(s['epoch']) - T0, 1))
            w.append(round(float(s['power_w'])))
            u.append(int(s['util_pct']))
            vr.append(int(s['mem_used_mib']))
    cards.append(dict(
        chunk=ch, site=loc, site_name=NAME.get(loc, loc),
        inputs=int(r['inputs']), segments=int(r['segments']),
        wall_s=float(r['wall_s']), s_per_segment=float(r['s_per_segment']),
        start_s=float(r['start_offset_s']), end_s=float(r['end_offset_s']),
        peak_w=float(r['peak_power_w']), peak_c=int(r['peak_temp_c']),
        peak_vram_mib=int(r['peak_vram_mib']), mean_util=float(r['mean_util_pct']),
        cpu=r['cpu'], t=t, w=w, u=u, vram=vr))

# ---- aggregate phase, from each worker's aggw.log.
# The worker logs its 1st, 2nd and every 25th completed unit with an elapsed time, then a final
# total. So the early curve is measured and the tail has a known END POINT but no intermediate
# timing -- draw that part dashed and say so. Worker id wN was set to the chunk index, so the site
# colours carry straight over.
agg_workers=[]
for c in cards:
    p=f"{EV}/cards/chunk_{c['chunk']}/aggw.log"
    if not os.path.exists(p): continue
    pts=[]; total=tot_t=None; kinds={}
    for ln in open(p):
        a=re.search(r'(segment|join|resolve) \d+ in ([\d.]+)s \((\d+) done(?:, ([\d.]+)s elapsed)?\)',ln)
        if a:
            kinds[a.group(1)]=kinds.get(a.group(1),0)+1
            if a.group(4): pts.append([round(float(a.group(4)),1), int(a.group(3))])
        b=re.search(r'PUSH DONE (\d+) segments in ([\d.]+)s',ln)
        if b: total=int(b.group(1)); tot_t=float(b.group(2))
    if total is None: continue
    agg_workers.append(dict(w=c['chunk'], site=c['site'], units=total,
                            wall_s=tot_t, measured=pts))

cards.sort(key=lambda c: c['end_s'])                        # sorted by finish -> the staircase
walls = [c['wall_s'] for c in cards]
sites = {}
for c in cards: sites[c['site']] = sites.get(c['site'], 0) + 1

meta = json.load(open(f'{EV}/summary.json'))
bundle = dict(
    run='run 4', block=966256, txs=4741, inputs=sum(c['inputs'] for c in cards),
    cards=len(cards), gpu='RTX 4090',
    sites=dict(sorted(sites.items(), key=lambda kv: -kv[1])), site_names=NAME,
    wall_total_s=meta['wall_total_s'], cost_usd=meta['cost_usd'],
    energy_kwh=round(energy_j / 3.6e6, 3),
    total_segments=meta['total_segments'], retries_119=meta['retries_119'],
    phases=[dict(at=0.0,   label='T0',              note='fleet verified empty'),
            dict(at=40.0,  label='proves launched', note='27 cards'),
            dict(at=287.9, label='chunks complete', note='all 27, rc=0'),
            dict(at=293.7, label='receipts staged', note='27/27'),
            dict(at=333.7, label='aggregate open',  note='workers attached'),
            dict(at=544.0, label='VERIFIED',        note='against METHOD_ID')],
    aggregate=dict(total_s=meta['aggregate_total_s'], execution_s=meta['agg_execution_s'],
                   worker_wall_s=meta['agg_worker_wall_s'], assembly_s=meta['agg_assembly_s'],
                   segments=585, depth=4,
                   workers=agg_workers,
                   units_total=sum(w['units'] for w in agg_workers),
                   worker_wall_min=min(w['wall_s'] for w in agg_workers),
                   worker_wall_max=max(w['wall_s'] for w in agg_workers),
                   units_min=min(w['units'] for w in agg_workers),
                   units_max=max(w['units'] for w in agg_workers)),
    chunk_stats=dict(min_s=min(walls), max_s=max(walls), mean_s=round(st.mean(walls), 1),
                     median_s=round(st.median(walls), 1),
                     straggler=round(max(walls) / st.mean(walls), 3)),
    journal_digest=meta['journal_digest'], method_id=meta['method_id'],
    receipt_sha='51a3a96ccb470902c8c96a4e9cda0cd4ac6a22e271f8d260432d5924b54e9028',
    receipt_bytes=226626,
    tip_hash='7e0c3c88a991d28cc56910a5cfff95cc68b6905ae2db00000000000000000000',
    card_list=cards)
json.dump(bundle, open(OUT, 'w'), separators=(',', ':'))
print(f"{OUT}: {os.path.getsize(OUT)/1024:.0f} KB, {len(cards)} cards, "
      f"{sum(len(c['t']) for c in cards)} trace points")
print(f"  energy {bundle['energy_kwh']} kWh | sites {bundle['sites']} | "
      f"straggler {bundle['chunk_stats']['straggler']}")

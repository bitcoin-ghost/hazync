#!/usr/bin/env python3
"""What a fleet actually cost per proof, accumulated across runs (hazync#448).

⛔ WHY THIS EXISTS. The 4090-only default in #449 rests on **nine runs on one block on one night**:

    all 4090          267.9 / 271.7 / 276.9 s   mean 272.2 s   $0.243
    contains an A40   357.9 / 380.2 / 410.4 s   mean 382.8 s   $0.262

That is enough to justify the default and nowhere near enough to leave unexamined. Nine runs is a
sample, RunPod's pricing moves, and a new card type would have to be argued about from scratch.
Nothing recorded the result of a run in a form that survived the run, so the only way to re-check
the claim was to go back through a session transcript -- which is exactly how the "142 s of
geography variance" mistake happened.

Every finished run now appends ONE line here. The claim gets stronger or it gets overturned, and
either way it is decided on rows rather than recollection.

⚠ THIS RANKS CARD TYPES FOR *SELECTION*, NOT CARDS FOR *KEEPING*. Once a card is rented the money is
spent and the run's wall-clock is set by the fleet, so `tip_lifecycle.rank` keeps the FASTEST cards
and is right to ignore price. Throughput-per-dollar answers the different question of what to rent
next time.
"""
import json
import os
import time

# One JSON object per line. Append-only: a run's outcome is a fact and is never rewritten.
DEFAULT_LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "docs", "history", "fleet-economics.jsonl")


def fleet_key(gpu_types):
    """A stable name for a fleet's composition, e.g. '2x A40 + 1x RTX 4090'.

    ⛔ SORTED AND COUNTED, never the order pods happened to arrive in: the same fleet must produce
    the same key on every run or the rows cannot be grouped, and a mixed fleet must never collide
    with a uniform one -- which is the whole failure this issue is about.
    """
    counts = {}
    for g in gpu_types:
        short = (g or "?").replace("NVIDIA ", "").replace("GeForce ", "").strip() or "?"
        counts[short] = counts.get(short, 0) + 1
    return " + ".join(f"{n}x {g}" for g, n in sorted(counts.items()))


def record(*, block, gpu_types, seconds, usd, ledger=None, now=None):
    """Append one finished run. Returns the row written, or None if it was not worth recording."""
    # ⛔ A RUN THAT DID NOT FINISH TELLS YOU NOTHING ABOUT COST PER PROOF. Recording a failed or
    # zero-length run would drag every mean toward whatever went wrong, and a $0.00 row would make
    # a card type look free. Refuse rather than store a misleading fact.
    if not gpu_types or not seconds or seconds <= 0 or usd is None or usd < 0:
        return None
    row = {
        "t": int(now if now is not None else time.time()),
        "block": str(block),
        "fleet": fleet_key(gpu_types),
        "cards": len(gpu_types),
        "seconds": round(float(seconds), 1),
        "usd": round(float(usd), 4),
        # The two numbers the decision actually turns on. Cost per proof is the one that overturned
        # "the A40 is cheaper": it is $0.49/hr against the 4090's $0.74/hr and still costs MORE.
        "usd_per_proof": round(float(usd), 4),
        "proofs_per_dollar": round(1.0 / float(usd), 2) if float(usd) > 0 else None,
    }
    path = ledger or DEFAULT_LEDGER
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return row


def load(ledger=None):
    path = ledger or DEFAULT_LEDGER
    if not os.path.exists(path):
        return []
    out = []
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        # ⛔ ONE BAD LINE MUST NOT DESTROY THE LEDGER'S VALUE. A partial write from a killed run is
        # skipped, not raised on -- the other rows are still facts.
        except json.JSONDecodeError:
            continue
    return out


def summarise(rows):
    """Group by fleet composition. Returns a list of dicts, best seconds first."""
    by = {}
    for r in rows:
        by.setdefault(r.get("fleet", "?"), []).append(r)
    out = []
    for fleet, rs in by.items():
        secs = sorted(x["seconds"] for x in rs if x.get("seconds"))
        usds = sorted(x["usd"] for x in rs if x.get("usd") is not None)
        if not secs or not usds:
            continue
        out.append({
            "fleet": fleet, "n": len(rs),
            "mean_s": round(sum(secs) / len(secs), 1),
            "min_s": secs[0], "max_s": secs[-1],
            "spread_s": round(secs[-1] - secs[0], 1),
            "mean_usd": round(sum(usds) / len(usds), 4),
        })
    return sorted(out, key=lambda d: d["mean_s"])


def report(rows):
    s = summarise(rows)
    if not s:
        return "no finished runs recorded yet"
    w = max(len(d["fleet"]) for d in s)
    lines = [f"{'fleet':<{w}}  {'n':>3} {'mean_s':>8} {'min':>7} {'max':>7} {'spread':>7} {'mean_$':>8}"]
    for d in s:
        lines.append(f"{d['fleet']:<{w}}  {d['n']:>3} {d['mean_s']:>8.1f} {d['min_s']:>7.1f} "
                     f"{d['max_s']:>7.1f} {d['spread_s']:>7.1f} {d['mean_usd']:>8.3f}")
    # ⚠ Say it out loud when the faster fleet is also the cheaper one. That is the counter-intuitive
    # result this ledger exists to keep honest, and a table alone lets a reader miss it.
    if len(s) > 1 and s[0]["mean_usd"] <= s[-1]["mean_usd"]:
        lines.append("")
        lines.append(f"⇒ the FASTEST fleet ({s[0]['fleet']}) is also no dearer per proof "
                     f"(${s[0]['mean_usd']:.3f} vs ${s[-1]['mean_usd']:.3f}) — "
                     f"ranking on price per HOUR would pick the wrong one")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(report(load(sys.argv[1] if len(sys.argv) > 1 else None)))

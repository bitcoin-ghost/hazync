#!/usr/bin/env python3
"""
Adversarial fuzz harness for the Hazync coordinator's range-chaining seam logic
(`server._frontier_chain`) — the S1/F1/H9 trust boundary in SECURITY.md, where ranges were once
chained on a weaker seam than the guest fold (a false-low-height / weak-flags splice risk).

Threat model: a submitter must have a REAL proof per range, so the boundary metadata
(in_tip/out_tip/in_bhash/out_bhash/range_work/out_leaves) is bound by the STARK — but a resourceful
attacker who can prove alternative histories (a low-difficulty fork, etc.) chooses *which* verified
ranges to submit and what legitimate boundary values they carry. The coordinator must NEVER assemble
those into a frontier that isn't a genuine genesis-anchored, seam-continuous, height-contiguous chain.

Method: a randomized model-checker over a tiny symbolic alphabet (so tips/digests collide often and
the seam logic is actually exercised), driving the REAL DB-backed `_frontier_chain`, checked against
an INDEPENDENT DFS oracle that enumerates every legitimate seam-path from genesis. The real output
must always be one of them. Two extra checks: a pure re-model of the walk must match the real code
(guards model drift), and no input may raise.

Positive control (`--control`): the same oracle against a walk with the H9 height guard REMOVED
(the pre-fix coordinator) must FAIL fast — proving the harness detects the exact splice class and
that the H9/S1 checks are load-bearing.

Usage:
  python3 seam_fuzz.py [N]            # soundness campaign (default N=40000). Silent success.
  python3 seam_fuzz.py --control [N]  # weaken H9, expect the oracle to catch a splice
"""
import os, sys, tempfile, itertools

GEN = "GEN"
# Env MUST be set before importing server (module-level constants capture it).
_tmpdb = tempfile.NamedTemporaryFile(prefix="seamfuzz_", suffix=".db", delete=False)
_tmpdb.close()
os.environ["COORD_DB"] = _tmpdb.name
os.environ["GENESIS_TIP"] = GEN
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # import-safe: HTTP server only starts under __main__
server.init_db()  # creates vranges incl. the in_bhash/out_bhash migration columns

# ------------------------------------------------------------------ deterministic PRNG ----------
def splitmix(state):
    state[0] = (state[0] + 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    z = state[0]
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return z ^ (z >> 31)

def rnd(state, n):
    return splitmix(state) % n

# Small symbolic alphabets so seams MATCH often (that's where the logic lives).
TIPS   = [GEN, "A", "B", "C", "D"]          # includes GEN so ranges can anchor to genesis
BHASH  = ["", "p", "q", "r"]                 # includes "" to exercise the falsy/not-chainable path
WORKS  = ["0", "1", "2", "3", "bad", None]   # includes non-numeric + None (must not crash)
LEAVES = [0, 1, 2, 7, None]

def gen_rows(state):
    """A random set of vrange rows. lo in [1..5], length in {1,2,3} so height-adjacency (lo==hi+1)
    is frequently satisfiable; everything else drawn from the symbolic alphabets.

    Tips are drawn INDEPENDENTLY of lo on purpose: in the real system in_tip == hash(the block's real
    parent) (prevhash_ok), but `lo` is the CLAIMED height, which a prover can lie about for a pre-BIP34
    block (bip34 doesn't bind it). So a range's real chain position (its tip) and its claimed `lo` are
    genuinely decoupled — that IS the false-height splice H9 exists to reject, and the harness must be
    free to generate it. (Tying in_tip to lo would silently delete that whole adversarial class.)"""
    nrows = 1 + rnd(state, 8)
    rows = []
    for k in range(nrows):
        lo = 1 + rnd(state, 5)
        length = 1 + rnd(state, 3)
        hi = lo + length - 1
        rows.append({
            "id": f"r{k}",
            "lo": lo, "hi": hi,
            "in_tip":  TIPS[rnd(state, len(TIPS))],
            "out_tip": TIPS[rnd(state, len(TIPS))],
            "in_bhash":  BHASH[rnd(state, len(BHASH))],
            "out_bhash": BHASH[rnd(state, len(BHASH))],
            "range_work": WORKS[rnd(state, len(WORKS))],
            "out_leaves": LEAVES[rnd(state, len(LEAVES))],
            "ts": float(k),
        })
    return rows

# ------------------------------------------------------------------ pure re-model of the walk ---
def walk(rows, enforce_height=True, enforce_bhash=True):
    """Faithful re-model of server._frontier_chain — MOST-WORK genesis-anchored DAG selection (mirrors
    the cum_work rule). The control (enforce_height=False) drops the H9 height guard so the soundness
    oracle can prove it still catches a height splice. Returns ((hi, tip_hash, cum_work, leaves), None)."""
    def wp(x):
        try: return int(x or 0)
        except Exception: return 0
    rr = [dict(r) for r in rows]
    for i, r in enumerate(rr):
        r["_i"] = i
    by_out = {}
    for r in rr:
        by_out.setdefault(r["out_tip"], []).append(r)
    best, frontier = {}, (0, GEN, 0, 0)
    for r in sorted(rr, key=lambda x: (x["lo"], x["ts"])):
        if not r["in_bhash"]:
            continue
        if r["in_tip"] == GEN and (not enforce_height or r["lo"] == 1):
            cw = wp(r["range_work"])
        else:
            bp = None
            for p in by_out.get(r["in_tip"], ()):
                if ((not enforce_bhash or str(p["out_bhash"]) == str(r["in_bhash"]))
                        and (not enforce_height or p["hi"] + 1 == r["lo"])
                        and p["_i"] in best):
                    if bp is None or best[p["_i"]] > bp:
                        bp = best[p["_i"]]
            if bp is None:
                continue
            cw = bp + wp(r["range_work"])
        best[r["_i"]] = cw
        if cw > frontier[2] or (cw == frontier[2] and r["hi"] > frontier[0]):
            frontier = (r["hi"], r["out_tip"], cw, r["out_leaves"] or 0)
    return frontier, None

# ------------------------------------------------------------------ independent DFS oracle ------
def legit_end_tuples(rows):
    """Every (hi, out_tip, cum_work, leaves) reachable by a genuine genesis-anchored, seam-continuous,
    height-contiguous SIMPLE path. Independent of the linear first-wins walk, so a splice / cycle /
    mis-anchor in the real code yields a tuple absent from this set. Valid seams strictly increase
    `lo`, so the seam graph is a DAG — DFS terminates."""
    def wparse(x):
        try: return int(x or 0)
        except Exception: return 0
    def vseam(a, b):
        return (b["in_tip"] == a["out_tip"] and str(b["in_bhash"]) == str(a["out_bhash"])
                and b["lo"] == a["hi"] + 1 and bool(b["in_bhash"]))
    def gstart(r):
        return r["in_tip"] == GEN and r["lo"] == 1 and bool(r["in_bhash"])

    out = {(0, GEN, 0, 0)}  # the empty frontier is always legitimate

    def dfs(node, used, work_acc):
        w = work_acc + wparse(node["range_work"])
        out.add((node["hi"], node["out_tip"], w, node["out_leaves"] or 0))
        for nb in rows:
            if id(nb) in used:
                continue
            if vseam(node, nb):
                dfs(nb, used | {id(nb)}, w)

    for r in rows:
        if gstart(r):
            dfs(r, {id(r)}, 0)
    return out

# ------------------------------------------------------------------ real DB-backed driver -------
_conn = server.db()
def real_frontier(rows):
    """Load rows into the real vranges table and call the REAL _frontier_chain()."""
    _conn.execute("DELETE FROM vranges")
    _conn.executemany(
        "INSERT INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work,in_bhash,out_bhash)"
        " VALUES(:id,:lo,:hi,:in_tip,:out_tip,'','',:ts,:out_leaves,:range_work,:in_bhash,:out_bhash)",
        rows)
    _conn.commit()
    return server._frontier_chain()

# ------------------------------------------------------------------ campaign --------------------
def main():
    args = [a for a in sys.argv[1:]]
    control = "--control" in args
    nums = [a for a in args if a.isdigit()]
    N = int(nums[0]) if nums else 40000

    state = [0x5EED_C0DE_1234_5678]
    fails = 0
    for i in range(N):
        rows = gen_rows(state)

        # 1) The real DB-backed code must never raise.
        try:
            real = tuple(real_frontier(rows))
        except Exception as e:
            print(f"[CRASH] _frontier_chain raised on rows={rows}\n  {type(e).__name__}: {e}")
            fails += 1
            if fails > 5: break
            continue

        # 2) Pure re-model must match the real code (guards model drift).
        model, _ = walk(rows, enforce_height=True, enforce_bhash=True)
        if model != real:
            print(f"[MODEL-DRIFT] pure walk {model} != real {real}\n  rows={rows}")
            fails += 1
            if fails > 5: break
            continue

        # 3) SOUNDNESS: the coordinator must report the MOST-WORK legitimate genesis-anchored chain.
        legit = legit_end_tuples(rows)
        if control:
            target, _ = walk(rows, enforce_height=False)
            if tuple(target) not in legit:  # the no-H9 walk produced an illegitimate frontier — oracle catches it
                print(f"[CONTROL-CAUGHT (expected)] no-H9 frontier {tuple(target)} is NOT a legitimate chain")
                fails += 1
                if fails > 50:
                    break
        else:
            if tuple(real) not in legit:
                print(f"[SOUNDNESS VIOLATION] frontier {tuple(real)} is NOT a legitimate genesis-anchored chain")
                print(f"  rows={rows}\n  legit={sorted(legit)}")
                fails += 1
                if fails > 5:
                    break
            elif (real[2], real[0]) != max((t[2], t[0]) for t in legit):  # must be the MAX-work legit chain
                print(f"[SOUNDNESS VIOLATION] frontier {tuple(real)} is not the most-work chain; "
                      f"best (work,hi)={max((t[2], t[0]) for t in legit)}")
                print(f"  rows={rows}\n  legit={sorted(legit)}")
                fails += 1
                if fails > 5:
                    break

    mode = "CONTROL (H9 height guard removed)" if control else "REAL _frontier_chain"
    print(f"\n{mode}: {N} scenarios, {fails} findings.")
    if control:
        print("Control PASSES iff findings > 0 (the harness must catch the pre-H9 splice).")
        sys.exit(0 if fails > 0 else 1)
    else:
        print("Soundness PASSES iff findings == 0.")
        sys.exit(0 if fails == 0 else 1)

if __name__ == "__main__":
    try:
        main()
    finally:
        try: os.remove(_tmpdb.name)
        except Exception: pass

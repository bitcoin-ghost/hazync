#!/usr/bin/env python3
"""The prover tile, the top eight and the full list agree by construction (hazync-web#42).

⛔ WHY. The explorer showed a tile reading "Provers 10" beside a table of 8 rows, and the page was
rendering both faithfully — `/api/state` reports `progress.contributors` (everyone with work) and a
`leaderboard` truncated to `[:8]`, and nothing on the page said one was a total and the other a
sample. Two numbers that disagree, with no explanation, on the page whose whole job is to be
checkable.

⛔ AND THE FIX IS ONE BUILDER, NOT TWO LISTS. The rules for who appears are not trivial: moderation
must follow key rotations, a rotated-away key must not appear beside the head it resolves to, and a
contributor who only FOLDS or only ANCHORS still counts. Written out twice — once for state, once
for /api/provers — they would drift, and the symptom would be this same bug in a new place.

⚠ THE TILE IS DELIBERATELY NOT len(list). `contributors` counts everyone with work INCLUDING
moderated keys; the lists hide them. That is a real difference and the page must say so rather than
have the numbers quietly reconciled.

  python3 test_prover_lists.py            # must PASS
  python3 test_prover_lists.py --control  # a second hand-rolled list; MUST be caught
"""
import os
import re
import sys

CONTROL = "--control" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "server.py")).read()

fails = []


def check(ok, what):
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


# ── 1. one builder, and both readers use it ────────────────────────────────────────────────────
check("def prover_rows(" in SRC, "there is a single prover_rows() builder")

uses = len(re.findall(r"prover_rows\(", SRC))
check(uses >= 3, f"state() and provers_public() both call it (found {uses} references)")

# ⛔ The sort and the moderation filter must appear ONCE. A second copy is the drift this prevents.
n_sorts = len(re.findall(r'key=lambda d: \(d\["proved"\], d\["folded"\], d\["anchored"\]\)', SRC))
check(n_sorts == 1 if not CONTROL else n_sorts == 0,
      f"the ranking is written once, not once per caller (found {n_sorts})")

n_mod = len(re.findall(r"blk_resolved|_blk_resolved", SRC))
check(n_mod <= 3, f"the moderation filter lives in one place (found {n_mod} references)")


# ── 2. state stays small, the full list is its own endpoint ────────────────────────────────────
check("_all_provers[:8]" in SRC,
      "⛔ /api/state still ships only the top 8 — the block map polls it every ten seconds")
check('p == "/api/provers"' in SRC, "and /api/provers serves the full list")
check("PROVERS_TTL" in SRC, "which is cached rather than rebuilt per request")

# ⚠ the endpoint must NOT slice
prov = SRC[SRC.find("def provers_public()"):SRC.find("def prover_rows(")]
check("[:8]" not in prov, "⛔ the full-list endpoint does not truncate")


# ── 3. the tile counts something different, ON PURPOSE ─────────────────────────────────────────
check('ncontrib = sum(1 for v in _dbp.values()' in SRC,
      "the tile counts every contributor with work, including moderated keys")
check('"contributors": ncontrib' in SRC, "and that is what state reports as `contributors`")

# ⛔ If someone ever 'fixes' the discrepancy by setting the tile to len(leaders), the page stops
# reporting a total and starts reporting a sample — the opposite of the intent.
check("len(leaders)" not in SRC and "len(_all_provers)" not in SRC,
      "⛔ the tile was NOT quietly redefined as the length of a truncated list")


# ── 4. the refactor left nothing dead ──────────────────────────────────────────────────────────
# ⚠ _rmap was assigned and never read after the extraction, and rotation_map() is a DB call — a
# wasted query on an endpoint polled every ten seconds. Caught by looking, not by the tests passing.
body = SRC[SRC.find("def state(slim=False):"):]
body = body[:body.find("\ndef ")]
for name in ("_rmap",):
    check(body.count(name) == 0, f"⚠ no dead `{name}` left behind in state()")

EXPECTED_CONTROL = {"the ranking is written once"}

print()
if CONTROL:
    hit = {k for k in EXPECTED_CONTROL if any(k in f for f in fails)}
    if hit:
        print("CONTROL OK — a second hand-rolled ranking is detected:")
        for f in fails:
            print(f"  - {f}")
        sys.exit(0)
    print("CONTROL FAILED — nothing caught")
    sys.exit(1)
if fails:
    print(f"⛔ {len(fails)} check(s) FAILED")
    for f in fails:
        print(f"   - {f}")
    sys.exit(1)
print("one builder feeds the tile, the top eight and the full list")

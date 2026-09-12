#!/usr/bin/env python3
"""
#281: a wide range that starts INSIDE existing coverage without being backed by it must be refused.

WHY THIS FILE EXISTS, stated as the failure it is here to prevent:

On 2026-09-11 a contributor submitted 50-block chunks with INCLUSIVE upper bounds, so each one began
on the block its predecessor ended on: [30000..30050], then [30050..30100], then [30100..30149]. Every
one of those proofs is valid and every one verified. But `_frontier_chain` needs `prev.hi + 1 == lo`
exactly (H9), so from the second chunk onward none of them could ever join the genesis chain — and
because `claim()` is COVERAGE-based, block 30,051 then looked proven and was never handed out again.
The board froze at 30,050 for thirteen hours while `proven` kept climbing, `failed` stayed empty and
`stalled_for` reported 0, because a VERIFIED row sat directly over the blocker.

Nothing in the system said a word. The coordinator already owned the rule that made those bounds
illegal; it just never ran it at submit time, only when drawing the board.

WHAT THIS DOES NOT COVER, so nothing reads as more assurance than it is:
  * Real STARK verification — VERIFY_MODE=mock, as in test_spine_fold.py. The guard runs BEFORE
    verification and is independent of it, but these tests cannot tell a real proof from a forged one.
  * The frontier itself. Mock receipts cannot be genesis-anchored (`is_genesis_anchored` needs the real
    GENESIS_TIP), so `frontier_hi()` is 0 here. What IS asserted is the property that actually broke:
    the gap block must stay UNCOVERED, because that is the thing `claim()` reads.

Usage:
  python3 test_overlap_guard.py            # assertions; exit 0 on success
  python3 test_overlap_guard.py --control  # remove the guard; these tests MUST then fail
"""
import base64
import os
import sys
import tempfile

CONTROL = "--control" in sys.argv

_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["COORD_DB"] = _tmpdb.name
os.environ["COORD_SPINE"] = tempfile.mkdtemp(prefix="spine_")
os.environ["COORD_PROOFS"] = tempfile.mkdtemp(prefix="proofs_")
os.environ["VERIFY_MODE"] = "mock"
os.environ["COORD_ALLOW_MOCK"] = "1"
os.environ["TIP_CACHE_TTL"] = "0"
os.environ.setdefault("COORD_WEB", os.path.dirname(__file__))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402

server.init_db()

fails = []


def check(cond, what):
    print(f"  {'ok  ' if cond else 'FAIL'} {what}")
    if not cond:
        fails.append(what)


# A real signature: HAVE_ED is true wherever `cryptography` is installed, and submit() will not accept
# an unsigned receipt on a board that can verify one. Signing for real also keeps this test honest if
# the signature check ever moves relative to the guard.
if server.HAVE_ED:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    _sk = Ed25519PrivateKey.generate()
    PUB = _sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw).hex()
    _sign = lambda m: _sk.sign(m).hex()                                            # noqa: E731
else:
    os.environ["COORD_ALLOW_UNSIGNED"] = "1"
    PUB, _sign = "00" * 32, lambda m: "00" * 64                                    # noqa: E731


def submit(lo, hi):
    """Drive the real submit() end to end. Returns (code, obj)."""
    rid = str(lo) if lo == hi else f"{lo}-{hi}"
    receipt = f"receipt-for-{rid}".encode()
    return server.submit({"range": rid, "pubkey": PUB, "handle": "tester",
                          "sig": _sign(receipt), "receipt": base64.b64encode(receipt).decode()})


def reset():
    c = server.db()
    c.execute("DELETE FROM vranges")
    c.execute("DELETE FROM ranges")
    c.commit()
    c.close()


def covered():
    """Exactly what claim() reads: the union of every verified range's heights."""
    c = server.db()
    out = set()
    for row in c.execute("SELECT lo,hi FROM vranges"):
        out.update(range(row["lo"], row["hi"] + 1))
    c.close()
    return out


if CONTROL:
    # Remove the guard the way it was absent before #281 — no overlap is ever found, so every range is
    # accepted. If the assertions below still pass with this in place, they are not testing anything.
    server._overlapping_vrange = lambda c, lo, hi: None

# ── the incident, reproduced exactly ──────────────────────────────────────────────────────────────
print("== the 30,050 freeze ==")
reset()

code, _ = submit(30000, 30050)
check(code == 200, "the first chunk [30000..30050] lands — it overlaps nothing")

code, obj = submit(30050, 30100)
check(code == 409, "the overlapping chunk [30050..30100] is REFUSED, not verified")
check("30051" in str(obj.get("error", "")),
      "the error names the bound the contributor should have used (30051)")
check("INCLUSIVE" in str(obj.get("error", "")),
      "the error says why the bounds were wrong, not just that they were")

# And here is the part that makes the whole thing self-heal. With the bad chunk refused, the NEXT one
# no longer overlaps anything, so it is accepted on its own merits — and the blocks between become an
# ordinary HOLE. A hole is the benign failure: claim() hands it straight out and the fleet fills it.
# The original bug was never "a gap"; it was a gap that LOOKED FULL.
code, _ = submit(30100, 30149)
check(code == 200, "the chunk after it is accepted — with the bad one refused it overlaps nothing")

# The property that actually broke. claim() is coverage-based, so the ONE thing that must stay true is
# that the blocks the frontier needs are still open for someone to take.
gap = covered()
check(30051 not in gap and 30099 not in gap,
      "the blocks between stay an ordinary HOLE, uncovered, so claim() hands them out")

# ── and the correct bounds still work ─────────────────────────────────────────────────────────────
print("== what must still be accepted ==")
reset()
check(submit(30000, 30050)[0] == 200, "chunk one lands")
check(submit(30051, 30100)[0] == 200, "the CORRECT next chunk [30051..30100] is accepted")

reset()
check(submit(500, 599)[0] == 200, "a wide range over fresh territory is accepted — it overlaps nothing")

# A genuine fold re-expresses blocks the board already holds, so it is tiled by its own children.
reset()
for h in range(100, 104):
    submit(h, h)
check(submit(100, 103)[0] == 200, "a fold whose leaves are all on the board is accepted")

reset()
for h in (100, 101):
    submit(h, h)
check(submit(100, 103)[0] == 409, "a 'fold' whose later leaves are missing is refused — it is not backed")

# Width 1 is the one shape that can never create this trap, so the guard must not touch it: a block
# legitimately appears both inside a wide range and as a standalone proof (see proven_count).
reset()
submit(100, 103)
check(submit(101, 101)[0] == 200, "a single block inside an existing wide range is still accepted")

# ── a board that ALREADY holds a bad range must still be repairable ───────────────────────────────
# Not hypothetical, and worth being blunt about: the guard refuses the wide REPAIR range too, because
# it overlaps the bad one. Replayed against the live board on 2026-09-12, it refuses 5 of 4,783 wide
# ranges -- the three bad ones AND the two the operator proved to bridge past them.
#
# That is survivable because repair happens the way a frontier repair should happen anyway: as LEAVES,
# which the guard never touches at all. Once the leaves are on the board, the fold over them is backed,
# so it is accepted too. Nothing is unreachable; the wide shortcut is what goes away.
print("== a board with a bad range on it is still repairable ==")
reset()
submit(30000, 30050)
_c = server.db()           # a bad range that predates this guard, inserted the way it landed in 2026-09
_c.execute("INSERT OR REPLACE INTO vranges(id,lo,hi,in_tip,out_tip,pubkey,handle,ts,out_leaves,range_work)"
           " VALUES('30050-30100',30050,30100,'in','out','','bip-448',0,0,'0')")
_c.commit(); _c.close()

check(submit(30051, 30100)[0] == 409, "the WIDE repair is refused — it overlaps the bad range")
_landed = all(submit(h, h)[0] == 200 for h in range(30051, 30101))
check(_landed, "the repair still goes through one block at a time — leaves are never refused")
check(submit(30051, 30100)[0] == 200, "and once the leaves are there, the fold over them is accepted")

# --- the half that costs nothing: SAY THE SPAN BEFORE PROVING IT --------------------------------
# The guard above refuses the bad bounds, but only after the proving is paid for. What actually
# protects a contributor's GPU time is being told what they asked for while it is still free to fix.
# `hazync run 30000-30050` proves FIFTY-ONE blocks and reads as fifty to almost everyone; "51 blocks"
# reads as fifty-one to everyone. Asserted against the CLI source, because coordinator/hazync has no
# .py extension and is not importable.
import re as _re
_cli = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hazync")).read()
_run = _cli[_cli.index("def cmd_run("):]
_run = _run[:_run.index("\ndef ")]
check("inclusive" in _run, "hazync run states that the span is INCLUSIVE")
check(_re.search(r'hi - lo \+ 1', _run) is not None,
      "  ...and prints the BLOCK COUNT, which is what makes an off-by-one visible")
# Before the witness probe, which is before the prove: a warning after the spend is a receipt.
_probe = _run.find("has no witness")
_say = _run.find("inclusive")
check(_say != -1 and _probe != -1 and _say < _probe,
      "  ...before anything expensive starts, not after")

# The message itself, driven rather than eyeballed: the real bounds from 2026-09-11.
def _span_line(lo, hi):
    n = hi - lo + 1
    return (f"proving {n} block{'' if n == 1 else 's'}: {lo}..{hi} inclusive"
            + ("" if n == 1 else f" — the next range starts at {hi + 1}"))
check("51 blocks" in _span_line(30000, 30050),
      "30000-30050 is announced as 51 blocks, the count that was misread")
check("starts at 30051" in _span_line(30000, 30050),
      "  ...and names where the next range begins, which is the bound that was wrong")
check(_span_line(500000, 500000).endswith("inclusive") and "1 block:" in _span_line(500000, 500000),
      "a single block says '1 block' and offers no next-range advice")

print()
if CONTROL:
    if fails:
        print(f"CONTROL OK — removed the overlap guard and {len(fails)} assertion(s) failed, as they must.")
        sys.exit(0)
    print("CONTROL FAILED — the guard was removed and every test still passed.")
    print("These tests cannot detect the thing they exist to detect.")
    sys.exit(1)

if fails:
    print(f"{len(fails)} failure(s).")
    sys.exit(1)
print("overlapping ranges are refused; folds, fresh territory and single blocks are not.")

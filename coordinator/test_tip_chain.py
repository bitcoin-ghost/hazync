#!/usr/bin/env python3
"""A tip bundle is proved only if it is the next link in the chain we already proved (hazync#556).

⛔ WHAT THIS EXISTS FOR. On 2026-09-28 the session asked `highest_tip_bundle` what to prove next.
968,984 and 968,985 were mined 5 seconds apart, both bundles landed at 13:24:59, the run took 985,
and 984 has no proof and never will. Nothing refused it, because nothing ever asked whether the
block about to be proved was the child of the one before it.

    968,983  appeared 12:11:21Z  accepted 12:17:12Z  blocks_behind: 0
    968,985  appeared 12:24:59Z  accepted 12:26:41Z  blocks_behind: 0
             ^ 968,984 never appears in the ledger AT ALL

The product is a CHAIN of proofs. A gap is a missing link, not a slower result, and it was invisible
in all four artifacts: the session summary, the dashboard grid, the run log, and the tip ledger built
to be the audit trail.

⚠ A height comparison alone would have caught that one case. This checks the real linkage, because
two worse faults produce a proof that does not chain and look like ordinary work while doing it: a
bundle whose filename and content disagree, and a right-height block on the wrong chain.

    python3 test_tip_chain.py             # gaps, mislabels and forks are refused
    python3 test_tip_chain.py --control   # no linkage check — the gap must be accepted
"""
import hashlib
import json
import os
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tip_chain as C  # noqa: E402

CONTROL = "--control" in sys.argv
fails = 0


def check(ok, what):
    global fails
    print("  " + ("ok   " if ok else "FAIL ") + what)
    if not ok:
        fails += 1


def header(parent, *, nonce=0):
    """A structurally real 80-byte header: version | parent | merkle | time | bits | nonce."""
    return (struct.pack("<I", 0x20000000) + bytes(parent) + bytes(32)
            + struct.pack("<I", 1790594662) + struct.pack("<I", 386014917)
            + struct.pack("<I", nonce))


def bundle(height, parent, *, hdr=None, in_tip="match"):
    h = hdr if hdr is not None else header(parent)
    b = {"height": height, "witness": {"header": list(h)}}
    if in_tip == "match":
        b["in_tip"] = list(h[4:36])
    elif in_tip is not None:
        b["in_tip"] = list(in_tip)
    return b


# ── a three-block chain: 983 -> 984 -> 985 ───────────────────────────────────────────────────────
GENESIS_PARENT = bytes(range(32))
h983 = header(GENESIS_PARENT, nonce=983)
b983_hash = C.block_hash(h983)
h984 = header(b983_hash, nonce=984)
b984_hash = C.block_hash(h984)
h985 = header(b984_hash, nonce=985)

B984 = bundle(968984, b983_hash, hdr=h984)
B985 = bundle(968985, b984_hash, hdr=h985)


def accepts(b, expect_height, prev_header):
    """The decision as the session makes it."""
    if CONTROL:
        return True                      # the old behaviour: prove whatever the selector handed us
    return C.verdict(b, expect_height=expect_height, prev_header=prev_header)["ok"]


# ── 1. the pure parts ────────────────────────────────────────────────────────────────────────────
if not CONTROL:
    check(len(h984) == 80, "a header is 80 bytes")
    check(C.bundle_parent(B985) == b984_hash, "the parent is read from the HEADER, not from in_tip")
    check(C.display_hash(b984_hash) == b984_hash[::-1].hex(),
          "display order is the reverse of internal order")
    # ⛔ The byte-order trap: comparing display against internal is False for ever and reads as a fork.
    check(C.bundle_parent(B985) != b984_hash[::-1],
          "internal and display order are NOT interchangeable")
    try:
        C.block_hash(b"short")
        check(False, "a short header should raise")
    except ValueError:
        check(True, "a header of the wrong length raises rather than hashing nonsense")

# ── 2. the good case ─────────────────────────────────────────────────────────────────────────────
check(accepts(B985, 968985, h984) is True, "the true successor is proved")
if not CONTROL:
    v = C.verdict(B985, expect_height=968985, prev_header=h984)
    check(v["linked"] is True, "and it is recorded as LINKED")

# ── 3. the gap — the 2026-09-28 fault, exactly ───────────────────────────────────────────────────
# We proved 968,983. The selector hands us 968,985, whose parent is 968,984 — which we never proved.
got = accepts(B985, 968985, h983)
if CONTROL:
    check(got is True,
          "control reproduces it: 968,985 is accepted although 968,984 was never proved — the gap")
else:
    check(got is False, "a bundle whose parent we never proved is REFUSED as a gap")
    v = C.verdict(B985, expect_height=968985, prev_header=h983)
    check("GAP" in v["why"] or "gap" in v["why"], f"and the reason says so: {v['why'][:70]}")

# ── 4. a mislabelled bundle: the name says one height, the content another ───────────────────────
mis = bundle(968984, b984_hash, hdr=h985)          # content is 985, offered as 984
got = accepts(mis, 968984, h983)
if CONTROL:
    check(got is True, "control: a bundle whose content contradicts its name is proved anyway")
else:
    check(got is False, "a bundle whose stated height differs from the one asked for is REFUSED")

# ── 5. a bundle inconsistent with itself ─────────────────────────────────────────────────────────
if not CONTROL:
    bad = bundle(968985, b984_hash, hdr=h985, in_tip=bytes(32))
    v = C.verdict(bad, expect_height=968985, prev_header=h984)
    check(v["ok"] is False and "in_tip" in v["why"],
          "in_tip disagreeing with the header's own parent is refused")

# ── 6. the first tip block of a session: allowed, but NOT claimed as linked ──────────────────────
if not CONTROL:
    v = C.verdict(B985, expect_height=968985, prev_header=None)
    check(v["ok"] is True, "the first tip block of a session may be proved")
    check(v["linked"] is False,
          "but it is NOT reported as linked — an unverifiable link must not look like a verified one")
    check("first tip block" in v["why"], f"and the log says why: {v['why'][:60]}")
    # the node can supply the hash when we did not prove the parent ourselves
    v2 = C.verdict(B985, expect_height=968985, parent_hash=b984_hash)
    check(v2["ok"] and v2["linked"], "a parent hash from the node links it just as well")

# ── 7. against the REAL mainnet bundle, when this box has one ────────────────────────────────────
# ⚠ Opportunistic: CI has no bundle. The synthetic headers above are the contract; this guards
# against them being unrepresentative of what the bridge actually writes.
real = os.environ.get("HAZYNC_TEST_BUNDLE")
if real and os.path.exists(real) and not CONTROL:
    d = json.load(open(real))
    hdr = C.bundle_header(d)
    check(hdr is not None and len(hdr) == 80, f"real bundle {os.path.basename(real)} has an 80-byte header")
    check(bytes(d["in_tip"]) == C.bundle_parent(d), "real bundle: in_tip == header parent")
    check(C.verdict(d, expect_height=d["height"], parent_hash=C.bundle_parent(d))["linked"] is True,
          "real bundle links against its own stated parent")
elif not CONTROL:
    print("  --   no HAZYNC_TEST_BUNDLE set; skipping the real-bundle cross-check")

print()
if fails:
    print("FAIL: " + str(fails) + " assertion(s)")
    sys.exit(1)
print("PASS (" + ("control" if CONTROL else "real") + ")")

"""Is this bundle the next link in the chain we have already proved? (hazync#556)

⛔ WHY A HEIGHT COMPARISON IS NOT ENOUGH. The obvious check is `height == last + 1`, and it would
have caught the 2026-09-28 skip. It would not catch the two faults that cost more if they ever
happen: a bundle whose FILENAME and CONTENT disagree, and a bundle that is the right height on the
WRONG chain. Both look like ordinary work and both produce a proof that does not chain.

The bundle already carries what is needed to settle it properly. Measured against mainnet
2026-09-28 on `bundle_968985.json`:

    in_tip              == witness.header[4:36]   (the parent, internal byte order)
    reversed(in_tip)    == bitcoin-cli getblockhash 968984
                        == 000000000000000000006b00bca1842bc56bb0ef58db54407b74b1488da44946

So a bundle states which block it builds on, and we hold the previous block's header the moment we
have proved it. Hashing that header and comparing is a real linkage test: it says "this is the child
of the thing we just proved", which is the property the product actually sells.

⚠ BYTE ORDER. Everything here is INTERNAL order -- the order a header stores its parent in. Display
order is the reverse, and is produced only by `display_hash` for humans. Mixing them yields a
comparison that is False for ever, which reads exactly like a fork. (Ghost pool learned this one the
hard way with share_hash.)

⚠ The FIRST tip block of a session has no predecessor we proved, so it cannot be linked to one. That
is reported as `linked=False` with a reason, never as a pass -- an unverifiable link and a verified
one must not look the same in the log.
"""
import hashlib

HEADER_LEN = 80
PARENT_SLICE = slice(4, 36)


def block_hash(header):
    """Double-SHA256 of an 80-byte header, in INTERNAL byte order."""
    b = bytes(header)
    if len(b) != HEADER_LEN:
        raise ValueError(f"a block header is {HEADER_LEN} bytes, got {len(b)}")
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def display_hash(h):
    """The reversed hex a block explorer shows. For humans only -- never compare on this."""
    return bytes(h)[::-1].hex()


def bundle_height(bundle):
    h = bundle.get("height")
    return int(h) if isinstance(h, int) or (isinstance(h, str) and h.isdigit()) else None


def bundle_header(bundle):
    """The raw 80-byte header, or None if the bundle does not carry a usable one."""
    w = bundle.get("witness")
    hdr = w.get("header") if isinstance(w, dict) else None
    if hdr is None:
        return None
    try:
        b = bytes(hdr)
    except (TypeError, ValueError):
        return None
    return b if len(b) == HEADER_LEN else None


def bundle_parent(bundle):
    """The block this bundle builds on, internal order.

    ⚠ Read from the HEADER, not from `in_tip`. They agree today (verified on mainnet), but the
    header is the part that is hashed into the proof -- so if the two ever disagree, the header is
    the one that is true and `in_tip` is the one that is wrong.
    """
    hdr = bundle_header(bundle)
    return hdr[PARENT_SLICE] if hdr else None


def verdict(bundle, *, expect_height, prev_header=None, parent_hash=None):
    """Whether this bundle may be proved as the successor we think it is.

    `prev_header` is the 80 bytes of the block we proved last; `parent_hash` is that block's hash in
    internal order, for a caller that has the hash but not the header (the node can supply it).
    Returns {"ok", "linked", "why", "height", "parent"}.

    ⛔ `ok` AND `linked` ARE DIFFERENT QUESTIONS. `ok=False` means do not prove this. `linked=False`
    means we proved it without being able to check the link -- true for the first block of every
    session, and it must be visible rather than rounded up to a pass.
    """
    out = {"ok": False, "linked": False, "why": "", "height": None, "parent": None}

    if not isinstance(bundle, dict):
        out["why"] = "the bundle is not an object"
        return out

    got_h = bundle_height(bundle)
    out["height"] = got_h
    if got_h is None:
        out["why"] = "the bundle states no height"
        return out

    # ⛔ THE FILENAME IS NOT EVIDENCE. `bundle_<h>.json` is a name someone can get wrong; the height
    # inside it is what the proof commits to. A disagreement means we are about to prove a block
    # while telling the board it is a different one.
    if int(expect_height) != got_h:
        out["why"] = (f"the bundle NAMED {int(expect_height)} states height {got_h} — refusing to "
                      f"prove a block under the wrong height")
        return out

    hdr = bundle_header(bundle)
    if hdr is None:
        out["why"] = f"no usable {HEADER_LEN}-byte header in the bundle"
        return out

    parent = hdr[PARENT_SLICE]
    out["parent"] = display_hash(parent)

    # ⚠ in_tip is a convenience field; disagreeing with the header means the bundle is inconsistent
    # with itself and nothing downstream should trust either value.
    it = bundle.get("in_tip")
    if it is not None:
        try:
            if bytes(it) != parent:
                out["why"] = ("the bundle's in_tip disagrees with its own header's parent — the "
                              "bundle is internally inconsistent")
                return out
        except (TypeError, ValueError):
            out["why"] = "the bundle's in_tip is not bytes"
            return out

    want = None
    if prev_header is not None:
        try:
            want = block_hash(prev_header)
        except ValueError as e:
            out["why"] = f"the previous header is unusable: {e}"
            return out
    elif parent_hash is not None:
        want = bytes(parent_hash)

    if want is None:
        # Nothing to link against. Allowed, and said plainly.
        out["ok"] = True
        out["why"] = (f"height {got_h} is consistent within the bundle, but there is no previously "
                      f"proved block to link it to (first tip block of the session)")
        return out

    if parent != want:
        out["why"] = (f"block {got_h} builds on {display_hash(parent)} but the block we proved last "
                      f"hashes to {display_hash(want)} — this is a GAP or a fork, not the next link")
        return out

    out["ok"] = True
    out["linked"] = True
    out["why"] = f"block {got_h} is the child of the block we proved last ({display_hash(want)[:16]}…)"
    return out

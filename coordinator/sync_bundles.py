#!/usr/bin/env python3
"""
Seed a coordinator's bundle directory from a peer (hazync#69).

The alternative to this is resyncing an ~865 GB archive node with `txindex=1` and re-running the
bridge from genesis. That is still the trustless option and it is the right one if you have the disk;
this exists because "or spend a week resyncing" is not a real answer for someone who wants to stand up
a second coordinator today.

WHAT THIS DOES NOT GIVE YOU, first, because the distinction matters:

  Bundles are WITNESS DATA, not proofs. Nothing here is verified and nothing here is trusted — a
  bundle that is wrong produces a proof that fails, wasting your GPU time and nobody else's. The
  actual security boundary is the guest: a receipt verifies against METHOD_ID regardless of who
  supplied the witness it was built from. So a hostile peer can waste your electricity and cannot
  make you prove something false.

  If you want the stronger property — that the bundles are what an archive node would have produced —
  run your own node. That is what `docs/RUN_YOUR_OWN_COORDINATOR.md` describes and this does not
  replace it.

RESUMABLE BY DESIGN. It walks in chunks and skips heights already present, so an interrupted run is
re-run, not restarted. At ~73 GB total that is not a nicety.

Usage:
  python3 sync_bundles.py https://peer.example/hazync ./bundles --from 1 --to 220000
  python3 sync_bundles.py https://peer.example/hazync ./bundles --from 1 --to 220000 --dry-run
"""
import argparse, io, json, os, sys, tarfile, time, urllib.request, urllib.error


def fetch_chunk(peer, frm, count, timeout):
    url = f"{peer}/api/witnesses?from={frm}&count={count}"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def extract(blob, out_dir):
    """Extract one chunk. Returns (written, manifest).

    Parsing to the end-of-archive marker is the completeness check: the coordinator speaks HTTP/1.0,
    so a response without Content-Length ends at connection close and a truncated transfer would
    otherwise be indistinguishable from a complete one. `tarfile` raises on a short archive.
    """
    tf = tarfile.open(fileobj=io.BytesIO(blob), mode="r")
    members = tf.getmembers()
    manifest = None
    written = 0
    for m in members:
        if m.name == "MANIFEST.json":
            manifest = json.loads(tf.extractfile(m).read())
            continue
        # Never trust an archive's member names with a path. A peer-supplied "../../etc/x" would
        # otherwise write outside out_dir — the classic tar traversal, and this archive comes from
        # someone else's server by definition.
        name = os.path.basename(m.name)
        if not name.startswith("bundle_") or not name.endswith(".json"):
            print(f"  skipping unexpected member {m.name!r}", file=sys.stderr)
            continue
        dst = os.path.join(out_dir, name)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(tf.extractfile(m).read())
        os.replace(tmp, dst)      # atomic: a killed run never leaves a half-written bundle behind
        written += 1
    if manifest is None:
        raise SystemExit("chunk had no MANIFEST.json — that peer is not serving #69's format")
    return written, manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("peer", help="peer coordinator base URL")
    ap.add_argument("out_dir", help="bundle directory (HAZYNC_BRIDGE_OUT)")
    ap.add_argument("--from", dest="frm", type=int, required=True)
    ap.add_argument("--to", dest="to", type=int, required=True)
    ap.add_argument("--chunk", type=int, default=1000, help="heights per request (peer may cap lower)")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true", help="report what is missing, download nothing")
    a = ap.parse_args()

    peer = a.peer.rstrip("/")
    os.makedirs(a.out_dir, exist_ok=True)
    total_written = total_missing = 0
    t0 = time.time()

    h = a.frm
    while h <= a.to:
        count = min(a.chunk, a.to - h + 1)
        # Skip what is already on disk. This is what makes an interrupted run resumable rather than
        # restartable, and at 73 GB the difference is days.
        need = [x for x in range(h, h + count)
                if not os.path.exists(os.path.join(a.out_dir, f"bundle_{x}.json"))]
        if not need:
            h += count
            continue
        if a.dry_run:
            print(f"  {h}..{h + count - 1}: {len(need)} missing")
            total_missing += len(need)
            h += count
            continue
        # Ask only for the span that is actually missing, not the whole chunk. On the ordinary resume
        # (everything up to X present, nothing after) this is the same request; where it differs is a
        # HOLE in the middle, which without this refetches every bundle either side of it. Measured on
        # a 20-height chunk with three missing: 20 files rewritten instead of 3.
        req_from, req_count = need[0], need[-1] - need[0] + 1
        try:
            blob = fetch_chunk(peer, req_from, req_count, a.timeout)
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf8", "replace")
            if e.code == 400 and "BULK_MAX" in body:
                # The peer caps chunks lower than we asked. Halve and retry rather than fail — the cap
                # is the peer's to choose and a syncing client has no business demanding a size.
                if count <= 1:
                    raise SystemExit(f"peer refuses even a single-height chunk: {body}")
                a.chunk = max(1, a.chunk // 2)
                print(f"  peer capped chunk size; retrying at {a.chunk}", file=sys.stderr)
                continue
            raise SystemExit(f"peer returned {e.code}: {body}")
        written, man = extract(blob, a.out_dir)
        total_written += written
        total_missing += len(man.get("missing", []))
        if man.get("missing"):
            # Reported, never silent: a gap in the peer's bridge output and the end of the chain are
            # different facts, and a syncing operator must not read one as the other.
            print(f"  {h}..{h + count - 1}: {written} written, "
                  f"{len(man['missing'])} MISSING at the peer", file=sys.stderr)
        h += count
        el = time.time() - t0
        print(f"\r  through {h - 1}/{a.to} — {total_written} bundles, {el:.0f}s", end="", file=sys.stderr)

    print(file=sys.stderr)
    if a.dry_run:
        print(f"dry run: {total_missing} bundles would be downloaded")
    else:
        print(f"done: {total_written} bundles written, {total_missing} unavailable at the peer")
        if total_missing:
            print("Heights the peer could not supply are NOT an error here — its bridge may simply not "
                  "have reached them. Re-run later, or fill them from your own node.")


if __name__ == "__main__":
    main()

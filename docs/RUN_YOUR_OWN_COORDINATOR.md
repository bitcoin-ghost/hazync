# Run your own coordinator

> **Two different things are called "coordinator" in this project.** This document is about the
> **board coordinator** — the long-lived service that hands out ranges, verifies submissions and runs
> the scoreboard. It never proves and needs no GPU.
>
> The **segment coordinator** ([`SEGMENT_DISTRIBUTION.md`](SEGMENT_DISTRIBUTION.md)) is a different
> thing entirely: an ephemeral `seg-serve` process that splits ONE block's proof across machines. It
> lives for one prove and does need a GPU. The two share no code.


A coordinator hands out proving work and keeps the board. It is **not** a trust anchor: a receipt
verifies against `METHOD_ID` regardless of who coordinated it, so losing a coordinator loses the
work queue and the scoreboard, never the validity of anything already proved.

This exists so that is true in practice as well as in principle — if one coordinator goes down,
anyone can stand up another and provers can point at it.

## The honest cost, first

The software is one file (`coordinator/server.py`) and trivially replicable. **The data is not.**

| what | size (measured 2026-08-02) | why |
|---|---|---|
| Bitcoin archive node, `txindex=1` | ~864 GB | the bridge needs arbitrary historical transactions |
| witness bundles | ~73 GB, bridge capped at height 220,000 | what provers actually receive |

Both figures are one box on one day and have not been re-measured; the node grows with the chain, and
bundles grow steeply with height, so raising the bridge's height cap raises the second by far more
than proportionally.

There is no packaging that removes this. A new coordinator either resyncs an archive node from
scratch, or seeds its bundles from an existing coordinator (below). Anyone telling you it is a
`docker run` is describing something else.

⚠ **The bridge will fill a disk.** It runs ahead of demand and there is a height cap for exactly
that reason — without one it fills ~10 hours' worth and stops. Set the cap deliberately.

## Seeding from a peer

`/api/witnesses` streams bundles as a tar, so seeding does not mean 220,001 individual requests:

```bash
curl -s "https://peer.example/api/witnesses?from=1&count=1000" | tar -x -C bundles/
```

The parameters are `from` and `count` (not `lo`/`hi`), and `count` defaults to `BULK_MAX`. The
response carries a manifest listing `served` and `missing` heights — **`missing` is reported rather
than skipped silently**, because a gap in the bridge's output and the end of the chain are different
facts and a syncing peer must not read one as the other. Compare it against what you extracted.

Streamed rather than buffered — one `RANGE_SIZE` chunk is a few hundred MB and the whole set was
~73 GB on 2026-08-02, so building an archive in memory would OOM the coordinator on the first request.

⚠ **Check the transfer completed.** This server speaks HTTP/1.0, so a response without a
`Content-Length` ends at connection close, and a *truncated* transfer looks exactly like a complete
one. A tar ends with two zero blocks, so a parser that reads to the end-of-archive marker can tell
the difference. `tar -x` does this; a naive `read()` loop does not.

## Not duplicating work

Set `PEER_COORDINATORS` and coordinators stop handing out each other's work:

```bash
PEER_COORDINATORS="https://a.example,https://b.example"
PEER_TTL=300              # seconds to cache a peer's proven set
PEER_SYNC_INTERVAL=300    # seconds between background proof adoptions
```

Three mechanisms, all failure-tolerant — an unreachable or malformed peer contributes nothing and
never raises:

- **`peer_proven_heights()`** — heights a peer has already proved are excluded from `pick()`, so
  finished work is not redone.
- **`peer_busy_heights()`** — heights a peer is proving *right now* are also excluded, which shrinks
  collisions to the in-flight window.
- **`sync_from_peers()`** — proofs are adopted from peers, after downloading and verifying each one.
  A peer's word is never taken for a frontier.

**Duplicate work is waste, never fault.** A coordinator that assigns badly, or maliciously, costs
effort and latency; it cannot produce an invalid proof. That is what makes this sufficient without
a consensus protocol between coordinators.

## Bootstrapping is trustless by omission

`/api/vranges` returns only `{lo, hi, handle, proof}`, plus `fold: 1` on a range that is a fold —
deliberately not `in_bhash`, `out_bhash` or
`range_work`. A new coordinator therefore **cannot** take a peer's word for the frontier: it has to
download each proof and run `verify-any` itself, deriving the seam fields from the receipt.

That is the correct behaviour and should stay that way. `_frontier_chain()` is a pure function of
the verified range set, so two coordinators holding the same set compute the same frontier and
converge by construction, with nothing to agree on.

## Minimum configuration

```bash
HAZYNC_HOST=/usr/local/bin/hazync-host   # the canonical host binary the coordinator VERIFIES with
VERIFY_MODE=real                         # the default once HAZYNC_HOST is set; stated so it cannot slip
COORD_PORT=8899                          # listen port
COORD_BIND=127.0.0.1                     # behind a reverse proxy on this box; 0.0.0.0 only behind a firewall
TRUSTED_PROXIES=127.0.0.1,::1            # the proxy's address(es); anyone else's X-Forwarded-For is ignored
COORD_DB=/var/lib/hazync/coordinator.db
COORD_STATE=/var/lib/hazync/coord_state
COORD_PROOFS=/var/lib/hazync/proofs      # retained receipts; defaults to inside the checkout otherwise
COORD_SPINE=/var/lib/hazync/spine        # likewise
HAZYNC_BRIDGE_OUT=/var/lib/hazync/bridge_bundles   # where the bridge writes bundles
RANGE_SIZE=1000                          # heights per range
CLAIM_TTL=3600                           # seconds without a heartbeat before a claim is reoffered
CLAIM_MAX=86400
```

This is the environment of `coordinator/deploy/hazync-coordinator.service` and its drop-ins, reduced
to what a new box needs; the full list is in `coordinator/server.py`. Three things stop it booting
or serving, and each fails in a way that is easy to misread:

- **An empty `HAZYNC_HOST` means `VERIFY_MODE=mock`.** `HAZYNC_HOST` is the path to the host binary,
  not a node address. Without it every proof is accepted unverified, and the server refuses to bind
  any non-loopback address in that state (`COORD_ALLOW_PUBLIC_INSECURE=1` overrides; not for a real
  board).
- **No `python3-cryptography` means signatures fail closed**, and counts as insecure for that same
  bind check.
- **`/var/lib/hazync` must exist and be writable by the service user**, or SQLite cannot create the
  database and the service dies at startup.

`CLAIM_TTL` is what recovers work from a prover that dies mid-block. Too short and slow provers lose
work they were going to finish; too long and a dead prover's blocks sit idle. A claim that never sends
a heartbeat at all is released sooner, after `CLAIM_GRACE` (600 s).

## What a prover needs from you

Very little, which is the point — but it does need **your guest**. A board prover's `hazync selftest`
compares its host's `METHOD_ID` with your `/api/meta`, `run-workers.sh` refuses to start on a
mismatch, and your coordinator rejects a receipt made by any other guest. Beyond that it needs only
what you serve: a witness bundle per block, with no node and no chain data of its own.

A *segment* worker (`seg-connect`, [`SEGMENT_DISTRIBUTION.md`](SEGMENT_DISTRIBUTION.md)) needs less
still — no session and no block, only the segment in front of it — but that belongs to a segment
coordinator, not to you.

So a prover can serve any coordinator without trusting it, provided both run the canonical guest; a
coordinator on a different guest is a different board.

## Related

- `docs/HAZYNC_ARCHITECTURE.md` for how the pieces fit together
- hazync#69 for the design reasoning behind federation

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

| what | size | why |
|---|---|---|
| Bitcoin archive node, `txindex=1` | ~864 GB | the bridge needs arbitrary historical transactions |
| witness bundles | ~73 GB | what provers actually receive |

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

Streamed rather than buffered — one `RANGE_SIZE` chunk is a few hundred MB and the whole set is
~73 GB, so building an archive in memory would OOM the coordinator on the first request.

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

`/api/vranges` returns only `{lo, hi, handle, proof}` — deliberately not `in_bhash`, `out_bhash` or
`range_work`. A new coordinator therefore **cannot** take a peer's word for the frontier: it has to
download each proof and run `verify-any` itself, deriving the seam fields from the receipt.

That is the correct behaviour and should stay that way. `_frontier_chain()` is a pure function of
the verified range set, so two coordinators holding the same set compute the same frontier and
converge by construction, with nothing to agree on.

## Minimum configuration

```bash
COORD_PORT=8899                 # listen port
COORD_BIND=0.0.0.0
COORD_DB=coordinator.db
HAZYNC_HOST=                    # archive node RPC
HAZYNC_BRIDGE_OUT=              # where the bridge writes bundles
RANGE_SIZE=1000                 # heights per range
CLAIM_TTL=3600                  # seconds before an unfinished claim is reoffered
CLAIM_MAX=86400
```

`CLAIM_TTL` is what recovers work from a prover that dies mid-block. Too short and slow provers lose
work they were going to finish; too long and a dead prover's blocks sit idle.

## What a prover needs from you

Very little, which is the point. A worker needs **no session, no ELF, no `METHOD_ID` and no block** —
only the work in front of it. Segments have been proved on a machine whose guest image id differed
from the coordinator's, and the receipts verified where it mattered.

So a prover can serve any coordinator without trusting it or matching its build.

## Related

- `docs/HAZYNC_ARCHITECTURE.md` for how the pieces fit together
- hazync#69 for the design reasoning behind federation

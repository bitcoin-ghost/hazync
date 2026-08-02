# Run your own coordinator

A coordinator is an **archive node + bridge + board**, not a mirror of someone else's. If you run one,
you generate your own witnesses and verify every proof yourself — you are not depending on this
project's box, and it is not depending on yours.

This page states the real cost up front, because the software is the easy part.

## What it actually costs

| | |
|---|---|
| **Bitcoin Core, `txindex=1`, unpruned** | ~865 GB and growing |
| **Bundles** the bridge emits | ~73 GB to height 220,000 |
| **Proof receipts** you accumulate | ~220 KB per block |
| **RAM** | the bridge peaks around 1.5 GB |
| **Disk overall** | budget 1 TB, not 100 GB |

**There is no way around the archive node.** The bridge replays the chain to drive a resident Utreexo
forest forward, so it needs the blocks and it needs the spend history. A pruned node cannot do this.

**You do not need to copy anyone's bundles.** Bundles are a deterministic function of the chain — same
blocks, same leaves, same bytes — so your node produces byte-identical ones. Syncing them from a peer
would be slower than generating them and would introduce a trust relationship you do not need.

## Why there is no consensus protocol

Two coordinators do not have to agree on anything, and this is worth understanding before you assume
it is harder than it is:

* **A proof is self-authenticating.** It verifies against `METHOD_ID` no matter who holds it. There is
  no canonical store — every coordinator keeps its own copy, and any of them can serve it.
* **The frontier is a pure function of the verified set.** Two coordinators holding the same set
  compute the same frontier by construction. Convergence is arithmetic, not agreement — no leader, no
  quorum, no clock.

So federation is a pull, not a protocol: fetch a peer's index, download what you lack, **verify it
yourself**, store it.

## Talking to other coordinators (optional)

Both features are off unless you set them, and both fail open — a peer being down never stalls your
board.

```
PEER_COORDINATORS=https://bitcoinghost.org/hazync,https://someone-else.example/hazync
PEER_TTL=300
```

**`pick()` stops offering heights a peer has already proven.** This does not eliminate duplicate work
— someone may be mid-proof on a height the peer has claimed but not finished — it bounds it to one
proof time instead of the whole board.

**`/api/witnesses?from=&count=` serves bundles in bulk**, as a streamed tar with a manifest, capped
per request by `BULK_MAX` (default `RANGE_SIZE`). This is what makes seeding a new coordinator from a
peer possible at all — with only `/api/witness/<n>` it is ~220,000 individual requests.

**`sync_from_peers()` adopts proofs you do not have.** Every receipt goes through the same STARK
verification a submission does, against *your* `METHOD_ID`. The claimed range is the peer's word; the
receipt has to prove it.

**You are not trusting the peer.** The worst a hostile one can do is waste your bandwidth serving junk
you reject, or withhold work from your own provers. It cannot put anything on your board.

## Seeding bundles from a peer instead of your own node

The bridge run in step 3 below is the long pole, and it is downstream of an ~865 GB node sync. If you
want a coordinator running *today*, you can pull the bundles from a peer:

```
python3 coordinator/sync_bundles.py https://bitcoinghost.org/hazync ./bundles --from 1 --to 220000
python3 coordinator/sync_bundles.py ... --dry-run     # what is missing, downloads nothing
```

It walks in chunks, skips what is already on disk (so an interrupted run is *re-run*, not restarted),
writes atomically, and reports any height the peer could not supply rather than skipping it silently —
a gap in someone's bridge output and the end of the chain are different facts.

**Bundles are witness data, not proofs, and this is not the trustless path.** Nothing downloaded here
is verified. What limits the damage is that it *cannot* be laundered into a bad proof: a receipt
verifies against `METHOD_ID` regardless of which witness produced it, so a hostile peer can waste your
GPU time on bundles that fail to prove and can do nothing else. If you want the stronger property —
that these are the bundles an archive node would have produced — run the node. That is what the rest
of this page is about, and the shortcut does not replace it.

At ~73 GB this is a large transfer either way; it is just a much shorter one than a node sync.

## Getting started

1. **Sync a node.** `txindex=1`, no pruning. This is the long pole — days, not hours. (Or seed bundles
   from a peer, above, and come back to this when you have the disk.)
2. **Get the canonical host.** Either the signed release, or `docker build -f reproduce/Dockerfile .`
   and check the id matches `reproduce/METHOD_ID`. A host built outside the fixed-path container has a
   different image id and will reject published proofs — that is the build being wrong, not the proofs.
3. **Run the bridge.** It walks the chain from genesis emitting one bundle per block. Expect this to
   take a while and watch your disk: see `HAZYNC_BRIDGE_TO` for a height cap, which exists because an
   uncapped bridge on a fast box filled a 2 TB disk in about ten hours.
4. **Run the coordinator.** `coordinator/deploy/` has the unit files and drop-ins.
5. **Point provers at it** with `COORD_URL`.

## Things that will bite you

**Everything must agree on `METHOD_ID`** — coordinator, bridge, provers, and the verifiers you publish.
A mismatch is not subtle in its effect (nothing verifies) but is easy to cause: the bridge is a
*separate service with its own binary*, and leaving it on an old guest once stalled a board dead while
every other component looked healthy.

**Run the services unprivileged, but move the data first.** `ProtectHome=`/`ProtectSystem=strict` are
worth having, and they are incompatible with keeping the database, proofs and bundles under `/root`.
See `coordinator/deploy/dropins/` for a working set.

**The spine needs a driver.** `hazync spine` absorbs what it can and returns — it is not a daemon.
`MODE=spine ./coordinator/run-workers.sh 1` keeps it advancing. Without it your board fills normally
while `/api/spine/proof` quietly serves nothing, and no other signal tells you.

**Back up the ledger off-box.** The receipts are re-provable, expensively; `coordinator.db` — who
proved what — is not re-provable at any price.

## Reporting problems

If your coordinator disagrees with another about the frontier, that is interesting and worth an issue:
given the same verified set the rule is deterministic, so a disagreement means the sets differ or the
rule is not what we think it is. Include both `/api/state` outputs and `/api/vranges`.

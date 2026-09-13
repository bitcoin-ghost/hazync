# Hazync v0.21.4 — a board that explains itself

Everything here comes from one night's incident. Block 39,413 pinned the frontier for five hours, and
the coordinator's entire account of it was *"a live worker is proving it"*. Each change below is a
thing that could not be seen at the time.

> ✅ **Not a re-baseline.** Canonical `METHOD_ID` is still
> `37987b85ec665970ac6c5e8031deb8160ac8ed846f09056c3790b5f78c8bb5dd`. The guest, the circuit and the
> verifier logic are unchanged. Every existing proof stays valid and the board does not reset. Only
> the host (`prover/host/src/main.rs`) changed on the prover side.

---

## What 39,413 turned out to be

It was never a bad block or a bad prover. Proved on a current binary:

```
range [39413..39413] (bridge): executed, 1012 segments at po2 21 -- proving
assembled 1012 segment receipts in 757s
proved range [39413..39413] in 4076.5s
```

**Assembly took 757 s against the 600 s silence timeout that v0.21.3 removed (#286).** A worker on an
older release does 55 minutes of correct GPU work, goes quiet for the final step, is killed as a
suspected hang, retries once, is killed again, gives up and re-claims — forever, about two hours of
GPU per cycle. The coordinator's record of that is a claim, heartbeats, and nothing else: zero
submissions, zero attempts, no error.

Unsticking that one block moved the frontier **39,412 → 42,305**, because 2,893 blocks proved above it
re-attached at once.

## So the board now says who is running what (#293)

The question "what release is that worker on?" could not be answered: the CLI sent no version and the
coordinator stored none. Every worker-side failure we have fixed leaves the same trace — #261 (no
usable GPU), #256 (a stall), #268 (a lost claim response), #286 (assembly read as a hang) — and
nothing distinguished them.

- The CLI reports `hazync-worker/<release>` on every request. **Stamped at package time, not
  maintained by hand:** `package-release.sh` rewrites it with the tag being built and asserts the
  stamp landed, so there is no constant to forget and no gate needed to catch someone forgetting. A
  checkout says `dev`, which is a real answer.
- The coordinator records it per contributor, **on claim as well as submit** — a worker that cannot
  finish never submits, so submit-only would be silent for exactly the case this exists for.
- The leaderboard carries it, so a contributor can see their own worker is stale without asking.
- A blocked frontier names the holder **and their release**, and stops calling a five-hour claim
  benign: past two claim cycles it needs attention and says so.

⛔ **Advisory, never enforcing.** Nothing is refused for its version. Proving out of order is the
design, old proofs stay valid while the guest is unchanged, and the guest id already gates what can
land (#99).

## A checkout can no longer write to the public board (#293)

`COORD_URL` defaults to production, and that default is right — a contributor who downloaded a
release should not be sent to localhost. It is also a footgun for anyone working *on* hazync, and it
fired: a fold of blocks 1–2 landed on the live board from an unrecognised key under the
auto-generated handle `ghost:ff0376`, with no claim and nothing before or after it.

A **release** writes to production silently, as before. A **checkout** must now say it means it:

```
COORD_URL=http://127.0.0.1:8899 ...     # your own coordinator
HAZYNC_ALLOW_DEV_WRITES=1 ...           # you really do mean the public board
```

⚠ `/opt/hazync` is a checkout, so running `./coordinator/hazync run …` there now needs that variable.

A refusal rather than a warning, deliberately, and unlike the default-handle warning a few lines
away in the same file: a wrong handle is a label on your own work that `hazync rotate` fixes (#113);
a stray submission is a permanent public record on someone else's board. Warnings are for what can be
undone.

## The frontier, continued (#281)

- **`hazync run` says what you asked for before it costs anything.** `hazync run 30000-30050` proves
  **51** blocks and reads as fifty — which is how three chunks came to start on the block their
  predecessor ended on and froze the board for thirteen hours. It now prints
  `proving 51 blocks: 30000..30050 inclusive — the next range starts at 30051` before the witness
  probe, let alone the prove. The count is what makes an off-by-one visible.
- **Only a single block may introduce coverage**, so `covered == proved` and the fold tree only
  re-expresses what is already there.
- **Leaves are submitted as they are proved.** A wide range used to send nothing until every block
  had been proved and the whole tree collapsed, so a failure at block 49 of 50 threw away all 49.
- **Key rotation is in CI.** It had been failing on main since `distinct_blocks_by_pubkey` was
  renamed, and nothing said so, because it was not wired in.

## Measurement (#252)

Every join and resolve round trip is now timed, **on the server's clock at both ends**:

```
[rtt] peer=1.2.3.4:55123 kind=join tag=0x10002 rtt_ms=412.7 bytes_out=770112 bytes_in=215040
```

That matters because the worker's own `compute_s` is a *duration*, not a timestamp, so
`rtt_ms - compute_s*1000` gives transport and queueing **without the two clocks having to agree** —
which, across rented boxes in several countries, they do not. #252 says its geography conclusion is
"inference, not measured (no RTTs were recorded)". Now they are. It measures; it does not conclude.

## Also

- `/api/spine/segments` — who absorbed each block into the spine, and how much of the chain is folded
  (#292), so the two steps after proving stop being anonymous.

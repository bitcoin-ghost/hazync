# Hazync Proof Party — coordinator (MVP)

A small, dependency-light service that turns "prove Bitcoin's history" into a community effort:
hand out block ranges + witnesses, receive **signed** proof receipts, **verify** them (a bad proof
fails verification — nobody can cheat), record signed attribution in an open ledger, and serve a
live dashboard.

The web **coordinates**; a contributor's **local GPU + CLI** does the proving. The browser never
runs the prover.

```
  pick/claim a range ──► witnesses auto-fetched ──► hazync prove (your GPU) ──► sign ──► hazync submit
   coordinator verifies signature + STARK receipt ──► chains it by tip ──► credits the block to you
```

## Run it

```bash
cd coordinator
python3 server.py                      # dashboard + API on http://localhost:8899
```

No dependencies for the core (Python 3 stdlib + SQLite). Signature verification uses `cryptography`
if installed (`pip install cryptography`) — otherwise it runs in **dev mode** (signatures accepted,
clearly flagged in `/api/state` and on the dashboard). Install it for the real signed ledger.

### Config (env)

| var | default | meaning |
|-----|---------|---------|
| `COORD_PORT` | `8899` | listen port |
| `COORD_DB` | `coordinator.db` | SQLite file |
| `TIP_HEIGHT` | `958301` | **floor**, not the answer. The ceiling is derived from the highest bundle the bridge can serve; this is the fallback when that is lower (a backfilling or absent bridge) and the denominator for % complete |
| `TIP_CACHE_TTL` | `300` | seconds to cache that bundle scan (~0.17s over 220,000 files) |
| `RANGE_SIZE` | `1000` | blocks per claimable range |
| `SEED_RANGES` | `60` | ranges to create on first run |
| `HAZYNC_BRIDGE_OUT` | — | archive-bridge bundle dir; `/api/witness/<n>` serves `bundle_<n>.json` from here |
| `WITNESS_DIR` | `./witnesses` | legacy per-block witnesses (`block_<n>.json`), replay fallback |
| `HAZYNC_HOST` | — | path to the canonical prover `host` binary (for real verification) |
| `VERIFY_MODE` | `real` if `HAZYNC_HOST` set, else `mock` | `mock` stubs the STARK check for testing |
| `CLAIM_TTL` | `1800` | auto-release a claim after this many seconds without a heartbeat |
| `CLAIM_MAX` | `86400` | hard cap: release a claim after this long regardless of heartbeats |
| `RATE_MAX` | `120` | max writes per IP per window (over → `429`) |
| `RATE_WINDOW` | `60` | rate-limit window, seconds (honours `X-Forwarded-For`) |
| `MAX_BODY` | `8388608` | reject POST bodies / receipts larger than this (`413`) |
| `MAX_HANDLE` | `48` | contributor handle length cap (non-printables stripped) |

## Contributor CLI

```bash
export COORD_URL=https://coordinator.example
export HAZYNC_HOST=/path/to/prover/target/release/host    # your GPU box
export WITNESS_DIR=/path/to/witnesses

./hazync id  my-handle          # create your ed25519 identity
./hazync pick                   # ask the coordinator which range to take next
./hazync run 45000-45999        # claim + prove (GPU) + sign + submit, end to end
./hazync run                    # no range → picks the next open one for you
```

Identity (`~/.hazync/key.hex`) and receipts (`~/.hazync/receipts/`) are local. `prove` **auto-fetches** a
ready-made archive **bundle** for each block in the range from the coordinator's `/api/witness/` endpoint
and proves each block directly with `prove-range-bridge` — **no chain replay** (O(1) per block). If the
coordinator serves no bundle for a block (no bridge configured), it transparently falls back to the legacy
replay path (`prove-range`, fetching every witness `1..hi`). Either way it folds the per-block receipts
into one with `fold-range`. You need no node of your own and no local witness data.

## API

- `GET /api/state` — progress, board (with per-claim `elapsed`/`beat`/`stale`), leaderboard, recent
- `GET /api/pick` — suggest the next open **block** past the frontier (skips claimed/verified, and
  anything the bridge cannot serve a bundle for). Width is one block; a wider aligned chunk is opt-in
  via `hazync run <lo>-<hi>`.
- `POST /api/claim` `{pubkey, handle}` — take the earliest available block. Held by a heartbeat
  (`POST /api/beat`), reopens after `CLAIM_TTL`, hard ceiling `CLAIM_MAX`. **Advisory on top**:
  `submit` accepts any height regardless of who claimed it, so an allocator bug can waste effort and
  can never lock a contributor out.
- `POST /api/submit` `{range, pubkey, handle, sig, receipt(base64)}` — verify + credit
- `GET /api/witness/<n>` — serve block `n`'s archive **bundle** (in-boundary + the real accumulator root + inclusion proofs) from the bridge; falls back to a legacy per-block witness. Accepts a block number or a `lo-hi` range id
- `GET /api/proof/<id>` — **download the verified proof receipt** for a block/range so anyone can
  re-verify it themselves (`host verify-any proof_<id>.bin`). Retained on every successful submit;
  the `vranges` list in `/api/state` carries a `proof` pointer for each. Retention is **per proven
  height, not a rolling window**: `check-retention.py` is a nightly gate that walks every height the
  board calls proven and fails if any one of them cannot be handed over on its own. It has caught a
  real regression before — an earlier fold path discarded the per-block leaves after folding, leaving
  800 blocks with no individual receipt. (Receipts run ~0.2–1.7 MB each, so this is archive-scale by
  construction; a `verify-anywhere` Groth16 wrap is the later upgrade so a proof checks with no RISC0
  runtime.)
- `POST /api/spine` also records the absorption in `submissions`, so spine work shows in the board's
  Recent work feed under its contributor's handle (#114). It deliberately does **not** increment
  `contributors.blocks`: that column means blocks of chain covered, and an absorption covers nothing
  new — it re-expresses already-proven blocks as one checkable file. Crediting effort separately is
  still open.
- `GET /api/foldable` — aligned **sibling pairs of equal width** whose parent does not exist yet, for
  `hazync fold`. Not any adjacent pair: that does not converge, and on this board it once produced 581
  folds covering 96 blocks where a tree needs 95.
  Advisory and stateless: several candidates are returned so concurrent workers spread out without
  anything being leased. A duplicate fold is wasteful, not incorrect — the loser's submission is
  discarded as already proven, and folding is far cheaper than proving.
- `GET /api/spine` — metadata for the **genesis-anchored head**: how far it reaches, its out-tip,
  work, size and who last advanced it.
- `GET /api/spine/proof` — the head receipt itself. This is the headline artifact: one file attesting
  that every block from 1 to N is valid, checkable with `hazync-verify` and nothing else.
- `POST /api/spine` `{pubkey, handle, sig, receipt(base64)}` — submit an extended spine. The
  coordinator does **not** build it: extending is a prove operation and this box has no GPU. It
  verifies (`verify-range` for the full genesis pin, then `verify-any` for the machine-readable
  boundary), stores it, and refuses anything that does not advance the head. Advancing the spine is a
  liveness single point of failure and never a soundness one — a wrong absorption does not verify, and
  because per-block receipts are retained anyone can rebuild the spine from scratch.

**Claim-lock + auto-release:** a claim locks the range to one contributor. The prover heartbeats while
working; if heartbeats stop for `CLAIM_TTL` (or the claim exceeds `CLAIM_MAX`), the coordinator
**auto-releases** it back to the pool (lazy reaping on each state/claim, so a dead claim frees up within
a poll interval). This is the "cut them off if progress isn't moving" — dead claims return in minutes,
not days.

`/api/submit` verifies the **ed25519 signature over the receipt bytes**, then verifies the proof on
**CPU** with `host verify-any` (real STARK verification, no genesis assertion, `VERIFY_MODE=real`),
confirms it's for the claimed `[lo..hi]`, and records its boundary tips. It **does not fold** — folding
is GPU proving work that belongs on contributors' boxes. Instead the coordinator **chains** verified
ranges by tip (`out_tip` of *k* == `in_tip` of *k+1*) to compute the genesis-anchored frontier. So any
block can be proved **out of order** and verified independently; the frontier advances as contiguous
runs connect. A forged/wrong proof fails `verify-any`; a receipt claiming the wrong range is rejected;
neither credits anything. The dashboard shows **two numbers**: *verified* blocks (any) and the
*genesis frontier* (contiguous from block 1).

## Deploy

Deployed **co-located with the archive bridge and a full `bitcoind`** on one box: `bitcoind` feeds the
bridge (`hazync-bridge.service`), which writes bundles the coordinator serves via `HAZYNC_BRIDGE_OUT` (a
local read — no cross-box copy); the coordinator verifies receipts on CPU with the canonical `host`
binary (no GPU required to *verify*). Put it behind a reverse proxy (nginx) with TLS; the public
`bitcoinghost.org/hazync` page points its board at this API (CORS is open by default). Units + cutover:
`deploy/hazync-bridge.service`, `deploy/hazync-coordinator.service`, `deploy/migrate-coordinator.sh` —
see `deploy/RUNBOOK.md`.

## Status — honest

MVP. Single-file, SQLite, single-process, **verify-only (CPU, no GPU)**. Verified end-to-end with real
proofs: blocks 1..10 were CPU-proved on a laptop (~64–110s each), signed, submitted, verified with
`verify-any`, and chained into the genesis frontier [1..10]. Out-of-order submission tested (block 3
before block 2: verified but the frontier held at 1, then jumped to 3 when block 2 filled the gap);
wrong-range receipts rejected; ed25519 signed ledger enforced. **All five roadmap items are done** on top
of this MVP: **verify-and-chain**, **claim-lock + heartbeat auto-release**, **pick-any-block + witness
serving** (the CLI auto-fetches the witnesses it needs), the **genesis→tip timeline UI** (frontier /
ahead / in-progress / open), and **hardening** (per-IP rate limits, exact-length ed25519 input caps,
body/handle caps). It now runs **co-located with the archive-node bridge + `bitcoind`**, so witness
bundles are served from local disk (`HAZYNC_BRIDGE_OUT`) with no replay. What's left before a public push
is operational, not code: the genesis→tip GPU seeding campaign (now O(1) per block via the bridge) and
independent adversarial review.

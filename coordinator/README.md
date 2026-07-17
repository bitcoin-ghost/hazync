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
| `TIP_HEIGHT` | `958301` | chain tip (denominator for % complete) |
| `RANGE_SIZE` | `1000` | blocks per claimable range |
| `SEED_RANGES` | `60` | ranges to create on first run |
| `WITNESS_DIR` | `./witnesses` | per-block witness files: `block_<n>.json` (served via `/api/witness/<n>`) |
| `HAZYNC_HOST` | — | path to the prover `host` binary (for real verification) |
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

Identity (`~/.hazync/key.hex`) and receipts (`~/.hazync/receipts/`) are local. `prove` **auto-fetches**
every witness it's missing (blocks `1..hi`, which the prover replays to rebuild the accumulator) from
the coordinator's `/api/witness/` endpoint into `WITNESS_DIR` before it starts — so you don't need any
local witness data. It then proves each block in the range and folds them with the existing
`prove-range` / `fold-range` commands.

## API

- `GET /api/state` — progress, board (with per-claim `elapsed`/`beat`/`stale`), leaderboard, recent
- `GET /api/pick` — suggest the next open range past the frontier (skips claimed/verified)
- `POST /api/claim` `{range, pubkey, handle}` — **lock** a block/range to you; rejected if held by someone
  else. `range` is either a **single block** (`"764321"` — "I just want one block") or a RANGE_SIZE-aligned
  range (`"45000-45999"`); the row is auto-created on demand (no pre-seeded list). Single blocks and aligned
  ranges are the only shapes, so two claim ids can never partially overlap
- `POST /api/heartbeat` `{range, pubkey}` — keep your claim alive (the CLI sends one every 30s while proving)
- `POST /api/submit` `{range, pubkey, handle, sig, receipt(base64)}` — verify + credit
- `GET /api/witness/<n>` — serve block `n`'s witness (accepts a block number or a `lo-hi` range id)
- `GET /api/proof/<id>` — **download the verified proof receipt** for a block/range so anyone can
  re-verify it themselves (`host verify-any proof_<id>.bin`). Retained on every successful submit;
  the `vranges` list in `/api/state` carries a `proof` pointer for each. (Receipts are ~0.2–1.7 MB each,
  so full-chain retention is archive-scale — a rolling window for launch; a `verify-anywhere` Groth16
  wrap is the later upgrade so a proof checks with no RISC0 runtime.)

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

Run on a box that has the prover `host` binary (verification needs it — no GPU required to *verify*).
Put it behind a reverse proxy (nginx/caddy) with TLS. The dashboard can be served from here, or the
public `bitcoinghost.org/hazync` page can point its board at this API (CORS is open by default).

## Status — honest

MVP. Single-file, SQLite, single-process, **verify-only (CPU, no GPU)**. Verified end-to-end with real
proofs: blocks 1..10 were CPU-proved on a laptop (~64–110s each), signed, submitted, verified with
`verify-any`, and chained into the genesis frontier [1..10]. Out-of-order submission tested (block 3
before block 2: verified but the frontier held at 1, then jumped to 3 when block 2 filled the gap);
wrong-range receipts rejected; ed25519 signed ledger enforced. **All five roadmap items are done** on top
of this MVP: **verify-and-chain**, **claim-lock + heartbeat auto-release**, **pick-any-block + witness
serving** (the CLI auto-fetches the witnesses it needs), the **genesis→tip timeline UI** (frontier /
ahead / in-progress / open), and **hardening** (per-IP rate limits, exact-length ed25519 input caps,
body/handle caps) with the auth + archive decisions recorded in `ROADMAP.md`. What's left before the
Delving post is operational, not code: host it on a cheap CPU VPS with a witness window, seed a few hours
of real GPU proving, and confirm the contributor onboarding end-to-end.

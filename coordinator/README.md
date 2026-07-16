# Hazync Proof Party — coordinator (MVP)

A small, dependency-light service that turns "prove Bitcoin's history" into a community effort:
hand out block ranges + witnesses, receive **signed** proof receipts, **verify** them (a bad proof
fails verification — nobody can cheat), record signed attribution in an open ledger, and serve a
live dashboard.

The web **coordinates**; a contributor's **local GPU + CLI** does the proving. The browser never
runs the prover.

```
  claim a range ──► GET witness ──► hazync prove (your GPU) ──► sign ──► hazync submit
        coordinator verifies signature + STARK receipt ──► folds it ──► credits the block to you
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
| `WITNESS_DIR` | `./witnesses` | per-range witness files: `witness_<lo>-<hi>.json` |
| `HAZYNC_HOST` | — | path to the prover `host` binary (for real verification) |
| `VERIFY_MODE` | `real` if `HAZYNC_HOST` set, else `mock` | `mock` stubs the STARK check for testing |

## Contributor CLI

```bash
export COORD_URL=https://coordinator.example
export HAZYNC_HOST=/path/to/prover/target/release/host    # your GPU box
export WITNESS_DIR=/path/to/witnesses

./hazync id  my-handle          # create your ed25519 identity
./hazync run 45000-45999        # claim + prove (GPU) + sign + submit, end to end
```

Identity (`~/.hazync/key.hex`) and receipts (`~/.hazync/receipts/`) are local. `prove` proves each
block in the range and folds them with the existing `prove-range` / `fold-range` commands.

## API

- `GET /api/state` — progress, board, leaderboard, recent (the dashboard polls this)
- `POST /api/claim` `{range, pubkey, handle}` — reserve a range
- `POST /api/submit` `{range, pubkey, handle, sig, receipt(base64)}` — verify + credit
- `GET /api/witness/<range>` — serve a range's witness (if present)

`/api/submit` verifies the **ed25519 signature over the receipt bytes**, then verifies the **STARK
receipt** by shelling to `host verify-range` (`VERIFY_MODE=real`). Only a proof that actually verifies
credits the range — a forged or wrong receipt is recorded as failed and credits nothing.

## Deploy

Run on a box that has the prover `host` binary (verification needs it — no GPU required to *verify*).
Put it behind a reverse proxy (nginx/caddy) with TLS. The dashboard can be served from here, or the
public `bitcoinghost.org/hazync` page can point its board at this API (CORS is open by default).

## Status — honest

MVP. Single-file, SQLite, single-process. It proves the flow works end-to-end (tested: claim → sign →
submit → real ed25519 verify → STARK verify → credit → dashboard updates). Not yet hardened for
hostile scale — no rate limiting, no witness-integrity binding beyond the receipt, no auth on `claim`.
Those are the next steps once the flow is exercised with real proving.

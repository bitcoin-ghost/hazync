# Proof Party coordinator — roadmap

The build order to get from the working MVP to a public, parallel, easy-to-onboard coordinator —
before the Delving post. Each item is scoped so the coordinator stays a cheap CPU VPS (no GPU: proving
and folding happen on contributors' GPU boxes; the coordinator only verifies + tracks + serves).

## 1. Verify-and-chain (replaces coordinator-side folding)  ← NEXT
The coordinator must **not** fold (folding is a proving step — GPU work). Instead:
- Verify each submitted range receipt on **CPU** (`host verify-any` — verify the STARK, read the
  committed `[lo, hi, in_tip, out_tip, work]`, **without** the genesis assertion).
- Store each verified range's tips. Recompute the **genesis-anchored frontier** by chaining verified
  ranges (start at block 1, follow `out_tip == next.in_tip`).
- This is both CPU-cheap **and** parallel: any block can be proved out of order and verified
  independently; the frontier advances as contiguous runs connect.
- Needs a small `host` addition (`verify-any`, host-only, no guest rebuild) + coordinator rewrite of
  `verify_receipt` + frontier tracking.
- Show TWO numbers on the dashboard: **verified blocks** (any, out of order) and **genesis frontier**
  (contiguous from 0). A lone far-out block is "verified but not yet chained" — not stalled.

## 2. Claim-lock + heartbeat + auto-release (liveness / no duplication)  ✅ DONE
- Claiming **locks** a block to one contributor; a claim by anyone else is rejected (409 "locked").
- The CLI sends a **heartbeat** every 30s while proving → `POST /api/heartbeat` (background thread).
- **Auto-release** (`reap`, lazy on each state/claim): no heartbeat for `CLAIM_TTL` (default 1800s) or
  held past `CLAIM_MAX` (default 24h) → the range returns to the pool. Dead claims free up in minutes.
- Dashboard shows assignee + elapsed + a `quiet` flag when heartbeats go stale (no precise % — RISC0
  proving gives no clean progress signal, so heartbeat/elapsed is the honest one).
- Tested: A claims → B rejected (locked); A heartbeats → stays; no heartbeat past TTL → auto-released;
  B reclaims → A's stale heartbeat rejected.

## 3. Pick-any-block + witness serving (rolling window)  ✅ DONE
- **Pick:** `GET /api/pick` suggests the next open range past the frontier (skips claimed/verified);
  `hazync pick` prints it, and `hazync run` (no arg) claims whatever it hands back. Any valid aligned
  range can also be claimed directly — `claim` auto-creates the range row on demand (`parse_range`
  validates alignment/size/bounds), so contributors aren't limited to a pre-seeded list.
- **Witness serving:** `GET /api/witness/<n>` (block number) or `/api/witness/<lo>-<hi>` (range id)
  serves that block's witness from the coordinator's `WITNESS_DIR`. The CLI's `prove` auto-fetches every
  witness it's missing (blocks 1..hi — the prover replays them to rebuild the accumulator) before it
  starts, so a contributor needs no local witness data. A block the coordinator doesn't hold yet returns
  404 and the CLI stops with a clear message. Full-chain witness hosting is the archive decision (item 5).
- Tested (mock): pick → 1-1; claim-any auto-creates the row; witness served by both block number and
  range id; pick skips a claimed range (→ 2-2); un-served block → 404; `hazync pick` + `fetch_witnesses`
  pull the right blocks into an empty contributor dir and fail cleanly on a block the coordinator lacks.

## 4. Timeline UI  ✅ DONE
- A genesis→tip strip on the dashboard, bucketed server-side into 240 segments (`state().timeline`,
  bounded payload at any chain size). Four states: **frontier** (solid green — verified *and* chained
  from genesis), **ahead** (light green — verified but out of order), **in progress** (orange —
  claimed), **open** (grey). A marker sits at the frontier position; a legend explains each state, and
  the header shows the two-number story (proven-&-chained + segments ahead).
- The rolling **board** below it stays the per-range zoom for the active window (contributor + elapsed +
  quiet/proving), so the strip is the macro view and the board the micro view.
- Tested: `timeline()` maps a genesis-chained run → frontier, an out-of-order verified range → ahead, a
  live claim → in-progress, everything else → open; `frontier==0` renders no green (edge fixed).

## 5. Harden + archive decision (last, before opening it up)  ✅ DONE
- **Hardening (done):**
  - **Rate limiting** — sliding per-IP window on all writes (`RATE_MAX`/`RATE_WINDOW`, default 120/60s;
    honours `X-Forwarded-For` behind the reverse proxy). Over budget → `429`.
  - **Input caps** — `pubkey`/`sig` must be exact-length ed25519 hex (32/64 bytes) when signatures are
    live; `range` must pass `parse_range` (aligned, correct size, in-bounds) on claim *and* submit;
    handles are stripped of non-printables and capped (`MAX_HANDLE`, default 48); request bodies over
    `MAX_BODY` (default 8 MiB) → `413`, and an oversized base64 receipt → `413`.
  - Tested: valid claim ok; non-hex pubkey → 400; misaligned range → 400; control-char/overlong handle
    sanitised in the ledger; rate limit trips after the budget; oversized body → 413.
- **Auth-on-claim — decision:** claims stay **cheap best-effort locks** (guarded by rate-limiting +
  auto-release), *not* signed. Cryptographic authentication lives at **submit** (ed25519 over the
  receipt bytes) — that's what gates credit, and a forged proof also fails `verify-any`. Signed claims
  would add onboarding friction for no security gain (the worst a bogus claim does is hold a range until
  the heartbeat lapses, minutes later). So: no signed claims.
- **Archive / witness source — decision:** launch with the **rolling window** (option b). Witnesses are
  served from the coordinator's `WITNESS_DIR`; the operator seeds a window covering the active frontier
  + the region contributors are working, and the CLI auto-fetches what it needs (item 3). A block outside
  the window returns 404 and the CLI says so plainly. **Co-locate** a full hazed archive/bridge node on
  the coordinator box (option a) later, when the disk cost is worth the clean "we host all the data"
  story. This keeps the launch coordinator a cheap CPU VPS, exactly as intended.

## Framing (non-technical, but load-bearing)
The proofs are a **public good** — public, verifiable, benefit every node runner (fast sync, spam-free
full nodes). Public copy is "help prove Bitcoin's history, for everyone, with your name on it" — **not**
"donate compute to us." That framing is what makes the Delving audience receive it well.

## Status
MVP done + tested end-to-end with real CPU proofs (blocks 1..10, genesis-anchored frontier climbing,
ed25519 signed ledger). **Items 1–5 all done** — verify-and-chain, claim-lock/heartbeat/auto-release,
pick-any-block + witness serving, genesis→tip timeline UI, hardening + archive decision. The coordinator
is ready to host on a cheap CPU VPS; remaining before the Delving post is operational, not code: stand up
the VPS + a witness window, run a few hours of GPU proving to seed real frontier progress, and confirm
the contributor onboarding UX end-to-end.

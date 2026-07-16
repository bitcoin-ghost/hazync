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

## 2. Claim-lock + heartbeat + auto-release (liveness / no duplication)
- Claiming **locks** a block to one contributor; others can't take it.
- The CLI sends a **heartbeat** (~every 30s) while proving → `POST /api/progress`.
- **Auto-release on heartbeat timeout** (~30–60 min no ping) — this is "cut them off if progress isn't
  moving." Plus a generous hard cap (a few hours / 7-day backstop). 7 days *per block* is far too long
  as the primary timeout — a block proves in minutes; dead claims must free up fast.
- Dashboard shows status + assignee + elapsed (+ coarse ETA at best — RISC0 proving gives no clean %,
  so heartbeat/elapsed is the honest signal, not a precise percentage).

## 3. Pick-any-block + witness serving (rolling window)
- Let a contributor pick any open block/range.
- Serve that block's **witness** (`GET /api/witness/<range>`) from a rolling window covering the active
  frontier. Full-chain witness hosting is the archive decision (item 5).

## 4. Timeline UI
- A genesis→tip strip: green = verified, orange = in-progress (contributor + elapsed), grey = open.
- Colour + contributor + elapsed per block; the two-number header (verified / frontier).

## 5. Harden + archive decision (last, before opening it up)
- Rate limiting, auth-on-claim, input caps.
- **Archive / witness source:** contributors need witnesses (per-block bridge data). Options:
  (a) **co-locate** a hazed archive/bridge node on the coordinator box that generates witnesses as it
      syncs and prunes proven blocks — clean "we host the data" story, but the box becomes an archive
      node (4–8 vCPU, 16 GB, hundreds of GB disk), not a $20/mo VPS;
  (b) **separate** archive box (seed/GPU box), coordinator serves a rolling cache / proxies.
  Lean: rolling window to start; co-locate the full hazed archive when it's worth the disk.

## Framing (non-technical, but load-bearing)
The proofs are a **public good** — public, verifiable, benefit every node runner (fast sync, spam-free
full nodes). Public copy is "help prove Bitcoin's history, for everyone, with your name on it" — **not**
"donate compute to us." That framing is what makes the Delving audience receive it well.

## Status
MVP done + tested end-to-end with real CPU proofs (blocks 1..10, genesis-anchored frontier climbing,
ed25519 signed ledger). Items 1–5 are the pre-Delving work.

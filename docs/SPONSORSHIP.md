# Sponsoring blocks

Status: **the records, the minimum, the private link, the public list, the API and the site's form exist;
payments and the proving bot do not.** Sponsorship is closed on the live coordinator (`SPONSOR_OPEN`
unset) and has no bitcoin price (`SPONSOR_BTC_USD` unset), so the site's form says so and nothing is recorded.

## What it is for

Anyone can pay for a block, or a range of blocks, to be proven. The payment funds GPU time; a bot starts
pods, runs the ordinary worker on those blocks, and the block map shows **Sponsor: <name>** next to the
prover. The sponsor's name is a coordinator record. It is never put inside a proof: changing what the
guest commits changes the prover program ID.

## The flow

```
requested -> invoiced -> paid -> proving -> proven
          |           \-> underpaid (kept as a donation)   \-> refunded
          \-> expired / cancelled
```

| Status | Set by | Built |
|--------|--------|-------|
| `requested` | `POST /api/sponsor` | yes, when `SPONSOR_OPEN=1` and the span is priced |
| `invoiced` | payment integration (BTCPay: Lightning and on-chain) | no |
| `paid` | payment integration, on settlement of **at least** `min_sats` | no |
| `underpaid` | payment integration, on settlement **below** `min_sats`; the payment is kept as a donation | status only |
| `proving` | sponsor bot, when it starts pods | no |
| `proven` | `submit()`, when every block of the span is covered by verified ranges | yes |
| `expired` | payment integration, invoice not paid in time | no |
| `cancelled`, `refunded` | operator | no |

A sponsor's name is published **only** when both hold: the status is `paid`, `proving` or `proven`, **and**
`paid_sats >= min_sats` (`SPONSOR_PUBLIC_SQL` in `server.py`, used by every query that returns a name, and
by the bot's queue). An unpaid request is text anyone can type; showing it would let anyone put a name on
any block. A row set to `paid` by hand below its minimum still shows nothing.

**An `underpaid` payment is kept as a donation** (decided 2026-09-13). No name is shown and its blocks are
not proven for it; the money funds proving time in general. The site says so on the Sponsor form before
anyone pays, on the sponsor's private page, and on the sponsors page.

## The minimum

Every span has a **minimum**, set in whole dollars per block by height and turned into sats at the bitcoin
price, and a sponsor may pay more. The minimum is a deliberate **overestimate** of the compute: card prices
change by the hour, and it is better to know a paid block can be proven than to take too little and have to
withhold the sponsor's name. Paying at least the minimum is what earns the name; less is kept as a donation.

### The price ladder (decided 2026-09-13)

| Blocks | Minimum per block | 2x the estimated cost of the heaviest 1% of blocks |
|--------|------------------:|---------------------------------------------------:|
| 1 to 100,000 | $1 | about $0.31 or less |
| 100,001 to 150,000 | $2 | $0.81 |
| 150,001 to 180,000 | $3 | $1.26 |
| 180,001 to 200,000 | $4 | $4.31 |
| 200,001 to 230,000 | $5 | $5.43 |
| above 230,000 | **no price** | not measured |

- **Basis.** Each band is twice the estimated GPU cost of the heaviest 1% of its blocks, rounded up to whole
  dollars. The cost is priced at **$0.74 per RTX 4090 card-hour**, the dearer of two prices RunPod charged
  the project (the other was $0.34). Up to about 57,000 the per-block cost is **measured** from the proof
  party fleet (mean about $0.003 a block, all-in). From there to 230,000 it is **extrapolated** from each
  block's measured bundle size: card-seconds = 18 + 3.0 x segments, with segments = bundle bytes / bytes
  per segment, using the middle of the four measured bytes-per-segment ratios.
- **The top two bands are about 1.85x, not 2x**, because the ladder stops at $5.
- **Above 230,000 there is no price.** The bridge cannot prove those blocks yet (bundles stop at 230,000)
  and nothing there has been measured, so they cannot be sponsored. The cheapest measurement is CPU only:
  execute a handful of sampled blocks per band and count their segments.
- **What it does not cover.** A rare block far heavier than the 1% line (block 55,862 took at least 9,600
  card-seconds) costs more than its band's price when sponsored alone. A range of ordinary blocks pays well
  over its cost; the difference funds proving time.

### Setting it

- `SPONSOR_PRICE_BANDS` (JSON `[[lo, hi, usd_per_block], ...]`, heights inclusive, whole dollars). **Unset
  means the ladder above** (`SPONSOR_PRICE_BANDS_DEFAULT` in `server.py`). Set but unparseable, empty, a
  zero, negative or fractional price, a band starting below block 1, or overlapping bands all mean unpriced.
  A span with any block outside every band is unpriced. Unpriced spans cannot be sponsored.
- `SPONSOR_BTC_USD`, dollars per bitcoin. **There is no default**: a price written into the code would be
  wrong within the day. Without it a quote gives the dollar minimum and no sats, and a request is refused
  (`503`). The rate goes stale as the market moves, so it has to be kept current by whoever runs the
  coordinator; when payments are built, invoicing in dollars through BTCPay can supply the rate instead.
- The minimum in sats is `ceil(min_usd x 100,000,000 / SPONSOR_BTC_USD)`, rounded up so it never falls
  short. Both `min_usd` and `min_sats` are stored with each request, so a later price or rate change does not
  move the minimum a sponsor was quoted, and the public rule compares what settled against `min_sats`.
- A preview with sponsorship open: `SPONSOR_OPEN=1 SPONSOR_BTC_USD=77400` (the bands default).

## Holds

A sponsorship **holds** its blocks from the moment it is paid at least its minimum until they are proven:
`status IN ('paid','proving') AND paid_sats >= min_sats` (`SPONSOR_HOLD_SQL` in `server.py`).

- **Normal workers are never offered a held block.** Holds are added to `coverage_and_held()`, the one set
  `claim()` and `pick()` both read, so they cover `claim()`'s scan, its frontier-blocker re-offer (#284) and
  `/api/pick`. An unpaid `requested` row, an `underpaid` one and a `paid` row below its minimum hold nothing.
- **The bot does not claim.** Claims are unsigned, so a "the bot's key may claim held blocks" rule could be
  spoofed by anyone who copies the key. The bot picks held blocks itself and proves them with
  `hazync-worker run <n>`, which needs no claim (see `docs/SPONSOR_BOT.md`).
- **A proof from anyone is still accepted**, as every proof is: submit never looks at claims or holds. A
  held block proven by someone else simply counts towards the sponsorship.
- **The hold ends when the whole span is covered.** `submit()` moves the sponsorship to `proven` (with
  `proven_at`) as soon as every block of the span is covered by verified ranges. Cancelling or refunding
  a sponsorship also ends its hold.
- **Only open blocks can be sponsored.** A quote or request is refused (`409`) if any block of the span is
  already proven, is being proven by a live claim, or is held by another sponsorship.
- **Holds never expire**, so they are watched. `/api/state`'s `blocked` names the sponsorship
  (`"sponsorship": id`) when the frontier's next block is held, and is calm about it until the hold is older
  than `SPONSOR_HOLD_ALERT` (default 6 hours), when it sets `needs_attention`. `/api/block/<n>` carries
  `"held": {"sponsorship": id, "since": seconds}`.
- ⚠ **Open with payments (step 3):** a hold should start when the invoice is created, for the invoice's
  payment window, so nobody proves or sponsors the blocks while the sponsor is paying. Until payments
  exist only a paid status holds.

## The private link and the public list

- **The link.** A successful request returns a `token` once (`secrets.token_urlsafe(24)`). Only its sha256
  is stored (`token_hash`), so the database cannot give it back and a leaked copy of the database cannot
  open anyone's page. The site shows it as `/sponsors/#<token>`: after the `#`, it never reaches a server
  log or a referrer. The link shows the sponsorship in every status, including before payment and when it
  is `underpaid`, which the public list never shows.
- **The list.** `GET /api/sponsors` is every public sponsorship, newest payment first, with the amount
  paid, how many of its blocks are proven and how many paid sponsorships are ahead of it. It never carries
  the pledge, the invoice, the note or the link.

## API

| Call | What it does |
|------|--------------|
| `GET /api/sponsor` | `{"open": bool, "max_blocks": n, "payments": false, "priced": bool, "bands": [[lo, hi, usd], ...], "btc_usd": n or null, "name_max": 40}` |
| `GET /api/sponsor/quote?lo=&hi=` | `{"lo", "hi", "blocks", "min_usd": n or null, "min_sats": n or null, "btc_usd": n or null, "priced": bool}`; answers while closed too |
| `POST /api/sponsor` `{"lo", "hi", "name", "amount_sats"}` | `503` while closed, unpriced, or with no bitcoin price. `400` (with `min_sats` and `min_usd`) below the minimum. Open: records a `requested` row, `202` with `id, token, status, lo, hi, blocks, name, min_usd, min_sats, pledged_sats, message` |
| `GET /api/sponsor/status/<token>` | one sponsorship: `id, lo, hi, blocks, name, status, min_usd, min_sats, pledged_sats, paid_sats, created_at, paid_at, proven_at, proven_blocks, queue_ahead, public`; `404` for an unknown link |
| `GET /api/sponsors` | `{"sponsorships": [{id, name, lo, hi, blocks, status, paid_sats, min_usd, min_sats, paid_at, proven_at, proven_blocks, queue_ahead}], "open", "priced"}`, public rows only |
| `GET /api/block/<n>` | includes `sponsor` (id, name, span, status) for a public sponsorship covering the block, else `null` |

Validation (the quote and the request share it): `1 <= lo <= hi <= tip`, at most `SPONSOR_MAX_BLOCKS`
(default 1000) blocks, and not a span that is already anchored (`409`). The request also needs a name of 1 to
40 characters (`name_max` in `GET /api/sponsor`; code points, after whitespace is collapsed) with no
control, formatting, surrogate or private-use character (a zero-width joiner is formatting, so joined emoji are
refused; an emoji newer than the coordinator's Python is not). The site's form applies the same rule before
sending, so a name it allows is never refused here, and `amount_sats`, a whole number of sats,
at least the minimum.

`queue_ahead` counts public `paid` sponsorships paid earlier; it is `null` unless the sponsorship is itself
`paid`. `proven_blocks` counts blocks in the span that are proven, folded or anchored, from the same runs
as `/api/blockstatus`.

## Not built, in the order it has to be built

1. **Measure what a block costs, and check the ladder against it.** The ladder above rests on a measured
   fleet cost up to about 57,000 and bundle-size extrapolation to 230,000. Run the bot on the project's own
   budget first and compare what blocks in each band really cost, overhead included, with their price;
   measure above 230,000 before pricing any of it. Prices stay at about **twice** the estimated cost
   (decided 2026-09-13): rented GPU prices move, and a sponsorship that pays its minimum must be enough to
   prove its blocks.
2. **Payments.** BTCPay invoices, settlement moving a row to `paid` (or `underpaid`, kept as a donation),
   expiry, and refunds.
3. **The bot** (`coordinator/sponsor_bot.py` is a dry-run skeleton): RunPod pods, a spending limit, the
   normal worker under the bot's key, `proving` and `proven` updates.
4. ~~**Priority.**~~ Built as **holds** (above): paid spans are kept for the bot, and nobody else is offered them.
5. **Moderation.** Sponsor names need the takedown handling contributor names already have.

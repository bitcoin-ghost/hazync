# Sponsoring blocks

Status: **the records, the minimum, the private link, the public list, the API, the site's form and the
BTCPay payments code exist; the proving bot cannot run live yet** ([#351](https://github.com/bitcoin-ghost/hazync/issues/351)).
Sponsorship is closed on the live coordinator (`SPONSOR_OPEN` unset) and payments are not connected there, so the
site's form says so and nothing is recorded. Payments must stay unconnected until the bot can prove what is paid for.

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
| `invoiced` | `POST /api/sponsor` with payments connected: a BTCPay invoice is open | yes |
| `paid` | the payment poller, on settlement of **at least** `min_sats`, **late or not** | yes |
| `underpaid` | the payment poller, when the invoice ends with **less** than `min_sats` settled; kept as a donation | yes |
| `proving` | sponsor bot, when it starts pods | no |
| `proven` | `submit()`, when every block of the span is covered by verified ranges | yes |
| `expired` | the payment poller, invoice ended with nothing paid | yes |
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

### The price ladder (decided 2026-09-14)

| Blocks | Minimum per block | What is measured |
|--------|------------------:|------------------|
| 1 to 200,000 | $1 | trial: slowest block $0.18 (2.0 MB); heaviest 1% about $0.27 |
| 200,001 to 400,000 | $2 | trial to 230,000: slowest block $0.22 (3.7 MB); heaviest 1% to 230,000 about $0.57; above 230,000 nothing |
| 400,001 to 600,000 | $3 | nothing |
| 600,001 to 800,000 | $4 | nothing |
| 800,001 to 1,000,000 | $5 | one block: 966,256, $1.39 of GPU time |
| above 1,000,000 | $6 | nothing; no block there is mined yet |

- **The trial (2026-09-14).** The sponsor bot proved two blocks in each of the old bands up to 230,000 on
  one RTX 4090 at $0.74 an hour: 20 s ($0.004) at 90,002 up to 1,047 s ($0.22) at 220,000. That is 248 to
  441 seconds per MB of bundle. At the slowest rate, the heaviest 1% of blocks (by bundle size, measured on
  the coordinator) cost about $0.27 up to 200,000 and $0.57 from 200,000 to 230,000: under half the price.
  The heaviest single blocks, 8.68 MB at 197,678 and 16.87 MB at 228,538, cost about $0.79 and $1.53:
  under their price, but not by 2x.
- **Above 230,000 no bundles exist yet,** so no pod can prove those blocks. A sponsorship there is taken and
  held, and waits until the bridge builds its bundles. The quote's `waiting` counts the blocks that wait, and
  the form says so before anyone pays. The bot never rents a pod for them.
- **From 230,000 to 1,000,000 the prices are not checked against a measurement,** except block 966,256
  ($1.39 of GPU time across 27 cards, 2026-09-10). Run a trial in each band once its bundles exist.
- **Above 1,000,000** the top band runs to `SPONSOR_BAND_TOP` (10^9), which stands for "and above". A block
  cannot be sponsored before it is mined.

### Setting it

- `SPONSOR_PRICE_BANDS` (JSON `[[lo, hi, usd_per_block], ...]`, heights inclusive, whole dollars). **Unset
  means the ladder above** (`SPONSOR_PRICE_BANDS_DEFAULT` in `server.py`). Set but unparseable, empty, a
  zero, negative or fractional price, a band starting below block 1, or overlapping bands all mean unpriced.
  A span with any block outside every band is unpriced. Unpriced spans cannot be sponsored.
- `SPONSOR_BTC_USD`, dollars per bitcoin. **There is no default**: a price written into the code would be
  wrong within the day. Without it a quote gives the dollar minimum and no sats, and a request is refused
  (`503`). The rate goes stale as the market moves, so it has to be kept current by whoever runs the
  coordinator. **With payments connected it is not used**: the rate is BTCPay's store rate (see Payments).
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
- **Only the sponsorship's own key can prove a held block.** `submit()` refuses (`403`) a proof that would
  newly cover a held block unless its key is registered for THAT sponsorship in `sponsor_keys` (the bot
  registers one key per sponsorship). The sponsor paid for those blocks, so another prover cannot take
  them, or their credit, by submitting them directly; claims already never offer them. A fold that only
  re-expresses held blocks already proven takes nothing and is accepted. Tested adversarially over HTTP on
  a copy of the live database (2026-09-14): before this rule, another key's direct submit of a held block
  was accepted and credited to it.
- **The hold ends when the whole span is covered.** `submit()` moves the sponsorship to `proven` (with
  `proven_at`) as soon as every block of the span is covered by verified ranges. Cancelling or refunding
  a sponsorship also ends its hold.
- **Only open blocks can be sponsored.** A quote or request is refused (`409`) if any block of the span is
  already proven, is being proven by a live claim, or is held by another sponsorship.
- **Holds never expire**, so they are watched. `/api/state`'s `blocked` names the sponsorship
  (`"sponsorship": id`) when the frontier's next block is held, and is calm about it until the hold is older
  than `SPONSOR_HOLD_ALERT` (default 6 hours), when it sets `needs_attention`. `/api/block/<n>` carries
  `"held": {"sponsorship": id, "since": seconds}`.
- **An open invoice holds its blocks too**, for its payment window, so nobody is offered them and no other sponsor
  can take them while the sponsor pays (see Payments). That hold is **soft**: `submit()` still accepts a proof of
  them from any prover, because an unpaid invoice costs nothing to open and must not be able to lock provers out.

## Payments

BTCPay Server (`donate.hazync.org`, [hazync-admin `infrastructure/btcpay-box.md`](https://github.com/hazync/hazync-admin/blob/main/infrastructure/btcpay-box.md))
takes the payment; the coordinator opens invoices and asks BTCPay what was paid. Connected only when
`BTCPAY_URL`, `BTCPAY_STORE_ID` and a readable `BTCPAY_API_KEY_FILE` are all set (`payments_enabled()`).

- **The key** is a Greenfield API key for the one store, with only *create invoices*, *view invoices* and *view store
  settings* (the rate). It lives in a root-only file named by `BTCPAY_API_KEY_FILE`, never in the unit's
  environment, where `systemctl show` would print it.
- **The rate** is BTCPay's store rate (`GET /api/v1/stores/<store>/rates?currencyPair=BTC_USD`), the same source that
  prices invoices, cached `SPONSOR_RATE_TTL` (60 s). If BTCPay stops answering the last good rate is used for
  `SPONSOR_RATE_STALE` (10 minutes), then there is no rate and requests are refused.
- **The invoice** is for the pledge, in **SATS**, the unit `min_sats` and the public rule are kept in, so what settles
  compares with the minimum exactly. It is payable for `SPONSOR_INVOICE_MINUTES` (30), BTCPay watches it for
  `SPONSOR_INVOICE_WATCH` (3 days) after that, and its checkout sends the sponsor back to their private link.
  `POST /api/sponsor` answers `202` with `checkout_url`; if BTCPay fails, `503` and nothing is recorded.
- **Requests are serialised** (`_sponsor_request_lock`), span check included, so two sponsors cannot both be given an
  invoice for the same block.
- **Unpaid holds are capped**, because an invoice is free to open: at most `SPONSOR_UNPAID_HOLD_MAX` (5,000) blocks
  under unpaid invoices in total, and `SPONSOR_OPEN_INVOICES_PER_IP` (3) open invoices per address; over either,
  `429`. The address is set by the request handler, never taken from the body.
- **The poller** (`sponsor_payments_poll`, a thread every `SPONSOR_POLL_S`, 30 s) asks BTCPay about every sponsorship
  that is `invoiced`, `expired` or `underpaid` and still inside its watch window. Only payments BTCPay calls
  *Settled* count:

  | What BTCPay reports | Becomes |
  |---|---|
  | settled at least `min_sats`, at any time | `paid` (late payments are **credited**, decided 2026-09-17) |
  | a payment seen but not confirmed | stays `invoiced`; its hold is pushed out `SPONSOR_CONFIRMING_HOLD` (2 h), re-armed each poll |
  | invoice expired or invalid, something settled below `min_sats` | `underpaid`; becomes `paid` if the rest arrives while watched |
  | invoice expired or invalid, nothing settled | `expired`; becomes `paid` if a payment arrives while watched |
  | invoice marked by hand in BTCPay | left alone and logged: the operator sets the status |

  A sponsorship paid late for a span other provers finished meanwhile goes straight to `proven`. Every transition
  is guarded by the status it moves from, so a repeated poll changes nothing. Each payment is recorded in
  `sponsor_payments` (on-chain `txid-vout` or Lightning payment hash, sats, status) for the accounts ledger.
- **Not built:** refunds, a webhook (polling every 30 s is fast enough for this and needs nothing reachable on the
  coordinator), and the public accounts ledger itself.

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
| `GET /api/sponsor` | `{"open": bool, "max_blocks": n, "payments": bool, "priced": bool, "bands": [[lo, hi, usd], ...], "btc_usd": n or null, "name_max": 40}` |
| `GET /api/sponsor/quote?lo=&hi=` | `{"lo", "hi", "blocks", "min_usd": n or null, "min_sats": n or null, "btc_usd": n or null, "priced": bool, "waiting": n}`; `waiting` counts blocks with no bundle yet; answers while closed too |
| `POST /api/sponsor` `{"lo", "hi", "name", "amount_sats"}` | `503` while closed, unpriced, or with no bitcoin price. `400` (with `min_sats` and `min_usd`) below the minimum. Open without payments: records a `requested` row, `202` with `id, token, status, lo, hi, blocks, name, min_usd, min_sats, pledged_sats, message`. With payments: the same plus `checkout_url` and `invoice_expires_at`, status `invoiced`; `503` if BTCPay fails (nothing recorded), `429` over the unpaid-hold caps, `409` if a block is waiting for another sponsor's payment |
| `GET /api/sponsor/status/<token>` | one sponsorship: `id, lo, hi, blocks, name, status, min_usd, min_sats, pledged_sats, paid_sats, created_at, paid_at, proven_at, proven_blocks, queue_ahead, public`, plus `checkout_url` and `invoice_expires_at` while `invoiced`; `404` for an unknown link |
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

1. **Measure what a block costs above 230,000.** The ladder is checked against the 2026-09-14 trial up to
   230,000 only. Once the bridge builds bundles in a band, run a trial there and compare what its blocks cost,
   overhead included, with their price. Prices stay at about **twice** the estimated cost: rented GPU prices
   move, and a sponsorship that pays its minimum must be enough to prove its blocks.
2. ~~**Payments.**~~ Built (see Payments), except refunds and the public accounts ledger. Not connected live until
   the bot can run (#351).
3. **The bot** (`coordinator/sponsor_bot.py` is a dry-run skeleton): RunPod pods, a spending limit, the
   normal worker under the bot's key, `proving` and `proven` updates.
4. ~~**Priority.**~~ Built as **holds** (above): paid spans are kept for the bot, and nobody else is offered them.
5. **Moderation.** Sponsor names need the takedown handling contributor names already have.

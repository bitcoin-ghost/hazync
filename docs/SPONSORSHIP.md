# Sponsoring blocks

Status: **the records, the minimum, the private link, the public list, the API and the site's form exist;
payments and the proving bot do not.** Sponsorship is closed on the live coordinator (`SPONSOR_OPEN`
unset) and unpriced (`SPONSOR_PRICE_BANDS` unset), so the site's form says so and nothing is recorded.

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
| `proven` | sponsor bot, when every block in the span is verified | no |
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

Every span has a **minimum**, in sats, and a sponsor may pay more. The minimum is a deliberate
**overestimate** of the compute: card prices change by the hour, and it is better to know a paid block can be
proven than to take too little and have to withhold the sponsor's name. Paying at least the minimum is what
earns the name.

Prices come only from `SPONSOR_PRICE_BANDS`, a JSON list of `[lo, hi, sats_per_block]` with heights
inclusive, for example `[[1, 200000, 150], [200001, 1000000, 900]]` (illustrative shape only, not a
price). The minimum for a span is the sum of each block's band price. **There is no default price**: unset,
unparseable, a zero or negative price, or overlapping bands all mean unpriced, and a span with any block
outside every band is unpriced. Unpriced spans cannot be sponsored.

⛔ The bands must come from **measured** cost per height band (step 1 below) times a safety margin, not
from a guess. `min_sats` is stored with each request, so a later price change does not move the minimum a
sponsor was quoted.

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
| `GET /api/sponsor` | `{"open": bool, "max_blocks": n, "payments": false, "priced": bool}` |
| `GET /api/sponsor/quote?lo=&hi=` | `{"lo", "hi", "blocks", "min_sats": n or null, "priced": bool}`; answers while closed too |
| `POST /api/sponsor` `{"lo", "hi", "name", "amount_sats"}` | `503` while closed or unpriced. `400` (with `min_sats`) below the minimum. Open: records a `requested` row, `202` with `id, token, status, lo, hi, blocks, name, min_sats, pledged_sats, message` |
| `GET /api/sponsor/status/<token>` | one sponsorship: `id, lo, hi, blocks, name, status, min_sats, pledged_sats, paid_sats, created_at, paid_at, proven_at, proven_blocks, queue_ahead, public`; `404` for an unknown link |
| `GET /api/sponsors` | `{"sponsorships": [{id, name, lo, hi, blocks, status, paid_sats, min_sats, paid_at, proven_at, proven_blocks, queue_ahead}], "open", "priced"}`, public rows only |
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

1. **Measure what a block costs.** Run the bot on the project's own budget first. Cost rises steeply with
   height (a median bundle is 354x larger at 220k than at the start), and a milestone night spent $95.34
   of RunPod credit for one $1.39 proof. A price has to come from measured cost per height band,
   overhead included, and `SPONSOR_PRICE_BANDS` set at **twice** that estimated cost (decided 2026-09-13):
   rented GPU prices move, and a sponsorship that pays its minimum must always be enough to prove its blocks.
2. **Payments.** BTCPay invoices, settlement moving a row to `paid` (or `underpaid`, kept as a donation),
   expiry, and refunds.
3. **The bot** (`coordinator/sponsor_bot.py` is a dry-run skeleton): RunPod pods, a spending limit, the
   normal worker under the bot's key, `proving` and `proven` updates.
4. **Priority.** Paid spans claimed ahead of open work. The claim path is not touched until then.
5. **Moderation.** Sponsor names need the takedown handling contributor names already have.

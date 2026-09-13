# Sponsoring blocks

Status: **the records, the API and the button exist; payments and the proving bot do not.** Sponsorship
is closed on the live coordinator (`SPONSOR_OPEN` unset), so the site's Sponsor button says so and
nothing is recorded.

## What it is for

Anyone can pay for a block, or a range of blocks, to be proven. The payment funds GPU time; a bot starts
pods, runs the ordinary worker on those blocks, and the block map shows **Sponsor: <name>** next to the
prover. The sponsor's name is a coordinator record. It is never put inside a proof: changing what the
guest commits changes the prover program ID.

## The flow

```
requested -> invoiced -> paid -> proving -> proven
          \-> cancelled          \-> refunded
```

| Status | Set by | Built |
|--------|--------|-------|
| `requested` | `POST /api/sponsor` | yes, when `SPONSOR_OPEN=1` |
| `invoiced` | payment integration (BTCPay: Lightning and on-chain) | no |
| `paid` | payment integration, on settlement | no |
| `proving` | sponsor bot, when it starts pods | no |
| `proven` | sponsor bot, when every block in the span is verified | no |
| `cancelled`, `refunded` | operator | no |

A sponsor's name is published **only** from `paid` onwards. An unpaid request is text anyone can type;
showing it would let anyone put a name on any block.

## API

| Call | What it does |
|------|--------------|
| `GET /api/sponsor` | `{"open": bool, "max_blocks": n, "payments": false}` |
| `POST /api/sponsor` `{"lo", "hi", "name"}` | `503` while closed. Open: validates and records a `requested` row, `202` |
| `GET /api/block/<n>` | includes `sponsor` (name, span, status) for a paid sponsorship covering the block, else `null` |

Validation: `1 <= lo <= hi <= tip`, at most `SPONSOR_MAX_BLOCKS` (default 1000) blocks, a name of 1 to 40
visible characters with no control or formatting characters, and not a span that is already anchored.

## Not built, in the order it has to be built

1. **Measure what a block costs.** Run the bot on the project's own budget first. Cost rises steeply with
   height (a median bundle is 354x larger at 220k than at the start), and a milestone night spent $95.34
   of RunPod credit for one $1.39 proof. A price has to come from measured cost per height band,
   overhead included.
2. **Payments.** BTCPay invoices, settlement moving a row to `paid`, and refunds.
3. **The bot** (`coordinator/sponsor_bot.py` is a dry-run skeleton): RunPod pods, a spending limit, the
   normal worker under the bot's key, `proving` and `proven` updates.
4. **Priority.** Paid spans claimed ahead of open work. The claim path is not touched until then.
5. **Moderation.** Sponsor names need the takedown handling contributor names already have.

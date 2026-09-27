Seventy-four commits since v0.21.7. The headline is that Hazync can now be paid: sponsorship
and donations are live, and the first sponsored block was paid over Lightning and proved by a
bot that rented a card for it. Most of the rest is what running the thing taught us.

## Money

Block sponsorship works end to end. Name a block, pay in bitcoin on-chain or over Lightning,
and a bot rents a GPU, proves it, releases the pod and credits you on the board. Donations are
open on the same server, and every one received is published.

⚠ One sponsorship has been proved this way. The price ladder ($1/block below 200,000 rising to
$6 above 1,000,000) is a starting point: for block 196,001 it projected $0.01 of GPU and the
true figure was $0.19, because cost follows the UTXO set and a band median predicts one block
badly.

## The bridge follows the tip

FINALITY now defaults to 0, so the bridge tracks the chain head rather than trailing it, with
reorg detection and an undo log behind it.

## What the tip runs cost, and what they taught us

The aggregate is now chosen by MEASURING its link rather than by whichever pod answered ssh
first — 12.48 GB flows through that one card on a large block. Slow workers are cut before the
clock starts, on measurement rather than on the data-centre label.

Every block's aggregate log now survives the run. It used to be deleted at the start of each
block, so a four-block run could report segment counts for one.

Measured on block 968,340: 9,600 segments, 31.8 minutes on 15 cards, and the chain moved three
blocks past us while we proved it. Join round-trips across six data centres ranged from 0.4 s
to 107.4 s — a 268x spread — and the fold waits for the slowest peer at every level.

## Defects worth naming

  * the sweep threshold read satoshis as BTC and announced a hot wallet of £1.29 billion,
    firing a false alert. Invisible until the first non-zero balance arrived, because zero
    satoshis and zero BTC are the same number.
  * a sponsor who paid was left on the payment page: the invoice carried a redirect URL but
    not `redirectAutomatically`, which BTCPay defaults to false.
  * the sponsor bot asked RunPod for `cloudType: ALL`, which includes community hosts that
    mostly never start. The fix had landed in the tip driver and not in the bot, though one
    imports the other's constants.
  * a run could claim board work under whatever identity the box happened to hold, and did.
  * the unit-drift check tore quoted `Environment=` values in half, so the repo's own unit
    tripped the repo's own checker.
  * an unquoted `Environment=` meant the coordinator had never been checking its root
    filesystem: systemd splits on whitespace and discards the rest.

## Also

The card catalogue ranks rented GPUs by measured cost per proof rather than by price per hour
— a mixed fleet once ran 40% slower AND cost more. The live dashboard was rebuilt: a dead feed
no longer reports "updated 0s ago", a card that stops answering no longer stops billing, and a
verified block counts in the headline.

Every fix above ships with a control that reproduces the bug, so a regression fails loudly
rather than passing quietly.

## What this is groundwork for

The next two targets are to follow Bitcoin's tip for an hour, then for a day, finishing every
block before the next one arrives. This release is most of the machinery that needs:

  * the bridge tracks the chain head instead of trailing it, so there is always a fresh block
    to prove;
  * the aggregate — the one card every segment is pushed from — is chosen on its measured
    link, and workers that cannot keep up are cut before the clock starts;
  * idle time between tip blocks goes into proving the board rather than into nothing;
  * each block's evidence survives the run, so a session that falls behind can be explained
    rather than guessed at;
  * fleets are ranked by measured cost per proof, so the cards rented are the ones that
    actually finish.

⚠ Neither target is met. Two hours have been run: every block was proved and verified at the
tip, but on the second the fleet fell three blocks behind on a large one. Keeping pace is a
question of enough cards and a short enough join tail, and this release is what makes that
measurable rather than a matter of opinion.

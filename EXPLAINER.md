# Hazync, in plain English

*No jargon. No maths background needed. If you've ever wondered "how does my Bitcoin wallet actually
know the money is real?" — this is for you. It explains what we built, why it matters, and how you can
help.*

---

## The problem: checking Bitcoin is getting harder every day

Bitcoin has no bank in the middle. So how does anyone know a coin is real and hasn't been spent twice?

The answer is beautifully stubborn: **every full node re-checks everything, itself, from the very
beginning.** When you start a new Bitcoin node, it downloads the entire history — every transaction
since 2009 — and re-verifies each one. Every signature. Every rule. All of it. Only then does it trust
the current balance sheet.

This is what makes Bitcoin trustless. It's also the catch:

- It takes a powerful computer the best part of a day, sometimes longer.
- The history only ever grows. Fifteen years now; fifty years later.
- Most people never do it. They use an app that just *tells* them the balance and hope it's honest.

Every person who gives up and trusts someone else's answer makes Bitcoin a little more centralised.
The dream is a world where anyone — on a cheap laptop, a phone — can check the whole chain for
themselves in seconds. Today they can't.

## The idea: a proof you can check without redoing the work

Imagine a teacher with 10,000 exam papers to grade. Grading them all takes weeks. Now imagine a magic
stamp: the teacher grades everything **once**, and produces a small sealed certificate that says *"I
checked all 10,000 papers and they're all correct."* Anyone can glance at the certificate and be
**certain** the grading was done properly — without regrading a single paper. If even one answer had
been wrong, the certificate simply could not have been made.

That magic stamp is real. In computer science it's called a **zero-knowledge proof** (the name is
unfortunate — it just means "a proof you can check quickly without re-running the work"). You run a
computation, and out pops a tiny certificate. Anyone can verify the certificate in a moment, and it is
mathematically impossible to fake one for work that wasn't actually done correctly.

**Hazync applies that magic stamp to Bitcoin's entire history.**

Instead of every new node re-checking fifteen years of transactions, someone does it **once** and
produces a small proof. Everyone else just checks the proof — in seconds, on a phone — and *knows*, with
the same certainty as if they'd done all the work themselves, that every block in Bitcoin's history
follows every rule.

## The hard part — and why ours is different

Here's the subtle bit that separates a toy from the real thing.

To make the certificate, you have to run Bitcoin's rules *inside* the magic-stamp machine. The easy,
tempting way is to **rewrite** Bitcoin's rules in whatever language the machine likes. Almost everyone
who has tried this took that shortcut.

The problem: a rewrite is a **photocopy**. If your photocopy of the rulebook differs from the real
rulebook in even one comma — one weird edge case out of millions — your certificate proves the wrong
thing. It says "valid" for something the real Bitcoin would reject. And Bitcoin's real rulebook is
famously full of tiny, load-bearing quirks that have to match *exactly*.

**Hazync doesn't rewrite anything.** We took Bitcoin Core's own actual source code — the exact same
program the whole network runs, unchanged — and ran *that* inside the magic-stamp machine. It's the
original rulebook, not a copy. There is no "did we translate it faithfully?" gap, because there is no
translation. This is the part everyone else got stuck on, and it's the part we cracked.

*(For the technically curious: we compile Bitcoin Core v28's real script interpreter, signature-hashing,
and its cryptography library and run them, unmodified, inside a zero-knowledge virtual machine. The
full details, and an honest list of what's proven vs still open, are in `docs/SOUNDNESS.md` and
`SECURITY.md`.)*

## What actually works today (and what doesn't — honestly)

We are allergic to hype, so here's the straight version.

**What's proven and working:**
- Real Bitcoin Core code, unmodified, running inside the proof machine. ✅
- Every kind of Bitcoin transaction — old-style, modern "SegWit", and the newest "Taproot". ✅
- Whole real blocks from the actual Bitcoin chain, checked end-to-end, producing a real certificate you
  can verify. We did this on genuine mainnet blocks, including a big modern one with hundreds of inputs. ✅
- A way to **stitch** block certificates together into one certificate for a whole *range* of history —
  and to do that stitching in parallel, on many machines at once. ✅

**What's not done yet:**
- The full run from Bitcoin's first block to today. Not because we don't know how — we do, and we've
  proven every piece works — but because making the certificate for fifteen years of history takes a
  **lot** of computing power (lots of graphics cards, for a while). We have the method; we don't yet
  have all the machines. That's the honest bottleneck.
- Independent experts poking holes in it. We've audited our own work hard and fixed what we found, but
  self-review isn't the same as outside review. We *want* people to try to break it.

This is roughly where a well-known earlier project called ZeroSync stopped and moved on. We think we've
gone past the wall they hit — mainly by not rewriting the rules — but we say "we think" on purpose. The
way you find out for sure is to invite the world to check.

## Why this matters (the payoff, plainly)

If Hazync is finished:

- **Anyone can run a fully-trustless Bitcoin node in seconds, on cheap hardware.** Download a small
  proof, check it, done. No day-long sync. No trusting someone else's server.
- **Bitcoin gets more decentralised, not less, as it grows.** Today, growing history pushes people
  toward trusting third parties. This flips that.
- **It uses Bitcoin's own rules, so there's nothing new to trust.** We're not asking you to believe a
  new rulebook — we're proving the existing one was followed.

That's the prize: keeping "don't trust, verify" possible for everyone, forever, even as the chain grows.

## How you can help

This is a public good. It doesn't make anyone rich; it makes Bitcoin harder to capture. If that's the
kind of thing you want to exist, here's how you can move it forward — pick whatever fits you:

- **Lend computing power.** The one big remaining task — making the certificate for all of history — is
  the kind of job that splits into thousands of independent pieces. Many people each proving a small
  chunk, then stitching the results, gets it done far faster than any single group. If you have
  graphics cards (a gaming PC counts) or cloud credits, you can prove a slice. Think of it as a
  community barn-raising for Bitcoin's history.
- **Donate.** Renting the graphics cards to complete the full run costs real money. Funding buys
  compute time directly. Small amounts add up; a handful of committed cards can keep the tip current
  once the history is done.
- **Break it.** If you know cryptography or Bitcoin's internals, try to find a case where our proof says
  "valid" when it shouldn't. `SECURITY.md` lists exactly where we think the soft spots are — that's your
  starting map. Finding a real flaw is the most valuable contribution there is.
- **Reproduce it.** Follow the steps in the `README` on your own machine and confirm you get the same
  result we did. Independent confirmation is worth more than any claim we make.
- **Spread the word.** Send this page to one person who'd care. Understanding is the first domino — no
  one helps build a thing they can't picture.

---

*Want the deep version? `README.md` is the technical overview, `docs/SOUNDNESS.md` is the exact security
claim and trust assumptions, `SECURITY.md` is our own hole-poking (and an open invitation to poke
harder), and `docs/HAZYNC_ARCHITECTURE.md` is how the full run gets done. We keep the honest caveats in writing on
purpose — a project like this is only worth anything if the claims are true.*

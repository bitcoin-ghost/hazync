# Field-operation profile

Counts which field operations one ECDSA verification actually performs, in stock libsecp256k1 on the
10x26 backend the RISC0 guest uses.

## Why this exists

hazync#129 proposes a field backend that keeps elements in `sys_bigint`'s native `[u32; 8]` form and
does modular multiplication in the precompile. Only `mul` and `sqr` gain from that. The other 26
functions in the backend contract do not.

And one of them gets *worse*. libsecp's 10x26 representation uses **lazy reduction**: magnitudes are
allowed to grow (to 32x) and normalisation is deferred, so `fe_add` is ten word additions with no
carry propagation and no reduction. A `[u32; 8]` representation is fully reduced by construction, so
every add needs a conditional subtract of p.

So the rewrite trades faster muls for slower adds, and whether it nets out depends on a ratio nobody
has measured. If adds dominate the EC inner loop, no amount of precompile speed rescues it.

Measure first. A day here can save the week that `docs/ACCELERATION.md` budgets for Step 1.

## What it does

Copies the pinned secp256k1 v0.5.1 source to a scratch dir, injects a counter into each field entry
point, builds it natively, and runs N ECDSA verifications. Nothing in the repo or in
`~/hazync-build/secp256k1` is modified.

## Run

```sh
tools/field-op-profile/run.sh          # defaults to 100 verifications
N=1000 tools/field-op-profile/run.sh
```

## Reading the result

The number that matters is `mul+sqr` against `add+negate+normalize*`. Roughly:

- muls dominate  -> the backend rewrite has room to win, proceed to Step 1
- adds comparable or greater -> the rewrite is at best a wash; reconsider before spending a week

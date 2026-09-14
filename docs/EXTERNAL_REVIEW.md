# What still needs outside eyes

Everything in this repo has been reviewed. Almost none of it has been reviewed by someone who could be
wrong in a different direction than we are.

This page exists because that gap kept being tracked as engineering work. It is not: no amount of test
writing closes it, and issues that engineering can never close rot in the tracker. hazync#50 carried an
"external review" item through five internal audits before this page took it over.

## What we already have, stated precisely

**Five internal audits** and **two external reviews** (SECURITY.md, rounds 10 & 11). Both external
passes were **AI-assisted full-source reviews, not a commissioned professional audit** — a distinction
SECURITY.md keeps deliberately, and this page keeps too. Only one audit has a standalone write-up,
`docs/AUDIT_2026-07.md` (SECURITY.md round 8); the others are recorded only where their findings were
fixed — `SECURITY.md`, `reproduce/METHOD_ID` and code comments.

They were not cheap talk. They found real defects, including one canonical-chain break (audit #3, F-1)
that had survived internal review because a comment, a test name and a fixture all agreed with each
other and none with the chain. Independent reviewers are how that class gets caught.

They also **agreed with each other** about where the residual risk is: the C++ bridge compiled into the
guest, and the accumulator. Two independent passes landing on the same two places is itself a finding.

## Before reading anything, verify something

Roughly a minute, on any Linux x86-64 box, no GPU and no chain data. The point is not that this proves
much — it proves one short range — but that a reviewer starts from a thing they checked themselves
rather than from our description of it.

```sh
curl -fsLO https://github.com/bitcoin-ghost/hazync/releases/latest/download/hazync-verify-x86_64-linux-gnu
chmod +x hazync-verify-x86_64-linux-gnu
curl -f https://bitcoinghost.org/hazync/api/spine/proof -o spine.snark
./hazync-verify-x86_64-linux-gnu --json spine.snark
```

That emits `"verified": true`, `"genesis_anchored": true`, the guest image id it verified against, and
the chain state a node would adopt. Two things are worth doing by hand with the output, because they
are what make it a claim about **Bitcoin** rather than a claim about itself:

- **Check `tip_hash` against any independent source.** It is in display order. At the time of writing
  the spine's tip matched `blockstream.info`'s hash for the same height exactly — if it ever does not,
  that is the most interesting bug in this repository and nothing else on this page matters.
- **Check the guest image id against `/api/meta` and `reproduce/METHOD_ID`.** All three must agree. A
  proof that verifies against the *wrong* guest is a proof of the wrong program.

`epoch_start_time` is **not** the genesis timestamp. It is the timestamp of the first block of the
retarget period containing the tip — block `⌊height / 2016⌋ × 2016` — because the verifier reports the
proof's *out*-boundary (`rs.out_epoch_start`, `verifier/src/lib.rs`). It equals `1231006505` only while
the tip is below height 2016. The genesis pin applies to the *in*-boundary (`docs/SPEC.md` §9), which
`--json` does not print; a spine that verifies has already passed it.

Note what this deliberately does not show: the spine is short, and it is not the `frontier` the board
reports. Those are different claims and the panel keeps them separate on purpose — the frontier is the
coordinator chaining many verified ranges, which you cannot check in one download; the spine is the
single file you can.

## The list, in the order worth spending on

### 1. A clean-machine reproducible build — highest value, lowest cost

Nobody outside this project has ever independently re-derived `METHOD_ID`.

```bash
docker build -f reproduce/Dockerfile .    # must print the id in reproduce/METHOD_ID
```

Five audits have verified the *pinning web* — that the toolchain, Core, secp256k1 and Cargo.lock are all
fixed, and that the id is consistently referenced everywhere. None has re-derived the id itself, because
every one of them (correctly) treated a local rebuild as out of scope.

Everything downstream rests on this. A proof verifies against an image id; if the id someone else builds
does not match the one we publish, either the build is not reproducible or the published id is not what
the source produces — and no other check in the project would notice. It is the cheapest item here and
the one with the most weight on it.

### 2. The accumulator — where a bug is unrecoverable

`accumulator/` and the `Stump` port in the guest. Formerly hazync#50.

This is the one component whose failure **reaches backwards**: every proof made against a broken
commitment is worthless, and the repair is re-proving from genesis. Everything else that breaks costs
time.

Internally covered, and worth being accurate about what that means:

- exhaustive single-delete equivalence against a full-forest oracle for every `n ≤ 40`, every index
- exhaustive cached-vs-pre-cache equivalence **across deletes**, plus the power-of-two neighbourhoods,
  with follow-up operations (a stale-entry off-by-one passes a single delete and fails after a later add)
- differential fuzzing of the cached `Forest` against the implementation it replaced — 192k runs
- ~893k-exec libFuzzer campaign on the hardened `delete`, with a positive control
- SEC-2 hardening: `delete` no longer trusts an unverified position

All of it self-audit, all of it small-`n`. The board is heading for ~200M leaves. It is also **new
code** — unlike the Core consensus path, nobody else has been running it for a decade.

### 3. The non-Core cryptographic code in the shipped guest

Since v0.21.0 the canonical guest is maximal-Core, not pure Core: two patches beneath libsecp256k1 sit on
the signature-verification path of every block, and neither is Core's code.

- **`patches/0012` — the `field_bigint2` field backend.** `prover/methods/guest/field_bigint2.h`,
  `prover/methods/guest/field_bigint2_impl.h` and `prover/methods/guest/src/field_bigint2.rs`. A lazy
  8×32-bit-limb representation, with add/sub/negate/half in C and multiply/square/inversion routed to the
  RISC0 bigint2 coprocessor. libsecp's algorithms above the field are untouched; the representation is
  not, and it admits states (elements ≥ p) that libsecp's own test suite never generates — two
  deliberately broken backends passed that suite and were caught only by the project's mod-p harness
  (`docs/FIELD_BIGINT2_BACKEND.md` §5b). **No corrupt-signature negative control has run on a CORE
  build.**
- **`patches/0013` — the `lift_x` witness hint.** `prover/methods/guest/src/liftx_hint.rs`. Replaces the
  square root in `secp256k1_ge_set_xo_var` with a host-supplied Y, checked as `y² == x³ + 7`. The argument
  that this accepts exactly what the square root would have produced is short (`docs/LIFTX_HINT.md` §3)
  and worth checking independently — including that a hostile or truncated table can only cost the
  fallback.

This is the newest consensus-relevant code in the repo, and no outside review has seen it: rounds 10 and
11 predate it by about a month.

### 4. The C++ bridge inside the guest

`prover/methods/guest/verify_input.cpp` — the glue between Core's real consensus code and the zkVM.

The consensus logic here is Core's own, compiled verbatim, which is the entire point. The *glue* is
ours: deserialisation, bounds handling, the canonical leaf construction, and the `core_*` exports that
pin every constant. A zkVM has no memory protection, so an out-of-bounds read is a silent wrong answer
rather than a crash.

Audit #5 read it in full and found one unguarded index (L-1, since fixed, no exposure). That is a good
sign, not a conclusion.

### 5. The coordinator under a HOSTILE-coordinator model

Currently reviewed under the stated model: `COORD_URL` is operator-chosen and trusted. That model is
honest today.

hazync#69 has landed (closed 2026-08-24): anyone can run a coordinator
(`docs/RUN_YOUR_OWN_COORDINATOR.md`), seed witnesses from a peer, and set `PEER_COORDINATORS` so
coordinators stop handing out each other's work. A worker still trusts the coordinator it is pointed at,
but there are now more of them to point at, so the worker's handling of coordinator responses is due
its second pass: `get()` in `coordinator/hazync` reads a response body with an unbounded `.read()`, and
claim-response handling, and anything else that treats the coordinator's answer as well-formed, needs
the same look.

### 6. ghostd integration — landed, and the highest-value consensus surface here

hazync#31, #42 and #46 are all met and closed — the work landed, which is exactly why it needs outside
eyes rather than less. This is the consumer side of the trust boundary: the code that decides a proof
is good enough to skip validating a million blocks. It is merged in bitcoin-ghost/ghost (PRs #543,
#627, #630, #631).

**Start here, because it is where a mistake is worst.** A bug in the prover produces a proof nobody
accepts. A bug here accepts a proof nobody should.

What is worth an outsider's eyes, roughly in order:

- **`haze::HazyncAdoption` — is the capability actually a capability?** Its constructor is private, so
  `Authorise()` is the only way to obtain one, and it returns nothing unless adoption was armed AND
  the proof verified AND the UTXO dump matched. It reaches `ActivateSnapshot` as an explicit argument
  rather than ambient state, which is what stops `loadtxoutset` acquiring a proof's authority for an
  arbitrary file. The claim to test: there is no path to adoption that skips a check.
- **The four chainparams dependencies it replaces.** Core refuses a snapshot at a height its
  developers did not compile in, and checks the coins against a hash they chose. Both are replaced —
  by a proof's authority and by the accumulator roots. A reviewer should confirm nothing else in that
  path still trusts a developer-chosen constant, and that where chainparams DOES know a height, its
  hash is still checked (two authorities disagreeing must be loud, not silently resolved).
- **`m_chain_tx_count` has no attested value.** The journal commits no transaction count, but the
  snapshot base must have a non-zero one or it never enters `setBlockIndexCandidates`. A proven lower
  bound (`height + 1`) is substituted. Only non-zero-ness is load-bearing; the magnitude feeds
  progress reporting, which under-reports. Worth checking that conclusion independently.
- **Rebuilt blocks and their txids.** A block rebuilt from stripped storage cannot compute its own
  txids — anything that had a scriptSig hashes to a different transaction — so the real ids travel on
  the block. `DisconnectBlock` keys every coin lookup on them. The failure this prevents is silent:
  wrong ids miss, spent coins survive, and the block reports only "unclean".
- **The distinction the code got wrong three times:** *is this NODE hazed* versus *is this BLOCK's
  payload stripped*. Conflating them marked genesis stripped though it is written whole, and a fresh
  hazed node could not start while every unit test passed. Grep for `IsHazeMode()` and ask, at each
  site, which question is being asked.

**What is NOT established, so a reviewer does not have to discover it:**

- **B4 acceptance.** That an adopted chainstate is byte-identical to one built by validating every
  block has been shown at a low height, not at a height with real transaction volume. Outstanding.
- **CI does not build ghost-core.** Only the release workflow touches cmake, on tag push. Every green
  check on those PRs is the Rust workspace; the C++ evidence is local runs. Do not read a green tick
  as "this compiled in CI".
- **The hazed→hazed receive guard is unreachable in practice.** A hazed node declines `NODE_NETWORK`,
  so nothing requests blocks from it. The guard is exercised by construction, not by transfer.

**The harnesses are more informative than the diff.** `test/hazync/` in the ghost repo carries the
adversarial suite, a live mainnet adoption test, a hazed-node suite and a two-node test. Every
substantive bug found in this work came from running those, not from reading the code.

## What a reviewer should be told

- **The guest is the authority.** `accumulator/`'s `Stump` is the readable spec; the guest's
  `utreexo.rs` is what is proven, and it carries hardening the host oracle does not.
- **`reproduce/METHOD_ID` is the source of truth for the id**, and the container is the only place it is
  canonically derived. A local build legitimately differs (it embeds `CARGO_HOME`).
- **`SECURITY.md` is a real changelog of findings**, including our own mistakes and retractions. Reading
  it is faster than rediscovering them.
- **Past audit findings are in `SECURITY.md`, `docs/AUDIT_2026-07.md` and `reproduce/METHOD_ID`.**
  Re-treading them is allowed but is not where the value is.

## Not on this list, deliberately

**Core's consensus code itself.** It is compiled from a pinned tag with only the portability patches
`0001` and `0002` applied, and has had far more review than we could commission. Auditing it here would
be auditing Bitcoin Core, and the pinning is what needs checking (item 1), not the code. The two
libsecp256k1 patches beneath it are not Core's, which is why they are item 3.

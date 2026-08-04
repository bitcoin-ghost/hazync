# What still needs outside eyes

Everything in this repo has been reviewed. Almost none of it has been reviewed by someone who could be
wrong in a different direction than we are.

This page exists because that gap kept being tracked as engineering work. It is not: no amount of test
writing closes it, and issues that engineering can never close rot in the tracker. hazync#50 carried an
"external review" item through five internal audits before this page took it over.

## What we already have, stated precisely

**Five internal audits** and **two external reviews** (SECURITY.md, rounds 10 & 11). Both external
passes were **AI-assisted full-source reviews, not a commissioned professional audit** — a distinction
SECURITY.md keeps deliberately, and this page keeps too.

They were not cheap talk. They found real defects, including one canonical-chain break (audit #3, F-1)
that had survived internal review because a comment, a test name and a fixture all agreed with each
other and none with the chain. Independent reviewers are how that class gets caught.

They also **agreed with each other** about where the residual risk is: the C++ bridge compiled into the
guest, and the accumulator. Two independent passes landing on the same two places is itself a finding.

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

### 3. The C++ bridge inside the guest

`prover/methods/guest/verify_input.cpp` — the glue between Core's real consensus code and the zkVM.

The consensus logic here is Core's own, compiled verbatim, which is the entire point. The *glue* is
ours: deserialisation, bounds handling, the canonical leaf construction, and the `core_*` exports that
pin every constant. A zkVM has no memory protection, so an out-of-bounds read is a silent wrong answer
rather than a crash.

Audit #5 read it in full and found one unguarded index (L-1, since fixed, no exposure). That is a good
sign, not a conclusion.

### 4. The coordinator under a HOSTILE-coordinator model

Currently reviewed under the stated model: `COORD_URL` is operator-chosen and trusted. That model is
honest today.

If untrusted coordinators ever become supported — which is what hazync#69 contemplates — the worker's
handling of coordinator responses needs a second pass: unbounded `get().read()` sizes, claim-response
handling, and anything else that treats the coordinator's answer as well-formed.

**Do not commission this until #69 has a design**, or the review will be of a model we are not building.

### 5. ghostd integration, when it lands

hazync#31, #42, #46 — the consumer side of the trust boundary, and consensus-critical on the node side.
Nothing to review yet.

## What a reviewer should be told

- **The guest is the authority.** `accumulator/`'s `Stump` is the readable spec; the guest's
  `utreexo.rs` is what is proven, and it carries hardening the host oracle does not.
- **`reproduce/METHOD_ID` is the source of truth for the id**, and the container is the only place it is
  canonically derived. A local build legitimately differs (it embeds `CARGO_HOME`).
- **`SECURITY.md` is a real changelog of findings**, including our own mistakes and retractions. Reading
  it is faster than rediscovering them.
- **Five internal audits are in `docs/AUDIT_*.md` and SECURITY.md.** Re-treading them is allowed but is
  not where the value is.

## Not on this list, deliberately

**Core's consensus code itself.** It is compiled verbatim from a pinned tag and has had far more review
than we could commission. Auditing it here would be auditing Bitcoin Core, and the pinning is what needs
checking (item 1), not the code.

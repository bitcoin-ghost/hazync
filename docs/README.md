# Hazync documentation

Bitcoin Core's own consensus code, executed inside a RISC0 zkVM, so that a block's validity can be
**proven once and verified by anyone** without re-executing it.

⏰ **Current as of 2026-09-14 (v0.21.4); where things stand is [`STATUS.md`](STATUS.md).** Anything not listed here is in [`history/`](history/README.md),
the development record, which **must not be quoted for numbers** without its corrections. Every release is
in [`../CHANGELOG.md`](../CHANGELOG.md); the GitHub releases are canonical.

## Use it

| | |
|---|---|
| [`STATUS.md`](STATUS.md) | where it stands: release, guest id, board, open issues, decisions (dated) |
| [`EXPLAINER.md`](EXPLAINER.md) | what this is, in plain terms |
| [`GOALS.md`](GOALS.md) | what it is for, and what it is not |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | contributing GPU time to the board |
| [`SPONSORSHIP.md`](SPONSORSHIP.md) | sponsoring blocks |
| [`../CHANGELOG.md`](../CHANGELOG.md) | every release, newest first |
| [`GLOSSARY.md`](GLOSSARY.md) | the terms this repository uses, each tied to the code |

## Run it

| | |
|---|---|
| [`PROVING.md`](PROVING.md) | proving, end to end |
| [`FLEET_OPERATIONS.md`](FLEET_OPERATIONS.md) | one block across many GPUs: `seg-serve` / `seg-connect`, knobs, failure handling |
| [`BUILDS.md`](BUILDS.md) | the CORE (shipped) and GHOST channels: patches, flags, measured card counts |
| [`TOPOLOGY_AND_SETTINGS.md`](TOPOLOGY_AND_SETTINGS.md) | fleet shape, card and per-box settings |
| [`RUN_YOUR_OWN_COORDINATOR.md`](RUN_YOUR_OWN_COORDINATOR.md) | operating a board coordinator |
| [`COORDINATOR_REFERENCE.md`](COORDINATOR_REFERENCE.md) | every coordinator route and setting, and the worker CLI, generated from the code |
| [`SPONSOR_BOT.md`](SPONSOR_BOT.md) | the sponsor proving bot |
| [`RELEASE_PROCESS.md`](RELEASE_PROCESS.md) | cutting a release, deploying the browser verifier, and re-baselining the guest |

## Review it

| | |
|---|---|
| [`SPEC.md`](SPEC.md) | the specification |
| [`SOUNDNESS.md`](SOUNDNESS.md) | what a proof does and does not establish |
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | per component: what it is trusted for, what an attacker can do, and the open items |
| [`../SECURITY.md`](../SECURITY.md) | security policy and the audit rounds |
| [`EXTERNAL_REVIEW.md`](EXTERNAL_REVIEW.md) | what still needs outside eyes |
| [`FUZZING.md`](FUZZING.md) | fuzzing posture: independent oracle, positive control, honest scope |
| [`../prover/evidence/README.md`](../prover/evidence/README.md) | index of the committed evidence files, with provenance and known issues |
| [`METHOD_ID_DURABILITY.md`](METHOD_ID_DURABILITY.md) | one `METHOD_ID`, and what we would do the day we are forced off it |
| [`PROOF_DURABILITY.md`](PROOF_DURABILITY.md) | what keeps a receipt verifiable |
| [`FIELD_BIGINT2_BACKEND.md`](FIELD_BIGINT2_BACKEND.md) | the coprocessor field backend for libsecp (`patches/0012`) |
| [`LIFTX_HINT.md`](LIFTX_HINT.md) | recovering a pubkey's Y from a verified hint (`patches/0013`) |

## History

| | |
|---|---|
| [`history/README.md`](history/README.md) | the development record, with its known-stale figures |
| [`history/releases/`](history/releases/) | copies of release bodies (v0.20.0 draft, v0.21.0–v0.21.4) |
| [`history/SECURITY_AUDIT_LOG.md`](history/SECURITY_AUDIT_LOG.md) | the round-by-round security review record, indexed from `SECURITY.md` |
| [`history/TOPOLOGY_AND_SETTINGS_2026-09-05.md`](history/TOPOLOGY_AND_SETTINGS_2026-09-05.md) | the pre-CORE topology page, as it stood before its rewrite |

## ⛔ How to read a number in this repository

Every performance figure should say **what was measured, on what, and with what still unmeasured.**
This project has repeatedly been wrong by believing a projection, and the corrections are recorded
rather than quietly edited out:

- a bigint2 projection of **7.53x** on the tip block measured **4.48x**
  (`history/TIP_BLOCK_BIGINT2_2026-08-28.md`)
- a field-backend projection of **3.67x** first measured **2.381x**: limb copies at the Rust/C boundary
  cost 2,191 M cycles, 38% of the block, and removing them gave **3.836x** (`FIELD_BIGINT2_BACKEND.md`)
- `fe_sqrt` sized at **9.83%** from a flat profile was **6.17%** cumulative once the field ops
  beneath it were accelerated (`patches/0013` header)
- a packer whose model priced every chunk identically had an actual straggler of **1.563x** on real
  cycles, worse than not packing (`prover/host/src/main.rs`, packer notes)
- a CORE card count of **10** L40S, computed from measurements on two cards, became **~13** once an
  eight-card fleet was run (`BUILDS.md` §1, `history/BENCH_8xL40S_2026-09-08.md`)

**If a figure does not say how it was obtained, treat it as a projection.**

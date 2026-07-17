# Proving Bitcoin's real consensus code in a zkVM

*A technical writeup of Hazync. For the plain-English version, see [`EXPLAINER.md`](EXPLAINER.md); for
the exact security claim, [`SOUNDNESS.md`](SOUNDNESS.md); for the self-review and open items,
[`SECURITY.md`](SECURITY.md).*

## The claim

A verified Hazync **chain-tip proof** attests that *every block from a trusted anchor to the tip is
valid under Bitcoin Core consensus, the UTXO set is exactly the committed accumulator root, and the
cumulative proof-of-work is as committed* — checkable by verifying one succinct proof, without
re-executing history or trusting peers.

The distinguishing property is what does the validating: **Bitcoin Core's own unmodified consensus
code.** `interpreter.cpp` (script evaluation), `SignatureHash`, `CheckTransaction`, `arith_uint256`,
the difficulty and merkle code, and **`libsecp256k1`** are compiled to `riscv32im` and proven *as-is*
inside a RISC0 zkVM. There is no consensus reimplementation.

## Why that property matters

Every prior validity-proof effort reimplements Bitcoin's consensus rules in a proof-friendly language
(a circuit DSL, Cairo, a custom VM). That inherits a permanent question: *does your reimplementation
match Bitcoin Core in every edge case, forever?* Bitcoin's real rulebook is full of load-bearing
quirks — `OP_CHECKMULTISIG`'s off-by-one, low-S enforcement, the sighash single-bug, the exact
weight/sigop accounting, `MINIMALIF`, taproot annex handling — and a proof of a faithful-looking
reimplementation that diverges in one of them proves the wrong thing. This is the wall ZeroSync and
others hit. Hazync doesn't have the question, because there is no reimplementation: it runs Core.

The cost of that choice is that Core's C++ has to run in the zkVM. It does, with exactly **two
portability shims** and libc/unwinder glue — no consensus-logic changes:
- `serialize.h`: a 32-bit `int` overload so ILP32 `riscv32` serialises identically to LP64 (patch 0001).
- SHA-256 routed to RISC0's accelerator, byte-identical to Core's (patch 0002).

Both are auditable and narrow, and neither changes consensus logic: ECDSA and Schnorr both go through
the compiled, unmodified `libsecp256k1`. There is a *third* patch, `patches/0003`, which substitutes
RustCrypto's `k256` for libsecp's ECDSA verify purely as a speed option. It is **not** applied in the
sound build (`provision-vps.sh` applies only 0001 + 0002) and is **not** part of the "runs Core" claim —
using it would reintroduce exactly the reimplementation-equivalence question this project avoids, which
is why it is opt-in and quarantined to `ACCELERATION.md`.

A gotcha worth noting for anyone reproducing: C++ static constructors
don't run in the bare-metal guest, so Core's global tagged-hash midstates (e.g. `TapSighash`) are
garbage until you call `__libc_init_array()` once at guest entry — without it, taproot sighashes are
silently wrong.

## What's covered — the consensus matrix

Enforced with real Core code, and demonstrated on real mainnet data (blocks 170; 741000 — a
segwit+taproot block with 670 inputs; the genesis→550 chain; and a real 90-day-CSV transaction):

- **Scripts, all types**, with per-height soft-fork flags (P2SH/DERSIG/CLTV/CSV/segwit/taproot) — real
  `VerifyScript`. **Signatures** (ECDSA + Schnorr) and **sighash** (legacy / segwit v0 / taproot) via
  real `libsecp256k1` and `SignatureHash`.
- **`CheckTransaction`** — structure, duplicate-input rejection, null-prevout rejection for
  non-coinbase (which also enforces "only the coinbase is a coinbase"), value ranges, coinbase
  scriptSig size.
- **No inflation** — Σinputs ≥ Σoutputs per tx; coinbase ≤ `subsidy(height) + fees` (exact halving).
- **Proof-of-work** (`CheckProofOfWork`, real `arith_uint256`) and the **difficulty retarget** formula.
- **Merkle root**, including the **CVE-2012-2459 mutation** check (duplicate-txid malleability).
- **Block weight** ≤ 4M and **sigop cost** ≤ 80k (legacy + P2SH + witness).
- **BIP141** witness commitment (recomputed in-guest from the real transactions, so a prover can't
  claim "no witness" to skip it).
- **BIP34** (height in coinbase), **BIP30** (duplicate-txid overwrite).
- **Coinbase maturity** (100 blocks); **absolute locktime** (BIP113, median-time-past); **BIP68**
  relative locktime, both height- and **time-based** (using the real `GetMedianTimePast(coinHeight−1)`).
- **`time-too-old`** — a block's timestamp must exceed the median-time-past of the previous 11 blocks.
- **UTXO transition** — every spent coin proven present in the accumulator and deleted, created coins
  inserted, result equals the committed next root; in-block spends and unspendable outputs handled.

## The accumulator — the one non-Core component

Bitcoin Core keeps the UTXO set in a database; a zkVM can't. Hazync uses a **Utreexo** hash-forest
accumulator: the guest holds only the roots (a `Stump`), verifies an inclusion proof for each spent
coin, deletes it, and inserts the block's new coins — a pure function
`(block, root_prev, inclusion_proofs) → root_next`, which is exactly what makes blocks compose under
recursion with no shared state. A bridge (any archive node) holds the full forest and emits the
inclusion proofs.

This is new code, not a Core reimplementation — it's a commitment layer *above* consensus, and its
entire security is SHA-256. It's exhaustively tested natively (every single-delete against a full-forest
oracle for all `n ≤ 40` and every index; double-spend rejection; thousands of interleaved ops). Each
coin's `(value, scriptPubKey, height, coinbase-flag, creation-MTP)` is committed into its leaf, so the
metadata that maturity and BIP68 depend on is unforgeable.

## Recursion and scale

Block proofs fold into a chain-tip proof two ways: a **sequential** increment (`chain_step`, RISC0
composition via `env::verify`), and a **parallel range-fold** — prove each block independently, then
merge adjacent ranges in a log-depth tree, checking `tip_hash`/`root`/`cumwork` boundaries at each
join. The range-fold is the backfill path: genesis→tip is embarrassingly parallel, so the one-time
historical proof distributes across many machines, and the tip is then kept current by a small cluster.
Within a block, inputs prove in parallel (segmentation) and aggregate. In the sound build only SHA-256
is routed to RISC0's accelerator (patch 0002, byte-identical); the EC-heavy `libsecp256k1` verification
runs *unaccelerated*, so the cycle counts above are for unaccelerated EC. Speeding it up — a bigint2
field backend, or the k256 substitution in `patches/0003` — is open work tracked in `ACCELERATION.md`
and is **not** used for any soundness claim.

## Trust model — what you actually assume

A verified chain-tip proof rests on exactly these, stated plainly:

1. **Real Bitcoin Core code** (the two shims above; no consensus-logic changes).
2. **RISC0 zkVM soundness** — the STARK/SNARK proving system (standard assumption).
3. **SHA-256** collision resistance (accumulator + merkle) and **secp256k1** (signatures).
4. **The anchor** — the chain starts from a trusted state. Genesis is unconditional; any later
   checkpoint is a documented trust input.

And, explicitly, what a zkVM proof **cannot** cover — these are trust boundaries, not oversights:

- **The 2-hour future-time limit.** Core rejects a block whose timestamp is too far ahead of
  *node-local adjusted wall-clock time*. That's not a function of the chain — it depends on a clock the
  zkVM doesn't have — so it is not a provable consensus rule and is intentionally omitted. (The
  `time-too-old` half, which *is* a function of the chain, is enforced.)
- **Data availability.** The proof attests validity *given* the block's transactions; those are bound
  to the header via the merkle root and to each txid, so the prover can't substitute different data,
  but the proof is not itself the data.
- **Recursion self-reference (audit S1).** The composition binds each step to the guest image id; the
  verifier checks the final receipt against the true `METHOD_ID`, and the nested id is committed and
  asserted equal at every level. Single-block proofs are unconditional here; the recursive case is
  hardened and its argument is written down in `SOUNDNESS.md §3`.

## Honest status

This is a **method that is built and demonstrated end-to-end on real mainnet data**, not a running
production network. Every consensus variable above is exercised on real blocks/transactions. What is
*not* yet done: the full genesis→tip run (the method is proven; the bottleneck is GPU-hours, which the
parallel backfill is designed to absorb — see [`SCALING.md`](SCALING.md)), and **independent external
review** (there has been none — the self-review and its findings are in `SECURITY.md`, and they are the
starting bounty list). The accumulator is the one component most deserving an outside audit.

## Reproduce it (~25 minutes on a GPU box)

```bash
GPU=1 REPO_DIR=$PWD ./provision-vps.sh                 # RISC0 + CUDA 12.6 + Core v28 + build
cd prover && cargo build --release --features cuda
for h in $(seq 1 550); do python3 fetch_block.py $h /w/block_$h.json; done
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=550 ./target/release/host check-ibd   # execute, seconds/block
HAZYNC_WITNESS_DIR=/w HAZYNC_FROM=1 HAZYNC_TO=170 HAZYNC_TIP=3 ./target/release/host prove-ibd   # real STARK
```

`check-full` on `prover/block_741000.json` proves the full segwit+taproot block; `test_bip68_real.sh`
runs the real time-locked transaction; `test_bip68_locks.sh` and `make_negative_tests.py` are the
negative tests.

## Try to break it

The valuable contribution is a case where a Hazync proof says *valid* for something Bitcoin Core
rejects. `SECURITY.md` lists exactly where we think the soft spots are — the accumulator, the recursion
binding, the metadata plumbing. That's the map. Reproduce it, review it, and try to make it lie.

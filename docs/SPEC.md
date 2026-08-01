# Hazync — specification

**Status: draft, and normative where it is precise.** This document defines the artifacts and the
verification procedure. It is the document to hand a reviewer who asks "what exactly is being claimed,
and how would I check it myself".

It deliberately does not argue. The soundness argument is [`SOUNDNESS.md`](SOUNDNESS.md); the design
rationale and history are [`HAZYNC_ARCHITECTURE.md`](HAZYNC_ARCHITECTURE.md); what is measured and what
is still open is [`GOALS.md`](GOALS.md). Where this document and those disagree about a *format*, this
one is wrong and should be fixed — the code is the authority, and `reproduce/METHOD_ID` is the
authority on which code.

Key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

---

## 1. The claim

A Hazync range proof for blocks `[lo..hi]` attests:

> Every block from `lo` to `hi` is valid under Bitcoin Core's consensus rules, as implemented by
> Bitcoin Core's own unmodified consensus source compiled into the proof circuit — including every
> script, signature and sighash — and the UTXO set transitions from the committed in-boundary to the
> committed out-boundary exactly.

A **genesis-anchored** proof is one where `lo = 1` and the in-boundary is genesis itself (§9). Only a
genesis-anchored proof attests that a chain is valid *from the start*. A mid-chain segment proof is
sound but says nothing about the blocks beneath it.

**What this is not.** It is not a claim of zero trust. Bitcoin Core already skips script verification
for most of the chain via `assumevalid`, on the authority of a hash its developers chose. Hazync
replaces that anchor with a proven one. The trust assumption is strictly *smaller* than the status
quo, not absent — see §12.

---

## 2. Notation

- `SHA256(x)` — one SHA-256 compression over `x`.
- `a || b` — byte concatenation.
- `LE32(n)`, `LE64(n)` — little-endian encodings of width 4 and 8.
- Block hashes and txids are handled in **internal** byte order unless a field is explicitly named
  *display* order. Display order is the byte-reverse, as printed by `getblockhash`.

---

## 3. Hash constructions

Two, and they MUST be domain-separated:

```
TAG_LEAF = 0x00
TAG_NODE = 0x01

leaf_hash(preimage)   = SHA256( TAG_LEAF || preimage )
node_hash(left,right) = SHA256( TAG_NODE || left || right )
```

The tags are not optional and are not decoration. A UTXO leaf preimage (§4) is `57 + |scriptPubKey|`
bytes, so a 7-byte `scriptPubKey` yields a 64-byte preimage — the same width `node_hash` consumes.
Without tags the two constructions are the same function over the same input length, and a value can
be valid as both. The residual barrier would be that a leaf preimage opens with a txid; a txid is the
hash of a transaction an attacker composes, so that is a grinding cost, not a separation.

Implementations MUST apply the tag as the **first** byte hashed. A tag appended after the operands
separates nothing.

These constructions exist in three places that MUST agree byte for byte — the host oracle
(`accumulator/src/lib.rs`), the guest accumulator (`prover/methods/guest/src/utreexo.rs`) and the guest
leaf builders (`prover/methods/guest/verify_input.cpp`). `scripts/check-utreexo.sh` enforces this.

---

## 4. UTXO leaf commitment

A UTXO is committed as:

```
leaf = leaf_hash(
    txid                    32 bytes, internal order
 || LE32(vout)
 || LE64(value_sat)
 || LE32(|scriptPubKey|)
 || scriptPubKey            variable
 || LE32(coin_height)
 || is_coinbase             1 byte, 0x00 or 0x01
 || LE32(coin_mtp)
)
```

Notes, each of which is load-bearing:

- **`scriptPubKey` is length-prefixed.** It is the only variable-length field; the prefix keeps the
  preimage injective if a future revision adds another.
- **`coin_height` and `is_coinbase`** are committed so coinbase maturity and BIP68 height-relative
  locks cannot be lied about.
- **`coin_mtp` is `MTP(coin_height - 1)`** — the median-time-past of the block *before* the one that
  created the coin, matching Core's `GetMedianTimePast`. It is committed so BIP68 *time*-relative locks
  are checked against the same value Core uses. This field is **not** part of Core's UTXO
  representation; it is recoverable from the header chain alone, which is what makes a Core-format
  UTXO snapshot sufficient to reconstruct the accumulator (§11.2).
- Outputs for which `IsUnspendable()` holds are **not** committed — they never enter the UTXO set.

---

## 5. Accumulator

A Utreexo hash forest. `n` leaves form a set of perfect binary Merkle trees, one per set bit of `n`,
laid out left to right in descending height. Because a leaf count has distinct bits, at most one tree
exists per height, so a proof's path length uniquely identifies the root it must match.

**State.** A verifier holds only `roots` (indexed by tree height, `None` where absent) and `num_leaves`.
This is the `Stump`. The full forest is a host-side oracle and never enters the circuit.

**Append.** Adding a leaf is a binary-counter carry: while a root exists at height `h`, replace it with
`node_hash(existing, carried)` and move up.

**Delete** is swap-and-shrink: the coin at global position `i` is replaced by the current rightmost
leaf, and the rightmost is dropped. A deletion MUST be accompanied by two inclusion proofs against the
*current* roots — one for position `i` and one for the rightmost leaf.

**Deletion soundness requirements.** The circuit MUST reject unless all hold:

1. `proof_i` verifies against the root at its own path length.
2. `proof_last` verifies likewise.
3. The tree containing `i` has height equal to `|proof_i.siblings|`.
4. `proof_i.position == i - tree_offset(i)` — the **local** index within the containing tree, not the
   global one.
5. `proof_last` is the true rightmost leaf, i.e. its position is `2^h - 1` within its tree.

(4) and (5) are the SEC-2 hardening. Without them a prover can present an honest inclusion proof for
one coin while claiming a different position, deleting a coin it does not control.

---

## 6. Block witness

The host supplies, per block: the 80-byte header, height, the coinbase transaction, the raw
transactions and their prevouts, per-input records (`tx_idx`, `input_idx`, `global_pos`, `coin_height`,
`coin_is_coinbase`, `coin_mtp`, `proof_i`, `proof_last`), and the in/out accumulator boundaries.

**The witness is untrusted.** Every field the circuit can derive, it MUST derive rather than read.
Specifically the circuit recomputes txids, wtxids, `has_witness`, the created-output leaf set, and the
block's script flags from the raw transaction bytes. Host-supplied copies of these are ignored where
present; a field the circuit merely *reads* is a field a malicious prover controls.

---

## 7. What the circuit asserts per block

A block is accepted only if **all** hold: `all_ok` (every input verified by Core's real `VerifyScript`
with the correct flags, and every accumulator deletion sound), `root_matches`, `pow_ok`, `merkle_ok`,
`subsidy_ok`, `weight_ok`, `sigops_ok`, `witness_ok` (BIP141 commitment), `bip34_ok`, `bip30_ok`.

Chain-linkage additionally requires: `prevhash_ok`, `carry_ok` (the in-boundary equals the previous
out-boundary), `retarget_ok` (nBits equals Core's `CalculateNextWorkRequired`) and `time_ok`.

Consensus constants are read from Core's compiled `CChainParams::Main()` rather than transcribed;
remaining Rust-side literals are pinned to Core's values at runtime and a mismatch aborts.

---

## 8. Journal (`RangeState`)

Committed publicly by every range proof. Fields, in order — the journal decodes **positionally**, so
order and type are part of the format:

```
kind          u32                  KIND_RANGE = 0xC4A10006
lo            u32                  first block in the range
hi            u32                  last block, inclusive
in_tip_hash   [u8;32]              internal order
in_roots      Vec<Option<[u8;32]>> indexed by tree height
in_leaves     u64
in_nbits      u32
in_time       u32
in_epoch_start u32
in_recent     Vec<u32>             timestamps for median-time-past
out_tip_hash  [u8;32]              internal order
out_roots     Vec<Option<[u8;32]>>
out_leaves    u64
out_nbits     u32
out_time      u32
out_epoch_start u32
out_recent    Vec<u32>
range_work    [u8;32]              cumulative work across the range, big-endian
self_id       [u32;8]              the guest image id, committed by the guest itself
```

Every field is listed rather than abbreviated to "the out-boundary, same shape". The journal decodes
**positionally**: a field reordered or retyped in a consumer does not fail, it silently misreads a
valid proof and reports confident nonsense. `scripts/check-rangestate.sh` enforces agreement between
the implementations, and `scripts/check-spec.sh` enforces that this list matches them.

`self_id` is the recursion pin (§10). Trailing `None` entries in a roots vector are padding and MUST be
normalised away before comparison.

---

## 9. Genesis anchoring

A proof is genesis-anchored iff **all** hold:

- `lo == 1`
- `in_tip_hash` equals the genesis block hash
- `in_leaves == 0` and `in_roots` normalises to empty
- `in_nbits == 0x1d00ffff`
- `in_epoch_start == 1231006505` and `in_time == 1231006505`
- `in_recent == [1231006505]`

All six MUST be checked. Checking only `lo == 1` admits a proof that claims to start at block 1 from a
fabricated in-boundary.

---

## 10. Composition

Two adjacent range proofs `[a..b]` and `[b+1..c]` compose into `[a..c]` when the left's out-boundary
equals the right's in-boundary in full — tip hash, roots, leaf count, nBits, epoch start and recent
times. `range_work` sums. Composition is a tree, not a chain: any adjacent pair may be folded in any
order, so only *anchoring* is sequential.

Each proof commits `self_id`, the image id of the guest that produced it, and the circuit verifies its
inputs against that same id. A proof therefore cannot absorb an assumption produced by a different
guest, and the recursion cannot be re-pointed at a weaker circuit.

### 10.1 The spine

Because composition is a tree but anchoring is sequential, exactly one artifact at a time is the
**genesis-anchored head** — the *spine*. It is the range proof `[1..N]` with the largest `N`, and it
is what "everything from genesis to N is valid" means concretely.

The spine **advances by absorption**, never by re-folding:

```
spine [1..N]  +  chunk [N+1..M]   ->   spine [1..M]
```

Absorption is ordinary composition (§10) in which the left operand is genesis-anchored, so the result
is too. It is one fold per absorbed chunk regardless of the chunk's width, which is why the chunk
should be as wide as the tree can make it: the tree does the parallel work, the spine takes the
result.

Three properties follow, and they are the reason the spine is defined this way rather than as a
periodic re-fold of everything proven:

1. **It is always shippable.** After every absorption there exists a complete genesis-anchored proof
   of the chain up to the head. There is no state in which the artifact is half-built.
2. **Advancing it is the only sequential step.** Proving and folding are unbounded in parallelism;
   only absorption must happen in order, because only the leftmost range can satisfy `lo == 1`.
3. **Whoever advances it cannot corrupt it.** Every absorption is a fold that any verifier re-checks
   against the canonical `METHOD_ID` and the genesis pin (§9). A wrong absorption does not verify. If
   per-block receipts are retained, the spine is also *rebuildable from scratch by anyone* without
   re-proving a single block — so a stalled or absent spine costs time, never soundness.

A verifier is not required to know that a proof is "the spine": the spine is an operational role, and
what it is checked against is exactly §11.1. Nothing in the format distinguishes it from any other
genesis-anchored range proof.

---

## 11. Verification

### 11.1 Verifying a proof

A verifier MUST, in order:

1. Verify the receipt against the canonical guest image id (`reproduce/METHOD_ID`).
2. Check `journal.self_id` equals that image id.
3. Check `journal.kind == KIND_RANGE`.
4. Check genesis anchoring per §9.

A verifier MUST NOT report success for a proof that verifies cryptographically but fails (4). It
SHOULD distinguish that outcome from an invalid proof, because segment proofs are cryptographically
perfect and common; the reference implementation exits `0`, `1` and `2` respectively.

Nothing else is required — no node, no peers, no chain data. The reference verifier is 1.7 MB.

### 11.2 Adopting state from a proof

A node MAY adopt the out-boundary of a verified genesis-anchored proof at height `hi` and resume
validation at `hi + 1`. To bind a Core-format UTXO snapshot to a proof, the node MUST reconstruct each
leaf per §4 — deriving `coin_mtp` as `MTP(coin_height - 1)` from its own header chain, since Core's
snapshot does not carry it — rebuild the forest, and require that the resulting roots and leaf count
equal the proof's `out_roots` and `out_leaves`. Only then is the snapshot the UTXO set the proof
attests to.

---

## 12. Trust base

What a verifier is trusting, exhaustively:

1. **SHA-256 and the RISC0 proof system** (STARK, and Groth16 for the wrapped form), including its
   trusted setup for the wrap.
2. **That the canonical image id corresponds to the published source.** This is checkable: the guest
   builds bit-reproducibly at fixed paths in a container, and CI asserts the id.
3. **The accumulator implementation** — the one component that is not Bitcoin Core's own code, and
   correspondingly the piece most worth auditing.

Explicitly *not* trusted: the prover, the witness, the bridge, the coordinator, and any host-supplied
value the circuit can recompute.

---

## 13. Versioning

The guest image id is the format version. Any change to guest source produces a new id, and proofs are
not interchangeable across ids — a verifier pinned to one id will reject proofs made under another,
correctly. `reproduce/METHOD_ID` is the source of truth and records every supersession with its reason.

A changed id proves that *something* in the guest changed. It does **not** prove that everything
rebuilt: a stale object file can leave part of the circuit older than its declared id. Builds MUST run
the consensus regression, which is what detects that.

---

## 14. Known limitations

- **Mainnet only.** The guest compiles `CChainParams::Main()`. A testnet or regtest proof requires a
  different guest and therefore a different image id.
- **No external audit yet.** Nine rounds of adversarial self-audit are recorded in `SECURITY.md`;
  self-audit is not review.
- **The wrapped proof is 2,033 bytes**, not the ~200–300 B quoted in some older docs.

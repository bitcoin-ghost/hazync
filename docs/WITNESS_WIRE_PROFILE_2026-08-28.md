# The witness, measured on the wire

Block 962,000, 8,006 inputs, 6,303 de-duplicated txs. Every figure produced by running the same
`risc0_zkvm::serde::to_vec` the executor uses, per sub-structure, so all of them share one unit.
Reproduce with `HAZYNC_WITNESS_SIZES=1 HAZYNC_BLOCK=block_962000.json host check-full`.

This replaces the residual arithmetic in `TEN_MINUTE_BLOCK.md` §7.8, which that section itself asked
for before anyone wrote an encoder.

## The profile

```
WITNESS block 962000 inputs=8006 txs(deduped)=6303 total=7256592B (wire)
  field            wire B      %     source B    amplification
  inputs           4610004   63.5%          --            --
    proofs         4353808   60.0%     1168512          3.73x
    scalars         256196    3.5%          --            --
  txs              1568620   21.6%     1529381          1.03x
  tx_prevouts       266132    3.7%      238272          1.12x
  txids             806916   11.1%      201728          4.00x
  smt                  272    0.0%
  stumps              2044    0.0%
  header+cbtx         2468    0.0%
  bip30                  4    0.0%
  per input: 575 wire B, of which 543 B is the two proofs
```

## What it confirms

- **`inputs` is 63.5% of the witness**, against §7.8's "~65%". Confirmed.
- **`txs` is 21.6% at 1.03x amplification.** `PackedBytes` is doing its job — transaction bytes are
  NOT 4x-bloated, exactly as the aborted encoder attempt discovered. #136's fix is genuinely on the
  witness type, and the 2.05x estimate that rested on the opposite premise is dead.
- **The two `WireProof`s are 60.0% of the entire witness**, at **3.73x** amplification. That is the
  lever, and it is where §7.8 said it was.
- **575 wire bytes per input**, against §7.8's ~595 B residual. The residual was good to 3.4%.

## ⛔ What it finds that nobody had priced: `txids`

`txids: Vec<[u8; 32]>` costs **806,916 wire bytes — 11.1% of the witness — at a full 4.00x**, the
worst amplification of any field. It is not mentioned in the plan, in §7.8, or in the ranked levers.

It is the same defect as the proofs: a `[u8; 32]` goes through risc0 serde's default path at one
32-bit word per byte. `PackedBytes` already exists and already solves it.

## What packing would buy — measured bytes, inferred cycles

| packed | witness B | vs now |
|---|---|---|
| nothing (today) | 7,256,592 | — |
| proofs only | 4,071,296 | **1.78x** |
| **proofs + `txids`** | **3,466,108** | **2.09x** |

⚠ **Bytes are measured; cycles are not.** Carrying these through the 78%-of-validation-is-
deserialisation figure gives roughly **1.52x** on block validation for proofs alone and **1.69x** with
`txids` — against the 1.54x currently in the plan, which assumed proofs only. So the plan's number was
about right for the lever it priced, and there is a second lever next to it worth another ~0.17x.

⛔ **Do not promote 1.69x to a measurement.** It assumes deserialisation cost scales with wire bytes at
a constant rate. Packing changes words-per-byte by 4x for the affected structures, which is the right
shape, but the constant has not been re-measured after packing. **Measure the cycles, then quote them.**

## Before writing the encoder

- ⛔ **`txids` STAYS on the wire. Pack it; do not try to drop it.** An earlier draft of this document
  suggested pricing its removal. That is the wrong question, and it is the same shape of mistake as
  the `tx_prevouts` one recorded in PR #200: *"the aggregate recomputes every leaf despite the chunks
  supplying `chunk_leaves` — that recomputation IS the anti-substitution check. Sending less is not
  available."*

  `w.txids` is doing two jobs at once, and the guest code is explicit about both:

  ```rust
  if w.txids.is_empty() || cb_txid != w.txids[0]     { all_ok = false; }  // coinbase bound
  if w.txs.len() + 1 != w.txids.len()                { all_ok = false; }  // no add/drop of a tx
  let t = gather(raw_tx, 0, &mut output_leaves);
  if tx_pos >= w.txids.len() || t != w.txids[tx_pos] { all_ok = false; }  // per-tx binding
  if tx_pos != w.txids.len()                         { all_ok = false; }  // count matches
  ...
  let flat: Vec<u8> = w.txids.iter().flatten().copied().collect();
  merkle_root(flat.as_ptr(), w.txids.len() as u32, ...);  // checked against header[36..68]
  ```

  It is the **merkle preimage** checked against the header, and simultaneously the **binding target**
  every independently computed txid is held to — together they are what makes "the raw bytes ARE the
  block's txs" true, which the output-leaf reconstruction then rests on.

  ✅ **None of that obstructs the win.** Packing changes the ENCODING, not whether the field is sent:
  4 wire bytes per source byte down to 1. The guest receives identical values, performs identical
  bindings, and hands an identical preimage to `merkle_root`. The 2.09x above is priced as PACKED
  (source bytes retained at 201,728), so it stands as written.
- The proofs are `[u8; 32]` leaf + `Vec<[u8; 32]>` siblings inside `WireProof`. Packing them changes
  the wire format on BOTH sides, so it moves `METHOD_ID` and rides the re-baseline batch.


---

# ✅ MEASURED AFTER THE ENCODER (same block, same command)

```
WITNESS block 962000 inputs=8006 txs(deduped)=6303 total=3594208B (wire)
  field            wire B      %     source B    amplification
  inputs           1552804   43.2%          --            --
    proofs         1296608   36.1%     1168512          1.11x
    scalars         256196    7.1%          --            --
  txs              1568620   43.6%     1529381          1.03x
  tx_prevouts       266132    7.4%      238272          1.12x
  txids             201732    5.6%      201728          1.00x
  per input: 193 wire B, of which 161 B is the two proofs
```

| | before | after | |
|---|---|---|---|
| **witness total** | 7,256,592 B | **3,594,208 B** | **2.019x** |
| proofs | 4,353,808 (3.73x) | 1,296,608 (**1.11x**) | |
| `txids` | 806,916 (4.00x) | 201,732 (**1.00x**) | |
| per input | 575 B | **193 B** | |
| `txs` (control) | 1,568,620 (1.03x) | 1,568,620 (1.03x) | unchanged, as it must be |

✅ **2.019x measured against 2.09x predicted.** The shortfall is length prefixes: every `PackedHash`
and `PackedHashes` carries a 4-byte count, and there are 16,012 proofs, so ~128 KB of prefix that the
idealised projection ignored. `txids` came out at **1.00x** -- as close to free as the encoding gets.

✅ **`txs` is byte-for-byte unchanged**, which is the control: it was already packed, so a change
there would have meant the edit reached something it should not have.

⇒ The reporter's own projection line now reads **1.037x**, i.e. the lever is spent. What remains is
prefix overhead, not amplification.

## Inferred, NOT measured

Carrying 2.019x through the 78%-of-validation-is-deserialisation figure gives roughly **1.65x** on
block validation (`0.78 x 0.4953 + 0.22 = 0.606`), against the 1.69x the plan projected from the
2.09x estimate.

⛔ **Still inferred.** It assumes deserialisation cost scales with wire bytes at a constant rate.
And ⛔ **this is a SIZE result, not a CORRECTNESS result** -- it proves the encoder shrinks the
witness, not that host and guest agree field-for-field. That needs the aggregate path (mode 5) and
therefore a GPU. See `STACK_INTEGRATION_PLAN.md`.

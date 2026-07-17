# Backfill hardening — design (2026-07-15)

Making the engine correct for a full genesis→tip run (beyond the early blocks tested). Three items;
H1 is the one that BREAKS a run today, and closing it properly also closes a latent soundness gap.

## H1 — in-block spends + output recomputation (correctness + soundness)  ★ core

**The gap.** A block may contain a tx that spends an output created by an *earlier* tx in the **same
block** (chained / CPFP). Today `build_block_carried` deletes all external inputs then adds all outputs,
so an in-block spend panics on the host (the parent output isn't in the accumulator yet) and would fail
the guest's `stump.delete`.

**The latent soundness gap (found while scoping H1).** The guest adds the host-supplied
`w.new_outputs` **without recomputing them from the block's transactions** (`validate_block`, the
`for out_leaf in &w.new_outputs { stump.add }` loop). A malicious prover could therefore add fabricated
output leaves (fake coins, spendable later) or omit real ones. The honest host always supplies correct
outputs, so the demos are unaffected — but for "undeniable" the guest must derive the output set itself.

**The fix (one change closes both).** The guest recomputes every output leaf from the block's txs and
handles in-block coins by **ephemeral cancellation** (a coin created and spent in the same block never
enters the accumulator):

- Guest gathers all this-block txs: `coinbase_tx` + `{inp.raw_tx : inp.tx_first == 1}` (dedup).
- New C helper `tx_out_leaves(raw_tx, height, is_coinbase, block_time, out[])`: parse tx, for each
  vout emit `coin_leaf(txid,vout,value,spk,height,is_coinbase,block_time)`, **skipping provably-
  unspendable outputs** (Core `CTxOut::scriptPubKey.IsUnspendable()` — OP_RETURN or > MAX_SCRIPT_SIZE).
  This is also H3.
- Build the this-block output set keyed by `(txid, vout)` → leaf.
- Partition inputs: an input is **in-block** iff its `prevout.txid ∈ this-block txids`; else **external**.
  - external: verify inclusion proof + `stump.delete` (as today).
  - in-block: verify the referenced `(txid,vout)` is a real this-block output whose recomputed leaf
    matches; mark that output "spent-in-block". NO stump op. Script still runs (VerifyScript is
    independent of the accumulator).
- Add to the stump exactly the **surviving** outputs (this-block outputs not spent-in-block), recomputed
  — replacing the trusted `w.new_outputs`.

**Wire change.** `BlockInput` gains `in_block: u32`. For in-block inputs `global_pos`/`proof_i`/
`proof_last` are unused; `prevouts` still carries the parent output's (value, scriptPubKey) so the
script check runs. Host `build_block_carried` builds the partition + surviving-output set to match.

**Why sound.** The guest independently derives txids (bound by the merkle check), the output leaves
(from the real tx bytes), and the in-block/external partition. A prover cannot mark an external spend
in-block (the referenced txid wouldn't be in this block), cannot fake an output (leaves come from real
tx bytes), and cannot omit a surviving output (the guest adds all of them). Ephemeral cancellation gives
the identical net root as add-then-delete.

## H2 — BIP30 duplicate-coinbase exception blocks (91842, 91880)

Pre-BIP34, blocks 91842/91880 carried coinbases with the same txid as 91812/91722. Our leaves commit
the creation **height**, so a "duplicate" coinbase produces a DISTINCT leaf (different height) — no
accumulator collision; the superseded coin becomes an unspent bloat leaf, sound. Post-BIP34 (227931)
duplicate txids are impossible (height in coinbase scriptSig ⇒ unique coinbase txid), which the guest
already enforces (`bip34_ok`). So H2 needs no code change for correctness; the two blocks validate.

**VERIFIED (2026-07-15).** Blocks 91812 and 91842 do carry the identical coinbase txid
`d5d27987d2a3dfc724e359870c6644b40e497bdc0589a033220fe15429d88599`. Computing our coin leaf for their
(identical) coinbase output at each height gives DIFFERENT leaves (`9f29c35f…` vs `26ad4502…`) — the
height commitment disambiguates, so no accumulator collision even if both coexist. `check-full` on both
blocks: VALID, tips byte-match mainnet. (This is the standalone validation of the mechanism + each
block's validity; a live genesis→91842 fold with both leaves present is unrun but moot given the leaves
provably differ.)

## H3 — provably-unspendable outputs

Core excludes `IsUnspendable()` outputs (OP_RETURN etc.) from the UTXO set. Folded into H1's
`tx_out_leaves` (skip them). Makes the accumulator equal Core's UTXO set exactly, so the S3 claim
("bound to the real UTXO set") is precise, not a superset.

## Implementation status — WRITTEN, staged for box validation (2026-07-15)
All three items implemented; **not yet compiled/run** (the guest change invalidates the cached ELF and
needs a box). Files:
- `prover/methods/guest/verify_input.cpp` — new `tx_out_leaves` (recompute a tx's spendable output
  leaves; skips `IsUnspendable`).
- `prover/methods/guest/src/main.rs` — `validate_block` now recomputes the output set from the real tx
  bytes, **binds each tx's computed txid to the merkle-committed `w.txids`**, derives in-block spends by
  leaf-membership (no wire change), cancels them (ephemeral), and adds the surviving outputs — replacing
  the trusted `w.new_outputs`. (`extern tx_out_leaves`, `BTreeSet` import.)
- `prover/host/src/main.rs` — `out_spendable` helper; `build_block_carried` handles in-block inputs
  (dummy proof, no forest touch) + skips unspendable + excludes in-block-spent outputs; `build_block`
  and `build_full` skip unspendable too (so their `root_next` still matches the recomputing guest).

**Soundness note:** the guest no longer trusts `new_outputs`; it derives outputs from tx bytes bound to
the merkle root, so a prover cannot fake/omit coins. In-block detection is leaf-membership, which is
self-authenticating (a mislabelled spend fails either the output-set match or the inclusion proof).

## Test plan (extensive test, needs a GPU box) — validates the above
1. Build the guest hardening on the box (guest rebuild ~min).
2. Re-run the KNOWN-GOOD vectors first (regression): `check-ibd` genesis→199, block 170/173/176 tips,
   `prove-full` block 741000 — all must still verify with identical hashes (proves the recompute +
   unspendable change didn't regress).
3. `check-ibd` (execute) over a range with real **in-block spends** and block **91842** (BIP30 dup
   coinbase) — confirm they validate and UTXO-leaf/tip match mainnet.
4. `prove-ibd` a substantial contiguous range; `rangecluster.sh` across both GPUs.
5. Any guest bug surfaces in step 2/3 in seconds (execute mode) — fix + rebuild in minutes.

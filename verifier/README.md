# `hazync-verify` — the whole trust check, in 1.6 MB

Verifies a genesis-anchored Hazync SNARK range proof. No node, no peers, no chain data, no proving.
Just the file.

```sh
cargo build --release --manifest-path verifier/Cargo.toml
./verifier/target/release/hazync-verify proof.snark
```

```
>>> SNARK RANGE PROOF [1..8] VERIFIED — genesis-anchored, 2353 bytes.
  out_tip_hash c60ddef1b7618ca2348a46e868afc26e3efc68226c78aa47f8488c4000000000  range_work 34360262664  total_cum_work 38655295497  UTXO leaves 8
  guest image id 3867611d99044e24dd5fe8753e6158f1d7f7d86c24bacc4ce968c621faed4b15
```

## Why it exists

The project's claim is that anyone can check Bitcoin's chain validity on low-compute hardware from a
few-KB proof. Until this existed, the only thing that could check one was `host verify-snark` — inside a
binary built around a full RISC0 **prover** and the guest ELF. So we could produce the artifact and not
hand anyone a way to check it. That was the gap in #19 / #24.

| Binary | Size |
|---|---|
| `host` (CUDA) | 312 MB |
| `host` (CPU) | 69 MB |
| **`hazync-verify`** | **1.6 MB** |

Measured on x86-64 Linux: **sub-millisecond** verification, **2.4 MB** peak RSS, and the only dynamic
dependencies are `libc` and `libgcc`.

The size comes from what it *doesn't* have. `risc0-zkvm` is pulled with `default-features = false`, so
there is no prover, and it deliberately does **not** depend on the `methods` crate — that crate builds
the guest, which is most of the bulk. A verifier needs the guest's *image id*, not the guest.

## What it checks

All five, in order. It asserts exactly what `host verify-snark` asserts:

1. the SNARK verifies against the canonical guest image id
2. the journal's `self_id` equals that same id — recursion pinned to this guest (S1)
3. the domain tag is `KIND_RANGE` — not some other receipt shape (H8)
4. the range starts at block 1
5. the in-boundary **is** genesis: hash, empty UTXO set, nBits, epoch start, recent-times, prev-time

**(5) is the one that matters.** Without it, a valid proof of an arbitrary mid-chain range would pass
and the claim collapses to "someone proved a thousand blocks somewhere". A smaller artifact that
checked less would be worse than the receipt it replaces — it would make a fabricated-anchor range
*more* shareable. Verified rejections:

```
$ hazync-verify neg500.snark          # valid proof, but [500..500]
NOT A GENESIS-ANCHORED CHAIN PROOF

  The SNARK is VALID and was produced by guest 3867611d.

$ hazync-verify bitflipped.snark      # one byte changed
VERIFICATION FAILED: the proof is not valid for guest 3867611d — forged, tampered, corrupt, or produced by a different guest build.
  underlying: verification indicates proof is invalid
```

Note how differently the two read, because they are different claims. The bitflipped proof is *invalid*:
nothing can be concluded from it. `neg500` is a perfectly **valid** proof that simply does not say what
a chain proof must say — so the verifier confirms its validity and its guest, then refuses the anchor.
Collapsing both into "VERIFICATION FAILED" would hide exactly the distinction that matters when someone
shows you a proof of something real and hopes you read it as a proof of something else.

## The embedded image id

`METHOD_ID_HEX` is a literal, because importing it from `methods` would drag in the guest build. That
makes it invisible to the doc-drift scan, so `scripts/check-versions.sh` has an explicit check that it
equals `reproduce/METHOD_ID`. **A re-baseline must update this constant**, or the verifier will silently
reject every current proof. CI fails the build if it drifts.

## Limitations, stated plainly

- **ARM64 is demonstrated; a handset is not.** It cross-compiles to `aarch64-unknown-linux-gnu` with
  no source changes (1,643,112 bytes) and, executed as ARM64 code under `qemu-aarch64-static`, both
  verifies a genuine genesis-anchored proof and rejects a non-genesis one on the pin — see
  `prover/evidence/verifier_aarch64.txt`. What is still *not* shown is execution on physical phone
  hardware or an Android/iOS package; the latter needs the NDK, which is packaging rather than
  portability. Timing under emulation is not representative and is deliberately not quoted.
- It verifies **range** proofs (`KIND_RANGE`) that are genesis-anchored. It is not a general receipt
  verifier; `host verify-any` remains the tool for un-anchored ranges.
- The `RangeState` layout is mirrored from the guest by hand. Field order is load-bearing — the journal
  decodes positionally, so a reordering misinterprets a valid proof rather than failing loudly. The
  struct is currently duplicated in three places (guest, host, here) with no shared crate.

# Prebuilt verifier binaries

| File | Target | Size | sha256 |
|---|---|---|---|
| `hazync-verify-aarch64` | `aarch64-unknown-linux-gnu` | 1,643,112 B | `b89cefce8099…` (see `.sha256`) |

Built 2026-07-28 from the tree at `f02ca11`, cross-compiled with
`aarch64-linux-gnu-gcc`. Verified as ARM64 code under `qemu-aarch64-static` —
accepts the genesis-anchored fixture, rejects the non-genesis one on the pin.
Full record: `prover/evidence/verifier_aarch64.txt`.

Pinned to guest image id `3f52baff…`. **A re-baseline invalidates it** — it will
reject every proof made against a new guest. Rebuild and replace it as part of
the re-baseline; `scripts/check-versions.sh` guards the source constant, but it
cannot check a committed binary.

## Why a binary is in the tree at all

It is here because it is otherwise **unreproducible without a cross-toolchain**,
and the machine it was built on was rented by the hour and released the same day.
1.6 MB is a cheap insurance premium against having to reconstruct that setup.

That said, **GitHub Releases is the right long-term home** for build artifacts —
a binary in git is dead weight that every clone pays for, and it cannot be
verified by CI the way source can. Attach these to the next tagged release and
drop them from the tree.

## Rebuilding

```sh
rustup target add aarch64-unknown-linux-gnu
sudo apt-get install gcc-aarch64-linux-gnu
CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
  cargo build --release --target aarch64-unknown-linux-gnu \
  --manifest-path verifier/Cargo.toml
```

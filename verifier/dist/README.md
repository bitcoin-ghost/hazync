# Prebuilt verifier binaries

| File | Target | Size | sha256 |
|---|---|---|---|
| `hazync-verify-aarch64` | `aarch64-unknown-linux-gnu` | 1,708,632 B | `2b4b6b64972d…` (see `.sha256`) |

Rebuilt 2026-07-30 for canonical guest `85dc0b56…` (accumulator domain
separation), cross-compiled with `aarch64-linux-gnu-gcc` in a container.
Verified as ARM64 code under `qemu-aarch64-static` — accepts the
genesis-anchored fixture (`fold_8.snark`, exit 0), rejects the non-genesis one
on the pin (exit 2).

The binary was REBUILT, not edited. It embeds the guest id in compiled code, and
substituting one 64-hex string for another is length-preserving — it would have
produced a binary claiming the new id while containing the old build, which is
worse than a corrupt one. Confirmed: 0 occurrences of `3f52baff` remain.

Still qemu, not real silicon — see #41. Full record:
`prover/evidence/verifier_aarch64.txt`.

Pinned to guest image id `85dc0b56…`. **A re-baseline invalidates it** — it will
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

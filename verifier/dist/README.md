# Prebuilt verifier binaries

| File | Target | Size | sha256 |
|---|---|---|---|
| `hazync-verify-aarch64` | `aarch64-unknown-linux-gnu` | 1,708,632 B | `d25ab8949eff…` (see `.sha256`) |

Rebuilt for canonical guest `dfc9eeda…`, cross-compiled with
`aarch64-linux-gnu-gcc` in a container. This is the same binary published as the
v0.14.0 release asset.
Verified as ARM64 code under `qemu-aarch64-static` — accepts the
genesis-anchored fixture (`fold_8.snark`, exit 0), rejects the non-genesis one
on the pin (exit 2).

The binary was REBUILT, not edited. It embeds the guest id in compiled code, and
substituting one 64-hex string for another is length-preserving — it would have
produced a binary claiming the new id while containing the old build, which is
worse than a corrupt one. Confirmed: 0 occurrences of any superseded id remain.

Still qemu, not real silicon — see #41. Full record:
`prover/evidence/verifier_aarch64.txt`.

Pinned to guest image id `71790584…`. **A re-baseline invalidates it** — it will
reject every proof made against a new guest. Rebuild and replace it as part of
the re-baseline.

This went wrong once, which is why the check below exists: the `be5e0528`
(2026-07-31)
re-baseline rebuilt the *release asset* but not this committed copy, so the tree
carried an `85dc0b56` verifier — self-consistent with its own `.sha256`, and
silently wrong. `scripts/check-versions.sh` now greps the committed binary for
the canonical id, so a stale copy fails the build rather than shipping.

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

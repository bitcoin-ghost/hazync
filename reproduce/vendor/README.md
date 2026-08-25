# Vendored build artefacts

## `recursion_zkr.zip`

The recursion circuit's zkr blob, consumed by `risc0-circuit-recursion`'s build script.

```
sha256  744b999f0a35b3c86753311c7efb2a0054be21727095cf105af6ee7d3f4d8849
size    59,768,781 bytes
```

**Why it is committed.** That crate's `build.rs` downloads this file from
`risc0-artifacts.s3.us-west-2.amazonaws.com` during `cargo build`. On 2026-08-24 the object began
returning **HTTP 403** from every network tried — Helsinki, Vienna and the UK — which broke every
from-scratch container build, including cutting a release. It had worked hours earlier.

`reproduce/Dockerfile` exists so that anyone can rebuild the guest and get the same `METHOD_ID`. That
guarantee cannot rest on a third party's bucket policy. Vendoring the artefact removes the last
network fetch from a cold build, so the container is reproducible **offline**.

**This bypasses a fetch, not a check.** `build.rs` prefers a local path and verifies it:

```rust
if src_path.exists() && check_sha2(&src_path) { copy; return; }
... download ...
```

`check_sha2` compares against the `SHA256_HASH` compiled into the crate, which is the same hash the
S3 key is named after. Supplying the file locally satisfies the identical check the download would
have. The artefact is self-verifying: its name *is* its hash.

**Wiring.** `reproduce/Dockerfile` sets `RECURSION_SRC_PATH` to point here. If the file is missing the
build still works — `build.rs` falls back to downloading — so this is a safety net, not a
requirement.

**If risc0 bumps the recursion circuit version**, `SHA256_HASH` changes and this file goes stale. The
build will then download the new one (when reachable) and this should be re-vendored. Verify any
replacement against the hash in `build.rs` before committing it.

Tracked as hazync#164.

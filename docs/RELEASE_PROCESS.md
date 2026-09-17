# Release process

How a release is cut, what has to change around it, how the browser verifier is deployed to both sites,
and how the guest is re-baselined. The scripts are authoritative: `scripts/release.sh`,
`scripts/package-release.sh`, `scripts/check-dist.sh`, `.github/workflows/release-sign.yml`,
`scripts/rebaseline-id.sh`, `scripts/check-deployed-verifier.sh`. Coordinator deployment and day-to-day
operation stay in [`coordinator/deploy/RUNBOOK.md`](../coordinator/deploy/RUNBOOK.md); the re-baseline
material below moved here from it on 2026-09-14.

## 1. Before the release: what lands on `main` first

`release.sh` refuses to run unless the working tree is clean, `HEAD` is `origin/main`, and CI has a passing
run on that commit. Everything in this list therefore has to be merged **before** the release is cut.

- [ ] **Release notes** in a file, passed as `NOTES=<file>` (step 5 refuses without one). Keep a copy in
      `docs/history/releases/RELEASE_NOTES_vX.Y.Z.md`, as the earlier releases are.
- [ ] **`CHANGELOG.md` entry**, newest first, summarised from the notes; mark it **re-baseline** if the id
      moved (the file's own maintenance note).
- [ ] **`docs/PROVING.md`**: bump "The current release is **vX.Y.Z**". `scripts/check-versions.sh` check 4
      reads that line: naming a version newer than the newest tag passes (a release in preparation), older
      fails.
- [ ] **`docs/STATUS.md`**: new release, date and shipped items. The board snapshot can follow the release.
- [ ] **`docs/COORDINATOR_REFERENCE.md`**, if `coordinator/server.py`, `coordinator/hazync` or
      `coordinator/run-workers.sh` changed: `python3 scripts/gen-coordinator-reference.py`. CI's `--check`
      fails otherwise, and `release.sh` needs CI green.
- [ ] **`verifier-wasm/README.md`** raw and gzipped sizes, if the wasm changed size.
      `check-deployed-verifier.sh` fails until the README's `raw` figure equals the deployed module.
- [ ] A re-baseline also needs everything in [§5](#5-re-baseline-the-guest-id-changed).

## 2. Cutting it: `scripts/release.sh`

```bash
./scripts/release.sh vX.Y.Z --dry-run                   # preflight + build + gate; publishes nothing
NOTES=<notes file> ./scripts/release.sh vX.Y.Z          # the release
./scripts/release.sh vX.Y.Z --verify-only               # resume at signing for a published, unsigned release
```

The tag must match `^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$`, which `release-sign.yml` also enforces. The
script is resumable: each phase skips work already done.

1. **Preflight.** Clean tree; `HEAD == origin/main`; tag free locally and on origin;
   `check-versions.sh` passes; at least one passing and no failed CI run on `HEAD` (a run whose only failed
   job is `deployed-verifier` is not counted, because that job tests the live site and is red by construction
   until a re-baseline is deployed); `docker` present; at least 25 GB free.
2. **Build hosts** (`prover/build-release.sh cpu` and `cuda`, with `SKIP_GROTH16=1` and `RZUP_TIMEOUT`
   defaulting to 7200). A staged host is reused only if `dist/.built-from-<asset>` equals `HEAD` **and** it
   carries the canonical id; the id alone is not a staleness signal for a host-only release.
3. **Package** (`scripts/package-release.sh` with `TAG`): `hazync-worker` with `VERSION` stamped from the tag
   and asserted, `hazync-coordinator.py`, `hazync-run-workers.sh`, and `hazync-verify.wasm` only if a
   `wasm32-unknown-unknown` target is installed (otherwise it warns and a wasm built elsewhere must be staged
   in `dist/`). The x86_64 verifier is built unless the staged one already contains the canonical id.
4. **Gate** (`scripts/check-dist.sh dist`): every staged artifact must belong to the canonical guest. A CUDA
   host that cannot run on the box is checked by its bytes (`scripts/embeds-method-id.sh`), or by an operator
   attestation in `HAZYNC_ATTEST_hazync_host_x86_64_linux_gnu_cuda=<id measured on a GPU>`, which is stronger.
5. **Publish.** Annotated tag pushed, then `gh release create` with **all seven** assets in one call:
   `release-sign.yml` signs what is attached when the release is published and picks up nothing later.
6. **Wait for signing**: the `Sign release` run whose `headBranch` is this tag. On failure or timeout the
   script prints how to re-run it and how to keep `latest` on the last signed release.
7. **Manifest**: `SHA256SUMS.txt` must list the seven assets plus `hazync-verify-aarch64`, and
   `SHA256SUMS.txt.asc` should verify (reported, not fatal, if the key is not imported locally).
8. **`latest`**: set `make_latest`, read `/releases/latest` back, then download
   `/releases/latest/download/hazync-verify-x86_64-linux-gnu` until it contains the canonical id (the CDN
   serves the previous binary for minutes).

`release-sign.yml` itself, on `release: published` or `workflow_dispatch` with a tag: builds
`hazync-verify-aarch64` from the tag, asserts it embeds the canonical id and attaches it; then downloads the
`hazync-*` assets, fails if any `hazync-*` asset would be left out, writes `SHA256SUMS.txt`, signs it with
`GPG_PRIVATE_KEY`, and uploads `SHA256SUMS.txt` and `SHA256SUMS.txt.asc`.

Not automated, and still required (the script says so at the end): smoke-test the CUDA host on a real GPU
(`method-id`, `regress`, `prove-block`), and repoint running provers, because `run-workers.sh` checks the
guest id only at startup (#99).

## 3. The browser verifier

Served at `https://hazync.org/verify/` (from `bitcoin-ghost/hazync-web`). A copy that does not match the release is caught only by
calling it: a stale module has the same size and exports as a correct one. Do this after every release whose
`hazync-verify.wasm` differs from the deployed one, and in the **same cutover** as the coordinator's binary
swap on a re-baseline (§5).

First fetch the wasm from the release and check it against the signed manifest:

```bash
curl -fLO https://github.com/bitcoin-ghost/hazync/releases/download/vX.Y.Z/hazync-verify.wasm
curl -fLO https://github.com/bitcoin-ghost/hazync/releases/download/vX.Y.Z/SHA256SUMS.txt
curl -fLO https://github.com/bitcoin-ghost/hazync/releases/download/vX.Y.Z/SHA256SUMS.txt.asc
gpg --verify SHA256SUMS.txt.asc SHA256SUMS.txt && sha256sum -c --ignore-missing SHA256SUMS.txt
```

**hazync.org** (`hazync/hazync-web`; its `README.md` lists the pins):

1. Copy the verified wasm to `verify/hazync-verify.wasm`, and `verifier-wasm/hazync-verify.js` from this
   repository at the tag to `verify/hazync-verify.js` if it changed.
2. Update the release version and the wasm's sha256 everywhere they are pinned: `deploy/deploy.sh` (`want=`
   and its message), `tools/check.py` (`WASM_SHA` and its message), `README.md`, `devs/index.html` (the
   hash, the release link and, on a re-baseline, the program-id table) and `explorer/index.html` (the release
   named beside the in-browser verifier).
3. `deploy/deploy.sh` (a dry run; it refuses if the wasm hash differs, and runs `tools/check.py`), then
   `deploy/deploy.sh --go`.

**hazync.org** (the web box, `152.53.86.216`): back up the live module under its old guest id or tag, then
install the new one:

```bash
sudo cp -p /var/www/hazync/verify/hazync-verify.wasm \
           /var/www/hazync/verify/hazync-verify.wasm.<old id or tag>
sudo install -o www-data -g www-data -m 644 hazync-verify.wasm \
           /var/www/hazync/verify/hazync-verify.wasm
```

`check-deployed-verifier.sh` also requires `hazync-verify.js` there to equal `verifier-wasm/hazync-verify.js`;
if the loader changed, install it beside the wasm the same way (its path on the box is inferred from the
served URL, not read from a deploy script).

**Then check both, over the wire:**

```bash
./scripts/check-deployed-verifier.sh                             # hazync.org (HAZYNC_SITE default)
```

Each run requires: the served loader byte-identical to `verifier-wasm/hazync-verify.js`; the served wasm size
equal to the release asset and to `verifier-wasm/README.md`'s `raw` figure; `methodId()` and `/api/meta`
equal to `reproduce/METHOD_ID`; the live spine verified as genesis-anchored in the served module; and a
one-bit-flipped spine not verified. Both sites serve `/api/spine/proof` (measured 200 on 2026-09-14).

## 4. After the release

- [ ] `docs/STATUS.md` board snapshot, if it was not refreshed in §1.
- [ ] Restart provers on the new binaries (§2); on a re-baseline, purge their bundle caches (§5).

## 5. Re-baseline (the guest id changed)

When the guest changes, `METHOD_ID` changes, and **every proof on the board was made against the old
id** — the coordinator will (correctly) reject them all on re-verification. The board must restart from
genesis. This is not a failure; it is the price of a guest change, so batch guest changes deliberately.

### Never read an id off a binary you did not just watch get built

This is the single most repeated mistake in this process — three times in one night on two boxes during
the v0.16.0 re-baseline, twice reporting a **stale id as a live result** and once nearly stopping the
release over a disagreement that was not real.

The shape is always the same: a build fails, the previous binary is still sitting there, and
`host method-id` answers cheerfully. Nothing is missing and nothing errors — the answer is simply about
different source than you think.

So, every time, all three:

```bash
BEFORE=$(stat -c %Y "$D/prover/target/release/host" 2>/dev/null || echo 0)
REPO_DIR="$D" HAZYNC_PROVISION=build ./provision-vps.sh; RC=$?
AFTER=$(stat -c %Y "$D/prover/target/release/host" 2>/dev/null || echo 0)
[ "$RC" -eq 0 ] && [ "$AFTER" -gt "$BEFORE" ] || { echo "build did not produce a new binary"; exit 1; }
```

1. **Check the exit code** — and check the right one. `ssh … | tail` gives you `tail`'s status, not the
   build's; `$?` after a pipeline is the last element. Use `PIPESTATUS[0]` or don't pipe.
2. **Check the mtime advanced.** This is what catches the case where the build failed *early* and left
   yesterday's binary in place.
3. **Only then read the id.**

Two corollaries worth internalising:

- **Empty is not "different".** A probe that returns nothing because it used a wrong path prints a
  mismatch that looks like a re-baseline emergency. Distinguish "no answer" from "wrong answer" before
  reacting to either.
- **A disagreement between two builds is a question, not a verdict.** Check what each one actually
  compiled — commit, dirty files, and whether the binary is even from that tree — before concluding the
  id moved. A binary can be built in one place and *copied* to another; the path it sits at tells you
  nothing about the path it was built at. `strings host | grep -oE '[^ ]*coinbase-smt/src/[a-z_]+\.rs'`
  shows the paths actually recorded in it, and settles this in seconds rather than a 25-minute rebuild.

### Publishing does NOT make a release `latest` — check it, twice

Two independent failures on the v0.15.0 publish, either of which leaves `/releases/latest/` serving the
previous release for ever:

**1. `make_latest` silently did not apply.** A single `PATCH` setting `draft=false` and
`make_latest=true` together published the release but left GitHub pointing `latest` at the PREVIOUS tag.
A second PATCH with only `make_latest=true` fixed it. `gh release edit --draft=false` also did nothing
at all on a draft — a draft has no resolvable tag, so it could not find the release, and reported no
error either time.

```bash
gh api repos/<owner>/<repo>/releases/latest --jq .tag_name    # must be the tag you just cut
```

**2. The `/latest/download/` CDN caches.** For several minutes after the fix it still served the OLD
binary — at 1,723,592 bytes where the real one is 1,726,504. Both pass sha256 against their own
manifest; only the embedded id distinguishes them.

```bash
curl -sL -H 'Cache-Control: no-cache' -o v \
  https://github.com/<owner>/<repo>/releases/latest/download/hazync-verify-x86_64-linux-gnu
grep -ac <new-id> v    # want 1
grep -ac <old-id> v    # want 0
```

Check via the **`latest`** URL, not the versioned one. The versioned URL was correct throughout while
`latest` was wrong, so verifying the versioned asset proves nothing about what a downloader receives.

### The CUDA release build needs 25 GB free, and failing costs more than a failed build

`build-release.sh cuda` consumed 21 GB -> 4.4 GB on the GPU box, burning **3.7 GB/minute** while
unpacking the CUDA toolkit. It was killed a minute short of filling the root filesystem — which on a
box running services is how things die silently, not merely how a build fails.

**Check before starting, not after:**

```bash
df -h /            # want 25 GB+ free; 21 GB is NOT enough despite looking close
```

21 GB looked adequate right up until it did not. The consumption is not linear: most of it lands in a
few minutes during the toolkit unpack, so a comfortable-looking figure five minutes in means nothing.

**Where to reclaim it on a box that is short:**

- `docker system prune -af` — 5.3 GB of unused images, always safe
- `/usr/local/cuda-13.x` — RISC0 3.0.5 kernels do NOT build against 13.x, so a host-side 13.x install
  is dead weight for this purpose (~4.8 GB). `provision-vps.sh` installs 12.8 by default
  (`HAZYNC_CUDA_VER`) inside the container.
- `~/.hazync/receipts` — the prover's LOCAL copies. The coordinator holds the board's own store, so
  clearing these loses nothing, and a re-baseline invalidates them regardless (~3.7 GB for 17k).

**Pass `SKIP_GROTH16=1`.** groth16 is a runtime rzup component the host does not link against, so the
release build does not need it — it only costs a 488 MB download and, on a slow link, three timeouts.

### Set RZUP_TIMEOUT when running build-release.sh

`build-release.sh` forwards `RZUP_TIMEOUT` **only when the caller sets it** — deliberately, since a
hardcoded default in a wrapper defeats the default it wraps. The consequence is easy to walk into: the
groth16 component is a 488 MB download, and on anything short of a datacentre link it times out at the
default and burns 3 x 300 s of retries before warning and carrying on.

```bash
RZUP_TIMEOUT=7200 ./prover/build-release.sh cpu
```

It is not fatal — groth16 is a RUNTIME rzup component, not something the host links against, so the
build completes and the binary is fine without it. It just costs fifteen minutes of a release for
nothing. Cost it once on 2026-08-03.

### Anything that PRODUCES a proof must be built at the CANONICAL paths

A guest's image id embeds absolute build paths, so a host built anywhere else produces proofs that
verify against **nothing published**. This is easy to walk into: the build succeeds, the binary works,
the proofs look fine, and they are worthless.

Measured on 2026-08-03 while regenerating the SNARK fixtures — the same tree produced **three
different ids**:

| built at | id |
|---|---|
| `/home/…/dev/projects/hazync` (dev box) | `1bed31ef…` |
| `/root/hazync-rebuild` (coordinator, scratch) | `1112670d…` |
| `/hazync-zkvm` (container / canonical) | `37987b85…` |

Only the third can produce a publishable proof. To prove or wrap on a box that is not the container,
reproduce the container's environment exactly:

```
HOME=/root                       # so CARGO_HOME=/root/.cargo
HAZYNC_BASE=/root/hazync-build   # Core + secp sources
REPO_DIR=/hazync-zkvm            # the checkout itself
```

Then `REPO_DIR=/hazync-zkvm HAZYNC_PROVISION=build ./provision-vps.sh`. Verify before proving anything:

```bash
/hazync-zkvm/prover/target/release/host method-id   # MUST equal reproduce/METHOD_ID
```

If it does not match, stop — everything proved with that binary is scrap.

### The mechanical half is SCRIPTED — do not hand-edit it

**`./scripts/rebaseline-id.sh <new-64-hex-id>`**, then let `check-versions.sh` verify.

This script already existed and nothing referenced it, so the 2026-08-03 re-baseline was done by hand:
nine sites across eleven locations, each discovered by a gate failure, in five rounds. Someone then
started writing a *second* script before noticing the first. If you take one thing from this section,
take the command above.

The id comes from the container and **only** from the container — a local build produces a different
id BY DESIGN, because the ELF embeds absolute build paths and normalising them is what
`reproduce/Dockerfile` is for:

```bash
docker build -t hazync-repro -f reproduce/Dockerfile .
docker run --rm hazync-repro                 # prints the canonical METHOD_ID
./scripts/rebaseline-id.sh <that id>         # rewrites every known site
./scripts/check-versions.sh                  # the backstop, not the discovery mechanism
```

⛔ **Known bug (verified 2026-09-14, not yet fixed): `rebaseline-id.sh` exits 1 at its last check**, with
`::error::reproduce/METHOD_ID does not end with the new bare id`. The check is `tail -1 reproduce/METHOD_ID`,
and the file ends with a comment line: the bare id is line 427 of 496. Run in a throwaway worktree with a
dummy id, the script had already applied every substitution, including the bare id line, and stopped
before printing its closing checklist. Until it is fixed, confirm the bare line yourself
(`grep -nE '^[0-9a-f]{64}$' reproduce/METHOD_ID`) and work through that checklist from the end of the script:
write the supersession note, regenerate the SNARK fixtures, run `check-versions.sh`, `check-utreexo.sh` and
`check-spec.sh`, cut the release, deploy the wasm, and run `check-deployed-verifier.sh`. Its deploy step
names hazync.org, which is the only site serving the verifier since bitcoinghost.org/hazync was retired
on 2026-09-16.

If `check-versions` names a site the script missed, **add it to the script** rather than hand-editing.
That is the whole point: the gate finding something should be rare and should teach the script.

**What the script deliberately will not do**, and you must:

1. **Write the supersession note** into `reproduce/METHOD_ID` — why the id moved and what it cost. A
   script cannot write that, and it is the part future readers actually need.
2. **Regenerate `prover/testdata/snark/*.snark`.** They are PROOFS made by the old guest; a proof
   carries its guest id inside it, so they cannot be re-pointed, only re-made. Until then `snark-verify`
   fails, and it *should*. Needs Groth16 on a **CPU** host — it crashed on every CUDA build tested
   (#20, closed 2026-07-28 as an upstream defect; the CUDA path has not been re-measured since).
3. **Check the WASM and the published verifiers by EMBEDDED ID, never by size.** Swapping one 64-hex
   literal for another is length-preserving, so a stale artifact is byte-identical in size to a correct
   one, and sha256 + PGP both pass over it — they attest the bytes are the bytes, not that they are
   right.

```bash
strings <artifact> | grep -c <new-id>   # want 1
strings <artifact> | grep -c <old-id>   # want 0
```

The aarch64 verifier is no longer committed (#85) — `release-sign.yml` builds it, asserts its embedded
id, and attaches it. Nothing guest-dependent should be committed under `verifier/dist/` again; a gate
now fails if it is.

### Ride-alongs — guest changes worth batching into ANY re-baseline

The board reset is the expensive part, and it costs the same whether one thing changes or five. So a
change that is not worth a re-baseline on its own becomes free once one is happening anyway. Check this
list whenever a re-baseline is planned:

- [ ] **Journal byte-packing** (was issue #22). risc0 serde commits each `u8` as its own 32-bit word, so
      a 32-byte Utreexo root goes on the wire as 128 bytes. Packing recovers 96 B per root: measured
      ~20% on a genesis-anchored `[1..1000]` wrap (3,441 B -> ~2,770 B) and ~25% on a projected
      full-chain wrap (~5,360 B -> ~4,020 B). **Deliberately NOT worth a re-baseline alone** — a few KB
      stays a few KB, and it buys no product capability, since 4 KB and 5.4 KB are equally trivial for a
      phone to fetch and verify. Free if the guest is changing regardless. Treat the percentages as an
      inferred two-point fit, not a measurement.

### Things that MUST be updated when the id changes

**The browser verifier is on this list and is easy to forget.** `verifier-wasm` embeds `METHOD_ID_HEX`
via the verifier crate, so the `.wasm` served at `/hazync/verify/` is a pinned artifact exactly like the
native binaries. Left stale it rejects every new proof, on the page the README leads with.

⚠ **Size is not a staleness signal for ANY of these.** Swapping one 64-hex literal for another is
length-preserving: the correct post-re-baseline `.wasm` is byte-for-byte the same size as the stale one
(1,063,349 B both sides, measured 2026-08-02). Check the embedded id, never the size:

```bash
strings <artifact> | grep -c <new-id>   # want 1
strings <artifact> | grep -c <old-id>   # want 0
```

**Deploy the wasm, to both sites ([§3](#3-the-browser-verifier-on-both-sites)), in the SAME cutover as the
coordinator binary swap and board reset.** Earlier and the
browser rejects the still-live old board; later and it rejects the new one. There is no safe order
other than together.


Easy to miss, and each fails in a way that looks like something else:

- [ ] `reproduce/METHOD_ID` — the source of truth; update it FIRST.
- [ ] `reproduce/LINEAGE.tsv` — `scripts/lineage.sh --write` appends the new row from git history once the
      id change is committed; `lineage.sh --check` is the CI gate.
- [ ] `CHANGELOG.md` — mark the entry **re-baseline**.
- [ ] hazync.org's pins, including the program-id table in `devs/index.html` (§3).
- [ ] `verifier/src/lib.rs` and `verifier-ffi/src/lib.rs` `METHOD_ID_HEX` — the verifiers embed the id
      as a literal (they cannot import it without dragging in the guest build).
      `scripts/check-versions.sh` fails the build if either drifts. The published verifier binaries are
      built by `release-sign.yml`, which asserts their embedded id; nothing is committed under
      `verifier/dist/` any more (#85), so there is nothing there to replace — cut a release.
- [ ] `prover/testdata/snark/*.snark` — the CI Groth16 fixtures are pinned to the id and will start
      failing `ci_snark_verify.sh`. Regenerate per `prover/testdata/snark/README.md`.
- [ ] **the archive bridge's binary** — it produces the bundles everyone else consumes. Missing it once
      already stalled the board dead while every other component looked healthy.
- [ ] docs stating the current id (`docs/PROVING.md`, `SECURITY.md`, `README.md`) — `check-versions.sh` enforces.

The coordinator derives the id it expects from its **own** `HAZYNC_HOST` binary (`expected_method_id()`,
served at `/api/meta`), so the swap is: new binary in, board cleared, workers restarted.

Read the real paths off the unit first — they are env-driven, so do not assume a layout. Use
`systemctl show`, not `systemctl cat`, which prints lines a drop-in has superseded (see
[Backup & restore](../coordinator/deploy/RUNBOOK.md#backup--restore) in the runbook):

```bash
systemctl show hazync-coordinator -p WorkingDirectory -p Environment | tr ' ' '\n' \
  | grep -E 'WorkingDirectory|COORD_DB|COORD_PROOFS|COORD_SPINE|HAZYNC_HOST'
```

On the production coordinator those are `COORD_DB=/var/lib/hazync/coordinator.db`,
`COORD_PROOFS=/var/lib/hazync/proofs`, `HAZYNC_HOST=/usr/local/bin/hazync-host`, with the checkout at
`/opt/hazync`. Substitute yours — and note these MOVED on 2026-08-02 (#58): everything used to live
under `/root`, which is `0700 root` and therefore blocked `User=`, `ProtectHome=` and
`ProtectSystem=strict` on both units.

**Both services now resolve the same binary** (`/usr/local/bin/hazync-host`). That is not cosmetic: it
structurally removes the split-binary failure described in step 2 below, where the bridge was left on
an old guest while the coordinator was upgraded. There is now one path to swap, not two.

```bash
# 1. BACK UP FIRST — the old ledger + receipts are the historical record of the previous baseline.
#    backup.sh honours COORD_DB/COORD_PROOFS, so pass them if they are not under $HZ_HOME.
COORD_DB=/var/lib/hazync/coordinator.db COORD_PROOFS=/var/lib/hazync/proofs \
  BACKUP_DIR=/var/lib/hazync/backups /opt/hazync/coordinator/deploy/backup.sh
cd /var/lib/hazync/backups/<STAMP> && sha256sum -c SHA256SUMS    # verify before relying on it
```

⚠️ Without `BACKUP_REMOTE` the snapshot sits on the **same disk** as the data. For a re-baseline that is
tolerable *only* because step 3 archives in place rather than deleting — but copy the DB off-box anyway;
it is the attribution ledger and it is small.

```bash
# 2. Stop, swap the host binary (KEEP the old one — it is what re-verifies the archived proofs).
#
#    ⚠️ THE COORDINATOR IS NOT THE ONLY BINARY. The archive bridge PRODUCES the witnesses everyone
#    else consumes, and it is a separate service with its own ExecStart path. Missing it is what
#    caused the v0.10.0 stall: the bridge stayed on a pre-v0.9.0 host, emitted bundles without the
#    `txs` field, and every prover panicked with "missing field `txs`" the moment the board reached
#    the first bundle it had written. Swap BOTH, then run scripts/check-deployment.sh --local.
systemctl stop hazync-coordinator hazync-bridge
cp /usr/local/bin/hazync-host /root/hazync-host.bak.<OLD_ID_PREFIX>   # KEEP: re-verifies archived proofs
install -m755 ./host-new /usr/local/bin/hazync-host
/usr/local/bin/hazync-host method-id                             # MUST equal reproduce/METHOD_ID

# 3. Clear the board. Archive, never delete — proofs/ is the artifact the "don't trust us" claim
#    rests on, and the old ledger stays re-verifiable with the archived binary.
mv /var/lib/hazync/coordinator.db /var/lib/hazync/coordinator.db.<OLD_ID_PREFIX>
mv /var/lib/hazync/proofs /var/lib/hazync/proofs.<OLD_ID_PREFIX>
mkdir /var/lib/hazync/proofs && chown hazync:hazync /var/lib/hazync/proofs

# ⚠ THE SPINE TOO — it is a PROOF and it does not live with the others.
# COORD_SPINE defaults to <server.py dir>/spine, i.e. INSIDE the checkout, and it is untracked — so
# `git checkout <tag>` does not touch it and it survives every step above. Left in place, the
# coordinator keeps serving a genesis-anchored proof made by the OLD guest, and /api/spine/proof —
# the exact command the README's 30-second demo tells strangers to run — fails against the new
# verifier. Missed on 2026-08-02 and caught only by walking the published path as a stranger.
# ⚠⚠ READ COORD_SPINE FROM THE UNIT — DO NOT ASSUME THE DEFAULT. Production overrides it to
#    /var/lib/hazync/spine, so archiving <server.py dir>/spine (the default) moves an EMPTY leftover
#    directory and leaves the real, old-guest spine live and being served. That happened on
#    2026-08-03: the board reported proven 0 / frontier 0 and spine_hi 8156, and /api/spine/proof kept
#    returning a proof made by the retired guest. The 8156 was the only visible symptom.
SPINE=$(systemctl show hazync-coordinator -p Environment --value | tr ' ' '\n' \
        | grep -oP 'COORD_SPINE=\K.*')
SPINE=${SPINE:-/opt/hazync/coordinator/spine}
echo "spine is at: $SPINE"          # CHECK THIS before moving anything
mv "$SPINE" "$SPINE.<OLD_ID_PREFIX>"
mkdir "$SPINE" && chown hazync:hazync "$SPINE"
# ⚠ the services run as USER hazync since #58 — anything you recreate by hand must be chowned, or the
#   coordinator starts, serves reads, and silently fails every write. That includes the PARENT
#   directory: /var/lib/hazync itself must be hazync-owned, or sqlite cannot CREATE the new DB and
#   the service dies at startup with "unable to open database file". Hit on 2026-08-02 despite this
#   warning already being written — an existing file is writable, a missing one needs a writable dir.

# 4. Start BOTH — init_db() reseeds the open ranges; the frontier restarts at 0.
systemctl start hazync-coordinator hazync-bridge
curl -s localhost:8899/api/meta      # method_id == the new id, frontier == 0

# 5. Verify no component was left behind. This is the check that would have caught the v0.10.0 stall.
./scripts/check-deployment.sh --local
```

### Then purge the provers' bundle caches

Regenerating bundles is **not enough**. The contributor CLI caches by height
(`BUNDLE_DIR/bundle_<h>.json`) and skips re-fetching when the file exists, so a prover that already
pulled a stale bundle keeps replaying it and keeps failing — long after the bridge is fixed. This cost
real time to spot, because the coordinator was serving the correct bundle the whole while.

On every prover:

```bash
./coordinator/run-workers.sh 4 --stop           # stops the loops AND their children; a bare pkill misses
                                                 # them (the child is `python3 ./hazync-worker run`)
rm -rf "$BUNDLE_DIR" ~/.hazync/bundles          # whatever BUNDLE_DIR the workers use
./coordinator/run-workers.sh 4                   # re-fetches cleanly; refuses to start on an id mismatch
```

Confirm a bundle actually re-fetched (a fresh mtime, and `txs` present) before assuming it worked.

Then restart the provers (they pre-flight with `hazync selftest`, which now compares against the new
`/api/meta` id and fails loudly on a stale worker binary — so update every worker's `HAZYNC_HOST` too).

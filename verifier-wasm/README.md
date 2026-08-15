# hazync-verify-wasm

The Hazync verifier, compiled to WebAssembly. Verifies a chain proof **in the browser, on the
device** — a phone, a laptop, anything with a browser. No install, no toolchain, no node, no chain
data, no network.

```
raw       1,065,791 bytes
gzipped     295,219 bytes   ← what a browser actually downloads (`gzip -c < FILE`; passing the path
                               instead writes the filename into the gzip header and changes the count)
verify           21 ms      ← blocks 1..1000, from a 3,441-byte SNARK  (x86-64, node 24)
verify          254 ms      ← blocks 1..1789, from the 226,434-byte STARK spine
```

Both verify figures matter, because they are different artifacts and a reader who sees only the first
will think the second is broken. The 21 ms is a Groth16-wrapped range; the 254 ms is the STARK receipt
that `/api/spine/proof` actually serves today, which is the file most people will drop on the page.

The raw size must agree in three places — `target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm`,
the `hazync-verify.wasm` release asset, and whatever is deployed at `$HAZYNC_SITE/verify/hazync-verify.wasm`
— and `scripts/check-deployed-verifier.sh` now prints all three alongside the figure above and fails if
this README disagrees. Size is a weak check by itself: the 2026-08-11 stale deploy was byte-for-byte the
same length as the correct module. It is asserted because it is nearly free, never instead of the guest
id and verdict checks.

## Why not an API

Because an API would defeat the point. If a device asks a server whether a proof is valid, the device
trusts the server — we would have replaced "trust Bitcoin Core's developers chose a good `assumevalid`
hash" with "trust our API", which is a *larger* trust assumption than the status quo, not a smaller
one. The entire argument for this project is that the check is cheap enough to do yourself.

290 KB is cheap enough to do yourself.

## Why no wasm-bindgen

wasm-bindgen would generate `hazync-verify.js` instead of it being hand-written, but it also puts a
version-matched codegen step between the Rust source and the artifact people are asked to trust. A
verifier's whole job is to be checkable. This builds with

```sh
./build.sh          # cargo build --release --target wasm32-unknown-unknown, and nothing else
```

so anyone can rebuild the `.wasm` and compare it byte for byte against what is being served. The cost
is ~40 lines of glue in `hazync-verify.js`, short enough to read in full before running it.

## One implementation of the rules

This crate contains **no verification logic**. It calls `hazync_verify::verify` — the same function
the CLI and the C ABI (`verifier-ffi`, which ghostd links) call. The anchoring rules are the entire
product claim, and a second copy of them would be a second place for them to be wrong; the wrong copy
would most likely fail *open*, accepting a sound-but-unanchored proof, which is exactly what this
tool exists to prevent.

`test-parity.sh` asserts the CLI and the WASM build return the same verdict for every fixture,
including mutations that must be refused:

```
CLI exit 0  <->  status "verified"
CLI exit 1  <->  status "invalid"
CLI exit 2  <->  status "not_anchored"
```

## Usage

```js
import { loadVerifier } from './hazync-verify.js';

const v = await loadVerifier(fetch('./hazync-verify.wasm'));
const result = v.verify(new Uint8Array(await file.arrayBuffer()));

result.status  // "verified" | "invalid" | "not_anchored"
```

`verified` carries everything the proof commits to — height, tip hash, cumulative work, UTXO
commitment, and the difficulty / median-time context. It is the same JSON as `hazync-verify --json`.

`not_anchored` is a **refusal, not a pass**. It is reported distinctly because the proof-party board
links every range to its proof, so mid-chain segments are the most common thing anyone will drop in,
and those are cryptographically perfect. Calling one "forged" would make the board look broken.

## Serving it

`index.html` is a drop-a-proof page. It must be served over http(s) — opening it from `file://`
blocks WebAssembly instantiation.

```sh
./build.sh
cp target/wasm32-unknown-unknown/release/hazync_verify_wasm.wasm hazync-verify.wasm
python3 -m http.server 8000     # then open http://localhost:8000/
```

Serve the `.wasm` with `Content-Encoding: gzip` — it compresses 3.7×, and nginx will not do it for
`application/wasm` unless told to (see `gzip_types`).

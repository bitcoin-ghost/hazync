// Glue for the Hazync WASM verifier. ~40 lines, hand-written on purpose.
//
// There is no wasm-bindgen here. That tool would generate this file, but it would also put a
// version-matched codegen step between the Rust source and the artifact people are asked to trust.
// A verifier's whole job is to be checkable, so the .wasm is a plain
// `cargo build --target wasm32-unknown-unknown` output that anyone can rebuild and compare byte for
// byte, and this file is short enough to read in full before you run it.
//
// Nothing here touches the network. The proof is verified on the device.
//
// Usage (browser):
//     import { loadVerifier } from './hazync-verify.js';
//     const v = await loadVerifier(fetch('./hazync-verify.wasm'));
//     const result = v.verify(new Uint8Array(await file.arrayBuffer()));
//
// Usage (node):
//     const v = await loadVerifier(fs.readFileSync('./hazync_verify_wasm.wasm'));

/** Read a `[u32 little-endian length][UTF-8 bytes]` block out of the module's linear memory. */
function readString(memory, ptr) {
  const view = new DataView(memory.buffer);
  const len = view.getUint32(ptr, true);
  return new TextDecoder().decode(new Uint8Array(memory.buffer, ptr + 4, len));
}

/**
 * @param source  a Response/Promise<Response> (browser), or ArrayBuffer/Uint8Array (node).
 * @returns {{verify: (bytes: Uint8Array) => object, methodId: () => string}}
 */
export async function loadVerifier(source) {
  const resolved = await source;
  // streaming instantiation where the host supports it, buffer instantiation otherwise
  const { instance } =
    typeof Response !== 'undefined' && resolved instanceof Response
      ? await WebAssembly.instantiateStreaming(resolved, {})
      : await WebAssembly.instantiate(
          resolved instanceof Uint8Array ? resolved.buffer ?? resolved : resolved,
          {}
        );

  const { alloc, verify_proof, method_id, memory } = instance.exports;

  return {
    /**
     * Verify a serialised proof. Returns the parsed result object, which always carries `status`:
     *   "verified"      valid AND genesis-anchored
     *   "invalid"       forged, tampered, corrupt, or a different guest
     *   "not_anchored"  cryptographically valid, but a mid-chain segment — a refusal, not a pass
     */
    verify(bytes) {
      const ptr = alloc(bytes.length);
      // Re-read memory.buffer after alloc: growing linear memory detaches any earlier ArrayBuffer,
      // so a view captured before the call would silently write into a dead buffer.
      new Uint8Array(memory.buffer, ptr, bytes.length).set(bytes);
      return JSON.parse(readString(memory, verify_proof(ptr, bytes.length)));
    },

    /** The guest image id this module verifies against. */
    methodId() {
      return readString(memory, method_id());
    },
  };
}

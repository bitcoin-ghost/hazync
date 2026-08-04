#![no_main]
use libfuzzer_sys::fuzz_target;
use audit_fuzz::forest_cache::Seq;

// Differential fuzz of the CACHED `Forest` against the pre-#40 implementation it replaced (hazync#50,
// item 2).
//
// #50 asks for this directly rather than inferring equivalence from emitted bundles: the bundle
// evidence was byte-identical output over 100 real blocks, which is evidence over the blocks tested,
// not a property for all inputs. This drives the structure itself with arbitrary add/delete sequences.
//
// It complements the exhaustive unit test rather than repeating it. That one covers every index of
// every size up to 40 (plus the power-of-two neighbourhoods) but only ever performs a fixed, short op
// sequence. This one reaches long interleavings — the shapes that arise when a delete re-shapes the
// forest under a later add, and where a cache error can survive several operations before it is read.
fuzz_target!(|s: Seq| {
    audit_fuzz::forest_cache::run(s);
});

//! Run the cached-vs-reference `Forest` differential WITHOUT libFuzzer or a nightly toolchain.
//!
//! `cargo fuzz` needs nightly and cargo-fuzz installed, which makes the differential in
//! `fuzz_targets/forest_cache_equivalence.rs` unrunnable for anyone who has neither — including CI
//! jobs on stable. This drives the exact same `forest_cache::run` over seeded pseudo-random
//! sequences, so the check is always available even though the coverage-guided search is not.
//!
//!     cargo run --release --example drive
//!
//! Deterministic on purpose: a failure here is reproducible from the seed alone, with no corpus to
//! carry around. It is a smoke test of the property, not a substitute for the campaign.

use audit_fuzz::forest_cache::{run, FOp, Seq};

fn main() {
    let mut st: u64 = 0xA5A5_1234;
    let mut mix = || {
        st = st.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = st;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    };

    let rounds = 200;
    for round in 0..rounds {
        // Vary the length so short sequences (where deletes hit small, shape-changing forests) and
        // long ones (where a cache error must survive many operations to be seen) both occur.
        let n = 20 + (mix() % 180) as usize;
        let ops: Vec<FOp> = (0..n)
            .map(|_| {
                if mix() % 3 == 0 {
                    FOp::Delete { idx: (mix() % 65536) as u16 }
                } else {
                    FOp::Add
                }
            })
            .collect();
        run(Seq { ops });
        if round % 50 == 0 {
            println!("  round {round} ok");
        }
    }
    println!("{rounds} randomised sequences: cached Forest == pre-cache reference at every step");
}

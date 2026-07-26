#![no_main]
use libfuzzer_sys::fuzz_target;
use audit_fuzz::Scenario;

// Differential control: same scenario against the UNHARDENED reference Stump (pre-SEC-2).
// Expected to break quickly — proving the harness detects the bug class the guest hardens against.
fuzz_target!(|s: Scenario| {
    audit_fuzz::run_reference(s);
});

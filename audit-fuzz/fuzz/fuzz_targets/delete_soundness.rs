#![no_main]
use libfuzzer_sys::fuzz_target;
use audit_fuzz::Scenario;

// Drive the guest's hardened Utreexo `delete` with honest + forged spends, asserting
// SOUNDNESS / ATOMICITY / COMPLETENESS against a ground-truth Forest oracle.
fuzz_target!(|s: Scenario| {
    audit_fuzz::run(s);
});

//! `hazync-verify` — the whole trust check, and nothing else.
//!
//! The product claim is that anyone can check Bitcoin's chain validity on low-compute hardware from a
//! few-KB proof. Until now the only thing that could check one was `host verify-snark`, which lives in a
//! 69 MB (CPU) / 312 MB (CUDA) binary built around a full RISC0 *prover* and the guest ELF itself. That
//! is the gap in #19/#24: we could produce the artifact but not hand anyone a way to check it.
//!
//! This binary verifies a genesis-anchored SNARK range proof and does nothing else. It never proves,
//! never touches the chain, needs no peers and no node.
//!
//! It asserts exactly what `host verify-snark` asserts — a verifier that checked LESS would be worse
//! than the receipt it replaces, because it would make a fabricated-anchor range *more* shareable:
//!
//!   1. the STARK/SNARK verifies against the canonical guest image id
//!   2. the journal's `self_id` equals that same image id      (S1: recursion pinned to this guest)
//!   3. the domain tag is KIND_RANGE                            (H8: not some other receipt shape)
//!   4. the range starts at block 1                             (genesis-anchored)
//!   5. the in-boundary IS genesis — hash, empty UTXO set, nBits, epoch start, recent-times, prev-time
//!
//! (5) is the one that matters most. Without it a valid proof of some *arbitrary* mid-chain range would
//! pass, and the whole claim collapses to "someone proved a thousand blocks somewhere".
//!
//! **Those checks now live in `lib.rs`**, so that this CLI, the C ABI and the WASM build share one
//! implementation of them. This file is only argument handling and presentation.

use hazync_verify::{verify, VerifyError, METHOD_ID_HEX};

fn die(msg: &str) -> ! {
    eprintln!("VERIFICATION FAILED: {msg}");
    std::process::exit(1);
}

/// The proof VERIFIED cryptographically but describes a range this tool does not accept.
///
/// Kept separate from `die` on purpose. The proof-party board links every range to its proof, so the
/// most common way anyone runs this binary is on a mid-chain segment — and telling them "VERIFICATION
/// FAILED" for a perfectly good proof reads as "this is forged" and makes the whole board look broken.
/// It still exits non-zero: there is no verified-but-not-anchored success path, by design.
fn not_anchored(lo: u32, hi: u32, detail: &str) -> ! {
    eprintln!("NOT A GENESIS-ANCHORED CHAIN PROOF");
    eprintln!();
    eprintln!("  The SNARK is VALID and was produced by guest {}.", &METHOD_ID_HEX[..8]);
    eprintln!("  It proves blocks {lo}..{hi} — a mid-chain SEGMENT, not a chain from genesis.");
    eprintln!("  {detail}");
    eprintln!();
    eprintln!("  This tool accepts only genesis-anchored proofs, because only those establish that");
    eprintln!("  the chain is valid FROM THE START. A segment proof is sound but says nothing about");
    eprintln!("  the blocks below it, so accepting one would reduce the claim to \"someone proved a");
    eprintln!("  thousand blocks somewhere\".");
    eprintln!();
    eprintln!("  Segment proofs are what the proof party produces; they are folded together into a");
    eprintln!("  single genesis-anchored proof, and THAT is what this verifies.");
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let json = args.iter().any(|a| a == "--json");
    let path = match args.iter().find(|a| !a.starts_with("--")) {
        Some(p) => p.clone(),
        None => {
            eprintln!("usage: hazync-verify [--json] <proof.snark>");
            eprintln!();
            eprintln!("Verifies a genesis-anchored Hazync range proof. Needs no node, no peers, no");
            eprintln!("chain data — just the file. Guest image id {}", &METHOD_ID_HEX[..8]);
            eprintln!();
            eprintln!("  --json   emit the chain state a node can ADOPT from this proof, as JSON.");
            eprintln!("           Everything a node needs to resume at height+1 without validating");
            eprintln!("           anything below it: tip, cumulative work, UTXO commitment, and the");
            eprintln!("           difficulty/median-time context. See prover/node-sync-demo.sh.");
            std::process::exit(2);
        }
    };

    let bytes = std::fs::read(&path).unwrap_or_else(|e| die(&format!("cannot read {path}: {e}")));

    let v = match verify(&bytes) {
        Ok(v) => v,
        Err(VerifyError::Invalid(m)) => die(&m),
        Err(VerifyError::NotAnchored { lo, hi, detail }) => not_anchored(lo, hi, &detail),
    };

    if json {
        // The state a node can ADOPT. Everything here is committed by the proof, so a node that
        // verifies the proof can start at height+1 without downloading or validating anything below
        // it. Block hashes are emitted in DISPLAY order (byte-reversed), which is what
        // `bitcoin-cli getblockhash` returns, so the two can be compared directly.
        println!("{}", v.to_json());
        return;
    }

    println!(
        ">>> SNARK RANGE PROOF [1..{}] VERIFIED — genesis-anchored, {} bytes.",
        v.height, v.proof_bytes
    );
    println!(
        "  out_tip_hash {}  range_work {}  total_cum_work {}  UTXO leaves {}",
        v.tip_hash_internal, v.range_work, v.cumulative_work, v.utxo_leaves
    );
    println!("  guest image id {METHOD_ID_HEX}");
}

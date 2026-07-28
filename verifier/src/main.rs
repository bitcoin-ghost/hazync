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

use serde::{Deserialize, Serialize};

/// Canonical guest image id (v0.10.0). Embedded rather than imported from the `methods` crate, which
/// would drag in the guest build. `scripts/check-versions.sh` fails the build if this drifts from
/// `reproduce/METHOD_ID`, which is the source of truth.
const METHOD_ID_HEX: &str = "3f52baff7e7d4adaa328b832d6f15fffb1b35968b6636760f9d50e045bbae67e";

const KIND_RANGE: u32 = 0xC4A1_0006;
const GENESIS_HASH: &str = "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f";
const GENESIS_TIME: u32 = 1_231_006_505;
const GENESIS_BITS: u32 = 0x1d00_ffff;
const GENESIS_WORK: u128 = 4_295_032_833; // GetBlockProof(0x1d00ffff): cumulative work through block 0

/// Mirror of the guest's `RangeState`. Field order is load-bearing — the journal decodes positionally,
/// so a reordering here silently misinterprets a valid proof rather than failing loudly.
#[derive(Serialize, Deserialize)]
struct RangeState {
    kind: u32,
    lo: u32,
    hi: u32,
    in_tip_hash: [u8; 32],
    in_roots: Vec<Option<[u8; 32]>>,
    in_leaves: u64,
    in_nbits: u32,
    in_time: u32,
    in_epoch_start: u32,
    in_recent: Vec<u32>,
    out_tip_hash: [u8; 32],
    out_roots: Vec<Option<[u8; 32]>>,
    out_leaves: u64,
    out_nbits: u32,
    out_time: u32,
    out_epoch_start: u32,
    out_recent: Vec<u32>,
    range_work: [u8; 32],
    self_id: [u32; 8],
}

fn die(msg: &str) -> ! {
    eprintln!("VERIFICATION FAILED: {msg}");
    std::process::exit(1);
}

fn hex_to_bytes(s: &str) -> Vec<u8> {
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap_or_else(|_| die("bad hex constant")))
        .collect()
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

/// Trailing empty root slots are not significant — an accumulator with 0 leaves may serialise with or
/// without them, so compare in normalised form.
fn normalize(mut v: Vec<Option<[u8; 32]>>) -> Vec<Option<[u8; 32]>> {
    while v.last() == Some(&None) {
        v.pop();
    }
    v
}

fn u128_be(b: &[u8; 32]) -> u128 {
    // range_work is a 256-bit little-endian counter; real chain work fits comfortably in the low 128.
    let mut acc: u128 = 0;
    for i in (0..16).rev() {
        acc = (acc << 8) | b[i] as u128;
    }
    acc
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
    let receipt: risc0_zkvm::Receipt =
        bincode::deserialize(&bytes).unwrap_or_else(|e| die(&format!("not a receipt: {e}")));

    let image_id = risc0_zkvm::sha::Digest::try_from(hex_to_bytes(METHOD_ID_HEX).as_slice())
        .unwrap_or_else(|_| die("bad embedded METHOD_ID"));

    // 1. the proof itself
    if let Err(e) = receipt.verify(image_id) {
        die(&format!(
            "the proof is not valid for guest {} — forged, tampered, corrupt, or produced by a \
             different guest build.\n  underlying: {e}",
            &METHOD_ID_HEX[..8]
        ));
    }

    let rs: RangeState = receipt
        .journal
        .decode()
        .unwrap_or_else(|e| die(&format!("journal is not a RangeState: {e}")));

    // 2. recursion pinned to this guest (S1)
    let claimed = hex(&rs.self_id.iter().flat_map(|w| w.to_le_bytes()).collect::<Vec<u8>>());
    if claimed != METHOD_ID_HEX {
        die(&format!("journal self_id {} != guest image id {}", &claimed[..16], &METHOD_ID_HEX[..16]));
    }
    // 3. domain tag (H8)
    if rs.kind != KIND_RANGE {
        die("receipt is not a RangeState (wrong domain tag)");
    }
    // 4 + 5. genesis anchoring — the assertion the whole artifact is built around
    if rs.lo != 1 {
        die(&format!("range starts at block {}, not 1 — NOT genesis-anchored", rs.lo));
    }
    let genesis_le: Vec<u8> = hex_to_bytes(GENESIS_HASH).into_iter().rev().collect();
    if rs.in_tip_hash.as_slice() != genesis_le.as_slice() {
        die("in-boundary tip is not the genesis block hash");
    }
    if rs.in_leaves != 0 {
        die("in-boundary UTXO set is not empty — the range does not start from nothing");
    }
    if !normalize(rs.in_roots.clone()).is_empty() {
        die("in-boundary UTXO roots are not empty");
    }
    if rs.in_nbits != GENESIS_BITS {
        die("in-boundary nBits != genesis");
    }
    if rs.in_epoch_start != GENESIS_TIME {
        die("in-boundary epoch start != genesis time");
    }
    if rs.in_time != GENESIS_TIME {
        die("in-boundary prev-time != genesis time");
    }
    if rs.in_recent != vec![GENESIS_TIME] {
        die("in-boundary recent-times != [genesis time]");
    }

    let total = GENESIS_WORK + u128_be(&rs.range_work);

    if json {
        // The state a node can ADOPT. Everything here is committed by the proof, so a node that
        // verifies the proof can start at height+1 without downloading or validating anything below
        // it — it needs the UTXO commitment to check spends, and the difficulty/median-time context
        // to check the next header. Block hashes are emitted in DISPLAY order (byte-reversed), which
        // is what `bitcoin-cli getblockhash` returns, so the two can be compared directly.
        let disp: String = hex(&rs.out_tip_hash.iter().rev().copied().collect::<Vec<u8>>());
        let roots: Vec<String> = rs
            .out_roots
            .iter()
            .filter_map(|r| r.as_ref().map(|h| hex(h)))
            .collect();
        println!("{{");
        println!("  \"verified\": true,");
        println!("  \"guest_image_id\": \"{METHOD_ID_HEX}\",");
        println!("  \"genesis_anchored\": true,");
        println!("  \"height\": {},", rs.hi);
        println!("  \"tip_hash\": \"{disp}\",");
        println!("  \"cumulative_work\": {total},");
        println!("  \"utxo_leaves\": {},", rs.out_leaves);
        println!("  \"utxo_roots\": [{}],", roots.iter().map(|r| format!("\"{r}\"")).collect::<Vec<_>>().join(", "));
        println!("  \"next_bits\": {},", rs.out_nbits);
        println!("  \"epoch_start_time\": {},", rs.out_epoch_start);
        println!("  \"recent_times\": [{}],", rs.out_recent.iter().map(|t| t.to_string()).collect::<Vec<_>>().join(", "));
        println!("  \"proof_bytes\": {},", bytes.len());
        println!("  \"blocks_not_validated\": {}", rs.hi);
        println!("}}");
        return;
    }

    println!(
        ">>> SNARK RANGE PROOF [1..{}] VERIFIED — genesis-anchored, {} bytes.",
        rs.hi,
        bytes.len()
    );
    println!(
        "  out_tip_hash {}  range_work {}  total_cum_work {}  UTXO leaves {}",
        hex(&rs.out_tip_hash),
        u128_be(&rs.range_work),
        total,
        rs.out_leaves
    );
    println!("  guest image id {}", METHOD_ID_HEX);
}

//! Measure the accumulator work the BRIDGE actually does, old structure vs cached internals.
//!
//! Deliberately not a microbenchmark of `prove`. A microbenchmark is what produced the 2,366x figure
//! for the coin-position index that turned out to be worth nothing on real blocks (GOALS.md §G2,
//! "Ruled out") — because it measured something that was 4% of block time. So this replicates the
//! bridge's inner loop from `prover/host/src/main.rs`:
//!
//!     roots()                                  once for root_prev
//!     per input:  prove(pos), prove(last), delete(pos)
//!     per output: add(leaf)
//!     roots()                                  once for root_next
//!
//! at a realistic forest size and input count.
//!
//! WHAT THIS DOES NOT MEASURE: block deserialisation, script extraction, the linear coin-position
//! scan, RPC. So the block-level speedup is necessarily LOWER than the ratio printed here. Treat this
//! as an upper bound on the win, to be confirmed by an A/B on real blocks.

use hazync_utreexo::{hash_leaf, parent, Forest, Hash, Proof};

// ---- the pre-cache implementations, verbatim, for comparison ------------------------------------
struct Naive {
    leaves: Vec<Hash>,
}

impl Naive {
    fn trees(&self) -> Vec<(usize, usize)> {
        let n = self.leaves.len();
        let mut out = Vec::new();
        let mut offset = 0usize;
        for h in (0..usize::BITS as usize).rev() {
            if (n >> h) & 1 == 1 {
                out.push((offset, h));
                offset += 1 << h;
            }
        }
        out
    }
    fn subtree_root(&self, offset: usize, height: usize) -> Hash {
        let mut level: Vec<Hash> = self.leaves[offset..offset + (1 << height)].to_vec();
        while level.len() > 1 {
            level = level.chunks(2).map(|c| parent(&c[0], &c[1])).collect();
        }
        level[0]
    }
    fn roots(&self) -> Vec<Option<Hash>> {
        let mut roots =
            vec![None; (self.leaves.len().max(1)).next_power_of_two().trailing_zeros() as usize + 1];
        for (offset, height) in self.trees() {
            if height >= roots.len() {
                roots.resize(height + 1, None);
            }
            roots[height] = Some(self.subtree_root(offset, height));
        }
        roots
    }
    fn prove(&self, index: usize) -> Proof {
        let (offset, height) = self
            .trees()
            .into_iter()
            .find(|&(off, h)| index >= off && index < off + (1 << h))
            .expect("index out of range");
        let local = index - offset;
        let mut level: Vec<Hash> = self.leaves[offset..offset + (1 << height)].to_vec();
        let mut pos = local;
        let mut siblings = Vec::with_capacity(height);
        while level.len() > 1 {
            let sib = if pos & 1 == 0 { level[pos + 1] } else { level[pos - 1] };
            siblings.push(sib);
            level = level.chunks(2).map(|c| parent(&c[0], &c[1])).collect();
            pos >>= 1;
        }
        Proof { leaf: self.leaves[index], position: local as u64, siblings }
    }
    fn delete(&mut self, i: usize) {
        let last = self.leaves.len() - 1;
        self.leaves.swap(i, last);
        self.leaves.pop();
    }
    fn add(&mut self, l: Hash) {
        self.leaves.push(l);
    }
}

fn lf(i: u64) -> Hash {
    hash_leaf(&i.to_le_bytes())
}

fn mix(s: &mut u64) -> u64 {
    *s = s.wrapping_add(0x9E3779B97F4A7C15);
    let mut z = *s;
    z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
    z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
    z ^ (z >> 31)
}

fn main() {
    // ~1.6M leaves is the live UTXO-set size the bridge is carrying around h=182,000 (GOALS.md §G2).
    // Input counts are the real per-era figures: ~600 at h=250k, rising to ~4,200 at tip.
    let leaves: usize = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(1_600_000);
    let inputs: usize = std::env::args().nth(2).and_then(|s| s.parse().ok()).unwrap_or(600);

    println!("forest {leaves} leaves, {inputs} inputs/block (one block's accumulator work)\n");

    let base: Vec<Hash> = (0..leaves as u64).map(lf).collect();

    // ---- cached internals ----
    let t = std::time::Instant::now();
    let mut f = Forest::new();
    for l in &base {
        f.add(*l);
    }
    let build_new = t.elapsed();

    let mut st = 0x1234_5678u64;
    let t = std::time::Instant::now();
    let mut sink = 0u8;
    let _ = f.roots();
    for _ in 0..inputs {
        let n = f.leaves.len();
        let pos = (mix(&mut st) as usize) % n;
        sink ^= f.prove(pos).siblings.last().map_or(0, |s| s[0]);
        sink ^= f.prove(n - 1).siblings.last().map_or(0, |s| s[0]);
        f.delete(pos);
    }
    for i in 0..inputs {
        f.add(lf(9_000_000 + i as u64));
    }
    let _ = f.roots();
    let new = t.elapsed();

    // ---- leaves only ----
    let t = std::time::Instant::now();
    let mut nv = Naive { leaves: base.clone() };
    let build_old = t.elapsed();

    let mut st = 0x1234_5678u64;
    let t = std::time::Instant::now();
    let _ = nv.roots();
    for _ in 0..inputs {
        let n = nv.leaves.len();
        let pos = (mix(&mut st) as usize) % n;
        sink ^= nv.prove(pos).siblings.last().map_or(0, |s| s[0]);
        sink ^= nv.prove(n - 1).siblings.last().map_or(0, |s| s[0]);
        nv.delete(pos);
    }
    for i in 0..inputs {
        nv.add(lf(9_000_000 + i as u64));
    }
    let _ = nv.roots();
    let old = t.elapsed();

    println!("  build (once, at startup)");
    println!("    leaves only      {:>10.2?}", build_old);
    println!("    cached internals {:>10.2?}   <- one extra hash per leaf, paid once", build_new);
    println!("\n  per block (roots + {inputs} x [2 proofs + delete] + {inputs} adds + roots)");
    println!("    leaves only      {:>10.2?}", old);
    println!("    cached internals {:>10.2?}", new);
    println!("\n  {:.0}x faster on the accumulator portion", old.as_secs_f64() / new.as_secs_f64());
    println!("  (block-level speedup will be LOWER — this excludes deserialisation, the coin-position");
    println!("   scan and RPC. Confirm with an A/B on real blocks.)");
    if sink == 42 {
        println!();
    }
}

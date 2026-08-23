// Copyright 2025 RISC Zero, Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! Run the zkVM guest and prove its results.

pub(crate) mod dev_mode;
pub(crate) mod keccak;
mod prover_impl;
#[cfg(test)]
mod tests;
pub(crate) mod union_peak;

use std::rc::Rc;

use anyhow::{anyhow, bail, ensure, Result};
use risc0_core::field::baby_bear::{BabyBear, Elem, ExtElem};
use risc0_groth16::prove::shrink_wrap;
use risc0_zkp::hal::{CircuitHal, Hal};

use self::{dev_mode::DevModeProver, prover_impl::ProverImpl};
use crate::{
    claim::{receipt::UnionClaim, Unknown},
    host::prove_info::ProveInfo,
    receipt::{
        CompositeReceipt, Groth16Receipt, Groth16ReceiptVerifierParameters, InnerAssumptionReceipt,
        InnerReceipt, SegmentReceipt, SuccinctReceipt,
    },
    sha::Digestible,
    ExecutorEnv, PreflightResults, ProverOpts, Receipt, ReceiptClaim, ReceiptKind, Segment,
    Session, VerifierContext, WorkClaim,
};

mod private {
    use super::{dev_mode::DevModeProver, prover_impl::ProverImpl};

    pub trait Sealed {}
    impl Sealed for ProverImpl {}
    impl Sealed for DevModeProver {}
}

/// A ProverServer can execute a given ELF binary and produce a [ProveInfo] which contains a
/// [Receipt][crate::Receipt] that can be used to verify correct computation.
pub trait ProverServer: private::Sealed {
    /// Prove the specified ELF binary.
    fn prove(&self, env: ExecutorEnv<'_>, elf: &[u8]) -> Result<ProveInfo> {
        self.prove_with_ctx(env, &VerifierContext::default(), elf)
    }

    /// Prove the specified ELF binary using the specified [VerifierContext].
    fn prove_with_ctx(
        &self,
        env: ExecutorEnv<'_>,
        ctx: &VerifierContext,
        elf: &[u8],
    ) -> Result<ProveInfo>;

    /// Prove the specified [Session].
    fn prove_session(&self, ctx: &VerifierContext, session: &Session) -> Result<ProveInfo>;

    /// Prove the specified [Segment].
    fn prove_segment(&self, ctx: &VerifierContext, segment: &Segment) -> Result<SegmentReceipt> {
        tracing::debug!("prove_segment");
        let results = self.segment_preflight(segment)?;
        self.prove_segment_core(ctx, results)
    }

    /// Run preflight on the specified [Segment].
    fn segment_preflight(&self, segment: &Segment) -> Result<PreflightResults>;

    /// Prove the specified [Segment] which has had preflight run on it.
    fn prove_segment_core(
        &self,
        ctx: &VerifierContext,
        preflight_results: PreflightResults,
    ) -> Result<SegmentReceipt>;

    /// Prove the specified keccak request
    fn prove_keccak(&self, request: &crate::ProveKeccakRequest)
        -> Result<SuccinctReceipt<Unknown>>;

    /// Lift a [SegmentReceipt] into a [SuccinctReceipt]
    fn lift(&self, receipt: &SegmentReceipt) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Join two [SuccinctReceipt] into a [SuccinctReceipt]
    fn join(
        &self,
        a: &SuccinctReceipt<ReceiptClaim>,
        b: &SuccinctReceipt<ReceiptClaim>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Unite two [SuccinctReceipt] into a [SuccinctReceipt]
    fn union(
        &self,
        a: &SuccinctReceipt<Unknown>,
        b: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<UnionClaim>>;

    /// Resolve an assumption from a conditional [SuccinctReceipt] by providing a [SuccinctReceipt]
    /// proving the validity of the assumption.
    fn resolve(
        &self,
        conditional: &SuccinctReceipt<ReceiptClaim>,
        assumption: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Lift a [SegmentReceipt] into a [SuccinctReceipt] with a proof of verifiable work (PoVW) claim.
    fn lift_povw(
        &self,
        receipt: &SegmentReceipt,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>>;

    /// Join two [SuccinctReceipt] with proof of verifiable work (PoVW) claims into a
    /// [SuccinctReceipt] a PoVW claim.
    fn join_povw(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        b: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>>;

    /// Join two [SuccinctReceipt] with proof of verifiable work (PoVW) claims into a
    /// [SuccinctReceipt], and unwrap the result (see [ProverServer::unwrap_povw]).
    fn join_unwrap_povw(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        b: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Resolve an assumption from a conditional [SuccinctReceipt] with a proof of verifiable work
    /// (PoVW) claim by providing a [SuccinctReceipt] proving the validity of the assumption.
    fn resolve_povw(
        &self,
        conditional: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        assumption: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>>;

    /// Resolve an assumption from a conditional [SuccinctReceipt] with a proof of verifiable work
    /// (PoVW) claim by providing a [SuccinctReceipt] proving the validity of the assumption, and
    /// unwrap the result (see [ProverServer::unwrap_povw]).
    fn resolve_unwrap_povw(
        &self,
        conditional: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        assumption: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Remove the proof of verifiable work (PoVW) information from a [SuccinctReceipt] to produce
    /// a [SuccinctReceipt] over the underlying claim.
    fn unwrap_povw(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Convert a [SuccinctReceipt] with a Poseidon hash function that uses a 254-bit field
    fn identity_p254(
        &self,
        a: &SuccinctReceipt<ReceiptClaim>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>>;

    /// Compress a [CompositeReceipt] into a single [SuccinctReceipt].
    ///
    /// A [CompositeReceipt] may contain an arbitrary number of receipts assembled into
    /// segments and assumptions. Together, these receipts collectively prove a top-level
    /// [ReceiptClaim](crate::ReceiptClaim). This function compresses all of the constituent receipts of a
    /// [CompositeReceipt] into a single [SuccinctReceipt] that proves the same top-level claim. It
    /// accomplishes this by iterative application of the recursion programs including lift, join,
    /// and resolve.
    fn composite_to_succinct(
        &self,
        receipt: &CompositeReceipt,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        <Self as Compress<_>>::composite_to_succinct(self, receipt)
    }

    /// SEGMENT DISTRIBUTION (hazync patch). Assemble a `Receipt` from segment receipts that were
    /// proved somewhere else.
    ///
    /// `prove_session` proves its segments and then assembles; this is that second half on its own,
    /// so a caller that obtained the receipts from other machines runs the SAME assembly rather than
    /// a copy of it. Receipts must be in session order -- the journal and assumptions are merged into
    /// the last one's claim and the composite verify walks the chain.
    ///
    /// Defaults to refusing: only the local prover can assemble, and a prover that cannot should say
    /// so rather than silently return something that is not a proof of this session.
    fn assemble_from_segment_receipts(
        &self,
        _ctx: &VerifierContext,
        _session: &Session,
        _segments: Vec<SegmentReceipt>,
    ) -> Result<ProveInfo> {
        anyhow::bail!("this prover does not support assembling externally proved segments")
    }

    /// Convert a composite receipt to a succinct work claim receipt.
    fn composite_to_succinct_povw(
        &self,
        receipt: &CompositeReceipt,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        <Self as Compress<_>>::composite_to_succinct(self, receipt)
    }

    /// Compress a [SuccinctReceipt] into a [Groth16Receipt].
    fn succinct_to_groth16(
        &self,
        receipt: &SuccinctReceipt<ReceiptClaim>,
    ) -> Result<Groth16Receipt<ReceiptClaim>> {
        let ident_receipt = self.identity_p254(receipt).unwrap();
        let seal_bytes = ident_receipt.get_seal_bytes();
        let seal = shrink_wrap(&seal_bytes)?.to_vec();
        Ok(Groth16Receipt {
            seal,
            claim: receipt.claim.clone(),
            verifier_parameters: Groth16ReceiptVerifierParameters::default().digest(),
        })
    }

    /// Compress a receipt into one with a smaller representation.
    ///
    /// The requested target representation is determined by the [ReceiptKind] specified on the
    /// provided [ProverOpts]. If the receipt is already at least as compressed as the requested
    /// kind, this is a no-op.
    fn compress(&self, opts: &ProverOpts, receipt: &Receipt) -> Result<Receipt> {
        match &receipt.inner {
            InnerReceipt::Composite(inner) => match opts.receipt_kind {
                ReceiptKind::Composite => Ok(receipt.clone()),
                ReceiptKind::Succinct => {
                    let succinct_receipt = self.composite_to_succinct(inner)?;
                    Ok(Receipt::new(
                        InnerReceipt::Succinct(succinct_receipt),
                        receipt.journal.bytes.clone(),
                    ))
                }
                ReceiptKind::Groth16 => {
                    let succinct_receipt = self.composite_to_succinct(inner)?;
                    let groth16_receipt = self.succinct_to_groth16(&succinct_receipt)?;
                    Ok(Receipt::new(
                        InnerReceipt::Groth16(groth16_receipt),
                        receipt.journal.bytes.clone(),
                    ))
                }
            },
            InnerReceipt::Succinct(inner) => match opts.receipt_kind {
                ReceiptKind::Composite | ReceiptKind::Succinct => Ok(receipt.clone()),
                ReceiptKind::Groth16 => {
                    let groth16_receipt = self.succinct_to_groth16(inner)?;
                    Ok(Receipt::new(
                        InnerReceipt::Groth16(groth16_receipt),
                        receipt.journal.bytes.clone(),
                    ))
                }
            },
            InnerReceipt::Groth16(_) => match opts.receipt_kind {
                ReceiptKind::Composite | ReceiptKind::Succinct | ReceiptKind::Groth16 => {
                    Ok(receipt.clone())
                }
            },
            InnerReceipt::Fake(_) => {
                ensure!(
                    opts.dev_mode(),
                    "dev mode must be enabled to compress fake receipts"
                );
                Ok(receipt.clone())
            }
        }
    }
}

trait Lift<Claim> {
    fn lift(&self, segment_receipt: &SegmentReceipt) -> anyhow::Result<SuccinctReceipt<Claim>>;
}

impl<P: ProverServer + ?Sized> Lift<ReceiptClaim> for P {
    fn lift(
        &self,
        segment_receipt: &SegmentReceipt,
    ) -> anyhow::Result<SuccinctReceipt<ReceiptClaim>> {
        <Self as ProverServer>::lift(self, segment_receipt)
    }
}

impl<P: ProverServer + ?Sized> Lift<WorkClaim<ReceiptClaim>> for P {
    fn lift(
        &self,
        segment_receipt: &SegmentReceipt,
    ) -> anyhow::Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        self.lift_povw(segment_receipt)
    }
}

trait Join<Claim> {
    fn join(
        &self,
        a: &SuccinctReceipt<Claim>,
        b: &SuccinctReceipt<Claim>,
    ) -> anyhow::Result<SuccinctReceipt<Claim>>;
}

impl<P: ProverServer + ?Sized> Join<ReceiptClaim> for P {
    fn join(
        &self,
        a: &SuccinctReceipt<ReceiptClaim>,
        b: &SuccinctReceipt<ReceiptClaim>,
    ) -> anyhow::Result<SuccinctReceipt<ReceiptClaim>> {
        <Self as ProverServer>::join(self, a, b)
    }
}

impl<P: ProverServer + ?Sized> Join<WorkClaim<ReceiptClaim>> for P {
    fn join(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        b: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> anyhow::Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        self.join_povw(a, b)
    }
}

trait Resolve<Claim> {
    fn resolve(
        &self,
        cond: &SuccinctReceipt<Claim>,
        assum: &SuccinctReceipt<Unknown>,
    ) -> anyhow::Result<SuccinctReceipt<Claim>>;
}

impl<P: ProverServer + ?Sized> Resolve<ReceiptClaim> for P {
    fn resolve(
        &self,
        cond: &SuccinctReceipt<ReceiptClaim>,
        assum: &SuccinctReceipt<Unknown>,
    ) -> anyhow::Result<SuccinctReceipt<ReceiptClaim>> {
        <Self as ProverServer>::resolve(self, cond, assum)
    }
}

impl<P: ProverServer + ?Sized> Resolve<WorkClaim<ReceiptClaim>> for P {
    fn resolve(
        &self,
        cond: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        assum: &SuccinctReceipt<Unknown>,
    ) -> anyhow::Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        self.resolve_povw(cond, assum)
    }
}

trait Compress<Claim> {
    fn composite_to_succinct(
        &self,
        composite_receipt: &CompositeReceipt,
    ) -> anyhow::Result<SuccinctReceipt<Claim>>;
}

impl<P, Claim> Compress<Claim> for P
where
    P: Lift<Claim>
        + Join<Claim>
        + Resolve<Claim>
        + Lift<ReceiptClaim>
        + Join<ReceiptClaim>
        + Resolve<ReceiptClaim>
        + ?Sized,
{
    fn composite_to_succinct(
        &self,
        composite_receipt: &CompositeReceipt,
    ) -> anyhow::Result<SuccinctReceipt<Claim>> {
        // JOIN TREE (hazync patch). The fold this replaces was strictly LINEAR:
        //
        //     try_fold(None, |left, right| match left {
        //         Some(left) => self.join(&left, &self.lift(right)?)?,
        //         None       => self.lift(right)?,
        //     })
        //
        // lift segment 1, join into the accumulator, lift segment 2, join, and so on. Every join
        // depends on the one before it, so the depth is N and there is no parallelism available in
        // it at all -- not across threads, not across machines. That is what caps a distributed
        // prover: segment proving divides, and then assembly does not. Measured on a 44-segment
        // chunk, assembly was 43% of the total (44 lifts x 13.8 s + 43 joins x 14.0 s = 1209 s
        // predicted against 1300 s observed), which put a ~2.4x ceiling on the whole thing.
        //
        // A balanced tree does the SAME N-1 joins and produces the SAME claim -- join(a, b) asserts
        // a.post == b.pre, and joining adjacent pairs, then adjacent pairs of pairs, preserves that
        // adjacency, so the operation is associative over the ordered sequence. What changes is the
        // DEPTH: log2(N) instead of N. At 44 segments that is 6 levels rather than 43 steps.
        //
        // THIS CHANGE COSTS AND SAVES NOTHING ON ITS OWN. It is still sequential: the same N lifts
        // and N-1 joins in the same process. What it changes is that the work becomes
        // PARALLELISABLE AT ALL -- every join at a given level is now independent of its siblings,
        // where before each one depended on the accumulator. The win comes from threading these
        // loops or handing levels to workers; this is the enabling step, not the winning one.
        //
        // Deliberately NOT threaded here. The CUDA prover's thread-safety through lift/join is
        // unestablished, and a deadlock in exactly this area (hazync#147) cost five hours of card
        // time to find. Concurrency goes in behind a measurement, not ahead of one.
        let mut level: Vec<SuccinctReceipt<Claim>> = Vec::with_capacity(composite_receipt.segments.len());
        for seg in composite_receipt.segments.iter() {
            level.push(self.lift(seg)?);
        }
        if level.is_empty() {
            return Err(anyhow!("malformed composite receipt has no continuation segment receipts"));
        }
        while level.len() > 1 {
            let mut next: Vec<SuccinctReceipt<Claim>> = Vec::with_capacity(level.len().div_ceil(2));
            let mut it = level.into_iter();
            loop {
                match (it.next(), it.next()) {
                    (Some(a), Some(b)) => next.push(self.join(&a, &b)?),
                    // Odd one out carries to the next level unchanged. It stays in position, so
                    // adjacency is preserved and the final claim is unaffected.
                    (Some(a), None) => next.push(a),
                    _ => break,
                }
            }
            level = next;
        }
        let continuation_receipt = level.pop().expect("checked non-empty above");

        // Compress assumptions and resolve them to get the final succinct receipt.
        composite_receipt.assumption_receipts.iter().try_fold(
            continuation_receipt,
            |conditional: SuccinctReceipt<Claim>, assumption: &InnerAssumptionReceipt| match assumption {
                InnerAssumptionReceipt::Succinct(assumption) => self.resolve(&conditional, assumption),
                InnerAssumptionReceipt::Composite(assumption) => {
                    self.resolve(&conditional, &SuccinctReceipt::<ReceiptClaim>::into_unknown(self.composite_to_succinct(assumption)?))
                }
                InnerAssumptionReceipt::Fake(_) => bail!(
                    "compressing composite receipts with fake receipt assumptions is not supported"
                ),
                InnerAssumptionReceipt::Groth16(_) => bail!(
                    "compressing composite receipts with Groth16 receipt assumptions is not supported"
                )
            },
        )
    }
}

/// A pair of [Hal] and [CircuitHal].
#[derive(Clone)]
pub struct HalPair<H, C>
where
    H: Hal<Field = BabyBear, Elem = Elem, ExtElem = ExtElem>,
    C: CircuitHal<H>,
{
    /// A [Hal] implementation.
    pub hal: Rc<H>,

    /// An [CircuitHal] implementation.
    pub circuit_hal: Rc<C>,
}

impl Session {
    /// For each segment, call [ProverServer::prove_session] and collect the
    /// receipts.
    pub fn prove(&self) -> Result<ProveInfo> {
        let prover = get_prover_server(&ProverOpts::default())?;
        prover.prove_session(&VerifierContext::default(), self)
    }
}

/// Select a [ProverServer] based on the specified [ProverOpts].
pub fn get_prover_server(opts: &ProverOpts) -> Result<Rc<dyn ProverServer>> {
    if opts.dev_mode() {
        eprintln!("WARNING: proving in dev mode. This will not generate valid, secure proofs.");
        return Ok(Rc::new(DevModeProver::new()));
    }

    Ok(Rc::new(ProverImpl::new(opts.clone())))
}

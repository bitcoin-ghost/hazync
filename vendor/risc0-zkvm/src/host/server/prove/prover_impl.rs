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

use std::collections::HashMap;

use anyhow::{anyhow, bail, ensure, Context, Result};

use super::{keccak::prove_keccak, ProverServer};
use crate::{
    claim::merge::Merge,
    host::{
        client::prove::opts::ReceiptKind,
        prove_info::ProveInfo,
        recursion::{identity_p254, join, lift, resolve},
        server::{exec::executor::ExecutorImpl, prove::union_peak::UnionPeak},
    },
    mmr::MerkleMountainAccumulator,
    receipt::{InnerReceipt, SegmentReceipt, SuccinctReceipt},
    recursion::prove::{
        join_povw, join_unwrap_povw, lift_povw, resolve_povw, resolve_unwrap_povw, union,
        unwrap_povw,
    },
    sha::Digestible,
    Assumption, AssumptionReceipt, CompositeReceipt, ExecutorEnv, InnerAssumptionReceipt,
    MaybePruned, Output, PreflightResults, ProverOpts, Receipt, ReceiptClaim, Segment, Session,
    UnionClaim, Unknown, VerifierContext, WorkClaim,
};

/// An implementation of a Prover that runs locally.
pub struct ProverImpl {
    opts: ProverOpts,
}

impl ProverImpl {
    /// Construct a [ProverImpl].
    pub fn new(opts: ProverOpts) -> Self {
        Self { opts }
    }
}

impl ProverServer for ProverImpl {
    /// Turn finished segment receipts into a `Receipt`, exactly as `prove_session` does.
    ///
    /// `prove_session` calls this immediately after proving its segments, so the monolithic and the
    /// distributed paths share this code rather than having two copies that can diverge. A caller
    /// that proved the segments elsewhere -- on other machines, in any order -- passes them here in
    /// SESSION ORDER and gets the same receipt.
    ///
    /// The receipts must be in session order: the journal and assumptions are merged into the last
    /// one's claim, and the composite verify checks the chain of segment claims.
    /// SEGMENT DISTRIBUTION step 3 (hazync patch). Merge the session output into the last segment
    /// receipt and lift every segment, returning the lifted receipts in session order.
    ///
    /// This is the front half of assembly, split out so the JOIN TREE over the result can be run
    /// somewhere else -- across threads, or across machines, one join per work item. The joins at a
    /// given level are independent of each other, which is what the balanced tree in
    /// `composite_to_succinct` exists to make true.
    ///
    /// The merge has to happen here rather than in a worker: it folds the session journal digest and
    /// the assumption set into the LAST segment's claim, and a worker has neither. Everything after
    /// this point needs only the receipts.
    fn prepare_lifts(
        &self,
        _ctx: &VerifierContext,
        session: &Session,
        mut segments: Vec<SegmentReceipt>,
    ) -> Result<Vec<SuccinctReceipt<ReceiptClaim>>> {
        let (assumptions, _): (Vec<_>, Vec<_>) = session.assumptions.iter().cloned().unzip();
        segments
            .last_mut()
            .ok_or_else(|| anyhow!("session is empty"))?
            .claim
            .output
            .merge_with(
                &session
                    .journal
                    .as_ref()
                    .map(|journal| Output {
                        journal: MaybePruned::Pruned(journal.digest()),
                        assumptions: assumptions.into(),
                    })
                    .into(),
            )
            .context("failed to merge output into final segment claim")?;

        let mut lifted = Vec::with_capacity(segments.len());
        for seg in segments.iter() {
            lifted.push(self.lift(seg)?);
        }
        Ok(lifted)
    }

    /// SEGMENT DISTRIBUTION step 3 (hazync patch). Finish assembly from a continuation receipt whose
    /// join tree was run elsewhere.
    ///
    /// Takes the single receipt left after joining, resolves the session's assumptions against it,
    /// and builds the `Receipt`. Pairs with `prepare_lifts`: together they are
    /// `assemble_from_segment_receipts` with the join tree lifted out of the middle.
    ///
    /// NOTE what is given up. `assemble_from_segment_receipts` builds a `CompositeReceipt` and runs
    /// `verify_integrity_with_context` and `check_claims` over it. There is no composite here, so
    /// those two checks are gone. What still holds: every receipt is verified as it arrives, `join`
    /// asserts `a.post == b.pre` at every level so a misplaced segment cannot survive the tree, and
    /// the returned `Receipt` is verified against the image id. That is weaker against a BUGGY
    /// PROVER, not against a dishonest worker.
    fn assemble_from_joined(
        &self,
        _ctx: &VerifierContext,
        session: &Session,
        joined: SuccinctReceipt<ReceiptClaim>,
    ) -> Result<ProveInfo> {
        let (_, session_assumption_receipts): (Vec<_>, Vec<_>) =
            session.assumptions.iter().cloned().unzip();

        let mut conditional = joined;
        for assumption_receipt in session_assumption_receipts {
            let inner = match assumption_receipt {
                AssumptionReceipt::Proven(receipt) => receipt,
                AssumptionReceipt::Unresolved(a) => bail!(
                    "assemble_from_joined cannot discharge an unresolved assumption: {a:#?}"
                ),
            };
            conditional = match inner {
                InnerAssumptionReceipt::Succinct(a) => self.resolve(&conditional, &a)?,
                InnerAssumptionReceipt::Composite(a) => {
                    let s = self.composite_to_succinct(&a)?;
                    self.resolve(&conditional, &SuccinctReceipt::<ReceiptClaim>::into_unknown(s))?
                }
                InnerAssumptionReceipt::Fake(_) => {
                    bail!("fake receipt assumptions are not supported here")
                }
                InnerAssumptionReceipt::Groth16(_) => {
                    bail!("Groth16 receipt assumptions are not supported here")
                }
            };
        }

        let receipt = Receipt::new(
            InnerReceipt::Succinct(conditional),
            session.journal.clone().unwrap_or_default().bytes,
        );
        Ok(ProveInfo {
            receipt,
            work_receipt: None,
            stats: session.stats(),
        })
    }

    fn assemble_from_segment_receipts(
        &self,
        ctx: &VerifierContext,
        session: &Session,
        mut segments: Vec<SegmentReceipt>,
    ) -> Result<ProveInfo> {
        let (assumptions, session_assumption_receipts): (Vec<_>, Vec<_>) =
            session.assumptions.iter().cloned().unzip();

        // Merge the output, including journal digest and assumptions, into the last segment.
        segments
            .last_mut()
            .ok_or_else(|| anyhow!("session is empty"))?
            .claim
            .output
            .merge_with(
                &session
                    .journal
                    .as_ref()
                    .map(|journal| Output {
                        journal: MaybePruned::Pruned(journal.digest()),
                        assumptions: assumptions.into(),
                    })
                    .into(),
            )
            .context("failed to merge output into final segment claim")?;

        let verifier_parameters = ctx
            .composite_verifier_parameters()
            .ok_or_else(|| anyhow!("composite receipt verifier parameters missing from context"))?
            .digest();

        let mut zkr_receipts = HashMap::new();
        let mut keccak_receipts: MerkleMountainAccumulator<UnionPeak> =
            MerkleMountainAccumulator::new();
        for proof_request in session.pending_keccaks.iter() {
            let receipt = prove_keccak(proof_request)?;
            tracing::debug!("adding keccak assumption: {}", receipt.claim.digest());
            keccak_receipts.insert(receipt)?;
        }

        // NOTE: Calling keccak_receipts.root() proves the union tree.
        if let Ok(root_receipt) = keccak_receipts.root() {
            let assumption = Assumption {
                claim: root_receipt.claim.digest(),
                control_root: root_receipt.control_root()?,
            };

            tracing::debug!("keccak root assumption: {:?}", assumption);
            zkr_receipts.insert(assumption, root_receipt.clone());
        }

        // TODO: add test case for when a single session refers to the same assumption multiple times
        let inner_assumption_receipts: Vec<_> = session_assumption_receipts
            .into_iter()
            .map(|assumption_receipt| match assumption_receipt {
                AssumptionReceipt::Proven(receipt) => Ok(receipt),
                AssumptionReceipt::Unresolved(assumption) => {
                    let receipt = zkr_receipts.get(&assumption).ok_or_else(|| {
                        anyhow!("no receipt available for unresolved assumption: {assumption:#?}")
                    })?;
                    Ok(InnerAssumptionReceipt::Succinct(receipt.clone()))
                }
            })
            .collect::<Result<_>>()?;

        let composite_receipt = CompositeReceipt {
            segments,
            assumption_receipts: inner_assumption_receipts,
            verifier_parameters,
        };

        let session_claim = session.claim()?;

        // Verify the receipt to catch if something is broken in the proving process.
        // NOTE: If the proof is very large, this could take > 1s, e.g. with 1000 segments.
        composite_receipt.verify_integrity_with_context(ctx)?;
        check_claims(
            &session_claim,
            "composite",
            MaybePruned::Value(composite_receipt.claim()?),
        )?;

        if self.opts.receipt_kind == ReceiptKind::Composite {
            let receipt = Receipt::new(
                InnerReceipt::Composite(composite_receipt),
                session.journal.clone().unwrap_or_default().bytes,
            );
            return Ok(ProveInfo {
                receipt,
                work_receipt: None,
                stats: session.stats(),
            });
        }

        let (succinct_receipt, work_receipt) = match session.povw_job_id.is_some() {
            true => {
                let work_receipt = self.composite_to_succinct_povw(&composite_receipt)?;
                let unwrapped = self.unwrap_povw(&work_receipt)?;
                (unwrapped, Some(work_receipt))
            }
            false => (self.composite_to_succinct(&composite_receipt)?, None),
        };

        if self.opts.receipt_kind == ReceiptKind::Succinct {
            let receipt = Receipt::new(
                InnerReceipt::Succinct(succinct_receipt),
                session.journal.clone().unwrap_or_default().bytes,
            );
            return Ok(ProveInfo {
                receipt,
                work_receipt: work_receipt.map(Into::into),
                stats: session.stats(),
            });
        }

        let groth16_receipt = self.succinct_to_groth16(&succinct_receipt)?;

        if self.opts.receipt_kind == ReceiptKind::Groth16 {
            let receipt = Receipt::new(
                InnerReceipt::Groth16(groth16_receipt),
                session.journal.clone().unwrap_or_default().bytes,
            );
            return Ok(ProveInfo {
                receipt,
                work_receipt: work_receipt.map(Into::into),
                stats: session.stats(),
            });
        }

        // As long as the checks above are exhaustive, this code is unreachable. If this statement
        // is reached, this is an implementation error.
        unreachable!(
            "proving not implemented for receipt kind {:?}",
            self.opts.receipt_kind
        );
    }

    fn prove(&self, env: ExecutorEnv<'_>, elf: &[u8]) -> Result<ProveInfo> {
        let ctx = VerifierContext::default().with_dev_mode(self.opts.dev_mode());
        self.prove_with_ctx(env, &ctx, elf)
    }

    fn prove_with_ctx(
        &self,
        env: ExecutorEnv<'_>,
        ctx: &VerifierContext,
        elf: &[u8],
    ) -> Result<ProveInfo> {
        let session = ExecutorImpl::from_elf(env, elf)?.run()?;
        self.prove_session(ctx, &session)
    }

    fn prove_session(&self, ctx: &VerifierContext, session: &Session) -> Result<ProveInfo> {
        tracing::debug!(
            "prove_session: exit_code = {:?}, journal = {:?}, segments: {}",
            session.exit_code,
            session.journal.as_ref().map(hex::encode),
            session.segments.len()
        );

        ensure!(
            self.opts.hashfn == "poseidon2",
            "provided `ProverOpts` has unsupported `hashfn` value of \"{}\"; \
            supported `hashfn` values are: \"poseidon2\".",
            &self.opts.hashfn
        );

        // PIPELINING (hazync patch). `prove_segment` is `segment_preflight` then `prove_segment_core`
        // in sequence, and this loop ran them back to back, so the GPU sat idle for every preflight.
        // Profiling a chunk prove on a B200 put ~35% of on-CPU time in preflight with the GPU waiting
        // on `cuStreamSynchronize`, and the share was the same at po2 20 and po2 22 despite 4.2x the
        // segment count — so the cost tracks cycles, not per-segment overhead. Overlapping the two
        // measured 1.39x on segment proving, replicated across two runs.
        //
        // Only the SCHEDULE changes. Segment n+1's witness does not depend on segment n's proof, the
        // seals are collected in the original order, and everything after this loop is untouched.
        //
        // Hooks force the sequential path: `on_pre_prove_segment` takes a `&Segment`, and a `Segment`
        // carries no `Send` bound, so it cannot cross the channel. Rather than resolve twice or fire
        // hooks out of order, a session with hooks keeps exactly its old behaviour.
        let mut segments = Vec::new();
        if session.hooks.is_empty() {
            // Borrow ONLY the segment list. Capturing `session` whole would drag in
            // `hooks: Vec<Box<dyn SessionEvents>>`, which is not Sync, and the worker has no use
            // for it — this branch is the one where there are no hooks.
            let segment_refs = &session.segments;
            let (tx, rx) = std::sync::mpsc::sync_channel::<Result<PreflightResults>>(1);
            std::thread::scope(|scope| -> Result<()> {
                // Depth 1 => at most two witnesses alive: one being proved, one buffered. Deeper buys
                // nothing (the GPU consumer is serial) and each one holds a full segment witness, so
                // the depth is a memory knob rather than a speed one.
                scope.spawn(move || {
                    for segment_ref in segment_refs.iter() {
                        let result = segment_ref
                            .resolve()
                            .and_then(|segment| self.segment_preflight(&segment));
                        let failed = result.is_err();
                        // A send error means the consumer stopped; so does a preflight failure, which
                        // the consumer surfaces. Either way there is nothing further to produce.
                        if tx.send(result).is_err() || failed {
                            break;
                        }
                    }
                });
                for index in 0..segment_refs.len() {
                    let preflight_results = rx.recv().map_err(|_| {
                        anyhow!("preflight worker stopped after {index}/{} segments", segment_refs.len())
                    })??;
                    segments.push(self.prove_segment_core(ctx, preflight_results)?);
                }
                Ok(())
            })?;
        } else {
            for segment_ref in session.segments.iter() {
                let segment = segment_ref.resolve()?;
                for hook in &session.hooks {
                    hook.on_pre_prove_segment(&segment);
                }
                segments.push(self.prove_segment(ctx, &segment)?);
                for hook in &session.hooks {
                    hook.on_post_prove_segment(&segment);
                }
            }
        }

        // SEGMENT DISTRIBUTION (hazync patch). Everything from here on is ASSEMBLY: it takes the
        // finished segment receipts and turns them into a Receipt. None of it touches the prover.
        // Split out so a distributed prover -- which obtains those receipts from other machines
        // rather than from the loop above -- runs byte-identical assembly instead of a reimplementation
        // that could drift from this one. The subtle step is the journal/assumption merge into the
        // LAST segment's claim, which is easy to miss and produces a receipt that fails its own check.
        self.assemble_from_segment_receipts(ctx, session, segments)
    }

    fn segment_preflight(&self, segment: &Segment) -> Result<PreflightResults> {
        tracing::debug!("segment_preflight");

        ensure!(
            segment.po2() <= self.opts.max_segment_po2,
            "segment po2 exceeds max on ProverOpts: {} > {}",
            segment.po2(),
            self.opts.max_segment_po2
        );
        let inner = risc0_circuit_rv32im::prove::segment_prover()?.preflight(&segment.inner)?;

        Ok(PreflightResults {
            inner,
            terminate_state: segment.inner.claim.terminate_state,
            output: segment.output.clone(),
            segment_index: segment.index,
        })
    }

    fn prove_segment_core(
        &self,
        ctx: &VerifierContext,
        preflight_results: PreflightResults,
    ) -> Result<SegmentReceipt> {
        tracing::debug!("prove_segment_core");

        ensure!(
            self.opts.hashfn == "poseidon2",
            "provided `ProverOpts` has unsupported `hashfn` value of \"{}\"; \
            supported `hashfn` values are: \"poseidon2\".",
            &self.opts.hashfn
        );

        let po2 = preflight_results.inner.po2();
        let seal =
            risc0_circuit_rv32im::prove::segment_prover()?.prove_core(preflight_results.inner)?;
        let mut claim = ReceiptClaim::decode_from_seal_v2(&seal, Some(po2))?;
        claim.output = preflight_results.output.into();

        let verifier_parameters = ctx
            .segment_verifier_parameters
            .as_ref()
            .ok_or_else(|| anyhow!("segment receipt verifier parameters missing from context"))?
            .digest();
        let receipt = SegmentReceipt {
            seal,
            index: preflight_results.segment_index,
            hashfn: self.opts.hashfn.clone(),
            claim,
            verifier_parameters,
        };
        receipt
            .verify_integrity_with_context(ctx)
            .context("verify segment")?;

        Ok(receipt)
    }

    fn lift(&self, receipt: &SegmentReceipt) -> Result<SuccinctReceipt<ReceiptClaim>> {
        let receipt = lift(receipt)?;
        receipt.verify_integrity().context("verify lift")?;
        Ok(receipt)
    }

    fn lift_povw(
        &self,
        receipt: &SegmentReceipt,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        lift_povw(receipt)
    }

    fn join(
        &self,
        a: &SuccinctReceipt<ReceiptClaim>,
        b: &SuccinctReceipt<ReceiptClaim>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        let receipt = join(a, b)?;
        receipt.verify_integrity().context("verify join")?;
        Ok(receipt)
    }

    fn join_povw(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        b: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        join_povw(a, b)
    }

    fn join_unwrap_povw(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        b: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        join_unwrap_povw(a, b)
    }

    fn resolve(
        &self,
        conditional: &SuccinctReceipt<ReceiptClaim>,
        assumption: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        let receipt = resolve(conditional, assumption)?;
        receipt.verify_integrity().context("verify resolve")?;
        Ok(receipt)
    }

    fn resolve_povw(
        &self,
        conditional: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        assumption: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<WorkClaim<ReceiptClaim>>> {
        resolve_povw(conditional, assumption)
    }

    fn resolve_unwrap_povw(
        &self,
        conditional: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
        assumption: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        resolve_unwrap_povw(conditional, assumption)
    }

    fn identity_p254(
        &self,
        a: &SuccinctReceipt<ReceiptClaim>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        // TODO: figure out how to verify this
        identity_p254(a)
    }

    fn prove_keccak(
        &self,
        request: &crate::ProveKeccakRequest,
    ) -> Result<SuccinctReceipt<Unknown>> {
        // TODO: figure out how to verify this
        prove_keccak(request)
    }

    fn union(
        &self,
        a: &SuccinctReceipt<Unknown>,
        b: &SuccinctReceipt<Unknown>,
    ) -> Result<SuccinctReceipt<UnionClaim>> {
        let receipt = union(a, b)?;
        receipt.verify_integrity().context("verify union")?;
        Ok(receipt)
    }

    fn unwrap_povw(
        &self,
        a: &SuccinctReceipt<WorkClaim<ReceiptClaim>>,
    ) -> Result<SuccinctReceipt<ReceiptClaim>> {
        unwrap_povw(a)
    }
}

fn check_claims(
    session_claim: &ReceiptClaim,
    other_name: &str,
    other_claim: MaybePruned<ReceiptClaim>,
) -> Result<()> {
    let session_claim_digest = session_claim.digest();
    let other_claim_digest = other_claim.digest();
    if session_claim_digest != other_claim_digest {
        tracing::debug!("session claim and {other_name} do not match");
        tracing::debug!("session claim: {session_claim:#?}");
        tracing::debug!("{other_name} claim: {other_claim:#?}");
        bail!(
            "session claim: {} != {other_name} claim: {}",
            hex::encode(session_claim_digest),
            hex::encode(other_claim_digest)
        );
    }
    Ok(())
}

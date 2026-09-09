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
        let last = segments.last_mut().ok_or_else(|| anyhow!("session is empty"))?;
        self.merge_session_output(session, last)?;

        let mut lifted = Vec::with_capacity(segments.len());
        for seg in segments.iter() {
            lifted.push(self.lift(seg)?);
        }
        Ok(lifted)
    }

    /// SEGMENT DISTRIBUTION step 5 (hazync#161). The claim surgery half of `prepare_lifts`, on its own.
    ///
    /// `prepare_lifts` does two things with very different requirements: it folds the session journal
    /// digest and assumption set into the LAST segment's claim, and it lifts. The merge needs the
    /// session and no prover; the lift needs a prover and no session. Bundling them is why the
    /// coordinator has to own a GPU for one step at the very end of an otherwise fully distributed
    /// run -- and why a CUDA build whose device has gone away aborts there, discarding everything.
    ///
    /// Split out so the merge can happen on the coordinator and the lift can be handed to a worker
    /// like any other job. `prepare_lifts` is written in terms of this, so there is one definition of
    /// how the session output is merged rather than two that can drift.
    fn merge_session_output(&self, session: &Session, last: &mut SegmentReceipt) -> Result<()> {
        let (assumptions, _): (Vec<_>, Vec<_>) = session.assumptions.iter().cloned().unzip();
        last.claim
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
        // merge_with hands back a &mut to what it merged into; the caller wants the receipt it
        // already owns, not a borrow of its interior.
        Ok(())
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

        let _ = session_assumption_receipts;

        // Written in terms of the two step-4 methods rather than repeating the loop, so that
        // resolving locally and resolving on a worker cannot drift apart in how they finish.
        let mut conditional = joined;
        for assumption in self.session_assumptions_succinct(session)? {
            conditional = self.resolve(&conditional, &assumption)?;
        }
        self.assemble_from_resolved(session, conditional)
    }

    /// SEGMENT DISTRIBUTION step 4 (hazync#153). See the trait for why this exists.
    fn session_assumptions_succinct(
        &self,
        session: &Session,
    ) -> Result<Vec<SuccinctReceipt<Unknown>>> {
        let (_, session_assumption_receipts): (Vec<_>, Vec<_>) =
            session.assumptions.iter().cloned().unzip();

        let mut out = Vec::with_capacity(session_assumption_receipts.len());
        for assumption_receipt in session_assumption_receipts {
            let inner = match assumption_receipt {
                AssumptionReceipt::Proven(receipt) => receipt,
                AssumptionReceipt::Unresolved(a) => {
                    bail!("cannot discharge an unresolved assumption: {a:#?}")
                }
            };
            out.push(match inner {
                InnerAssumptionReceipt::Succinct(a) => a,
                InnerAssumptionReceipt::Composite(a) => {
                    // Converted here, not at the caller: a worker should get something it can
                    // resolve directly, and composite_to_succinct is the prover's business.
                    let s = self.composite_to_succinct(&a)?;
                    SuccinctReceipt::<ReceiptClaim>::into_unknown(s)
                }
                InnerAssumptionReceipt::Fake(_) => {
                    bail!("fake receipt assumptions are not supported here")
                }
                InnerAssumptionReceipt::Groth16(_) => {
                    bail!("Groth16 receipt assumptions are not supported here")
                }
            });
        }
        // ORDER IS LOAD-BEARING. Resolves are a chain, each consuming the previous conditional, and
        // the guest recorded its assumptions in a definite order. Returning them in session order is
        // what lets a caller resolve them elsewhere and still land on the same claim.
        Ok(out)
    }

    /// SEGMENT DISTRIBUTION step 4 (hazync#153). See the trait for why this exists.
    fn assemble_from_resolved(
        &self,
        session: &Session,
        resolved: SuccinctReceipt<ReceiptClaim>,
    ) -> Result<ProveInfo> {
        let receipt = Receipt::new(
            InnerReceipt::Succinct(resolved),
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

        // Preflight pipelining was here and is REMOVED. It overlapped segment_preflight with
        // prove_segment_core by running preflight on a thread spawned inside thread::scope, and it
        // deadlocked CUDA: hazync#147, chunk 11 of block 962000 at po2 22, hung twice for 76 min
        // and 3h38m with the GPU at 0% and the consumer parked in rx.recv(). Confirmed by running
        // the same chunk, same po2, same binary with the schedule as the only variable.
        //
        // It is not worth fixing. A fix means either keeping CUDA work off the second thread, which
        // defeats the point, or managing CUDA contexts per thread inside risc0's HAL. And the same
        // overlap is available safely by running more worker PROCESSES, which do not share a CUDA
        // context: measured at 1.20x on one card, against the ~6% this bought. Removing it also
        // unblocks po2 22, worth 1.15x on chunks and 1.42x on the aggregate.
        let mut segments = Vec::new();
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
        // hazync#119: keep the evidence before the error unwinds. See `capture_119` at the end of
        // this file for why -- in short, every occurrence since 2026-08-16 has produced one log line
        // and no artifact, which is why the issue has a rate and no diagnosis.
        if let Err(e) = receipt.verify_integrity_with_context(ctx) {
            capture_119(&receipt, po2, "invalid", Some(&format!("{e:#}")));
            capture_119_failed()
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .insert(receipt.index);
            return Err(e).context("verify segment");
        }

        // This segment index produced an invalid proof earlier in THIS process and has now proved
        // correctly from identical input (the hazync#237 retry re-proves the same index). Two seals
        // for one claim, one valid and one not, is the artifact #119 has never had.
        if capture_119_failed()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(&receipt.index)
        {
            capture_119(&receipt, po2, "valid-after-retry", None);
        } else if std::env::var("HAZYNC_119_CAPTURE_ALL").as_deref() == Ok("1") {
            // Self-test only -- see the note above capture_119_dir.
            capture_119(&receipt, po2, "valid-capture-all", None);
        }

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

// ================================================================================================
// hazync#119 — EVIDENCE CAPTURE FOR THE INTERMITTENT INVALID SEGMENT PROOF
//
// `prove_segment_core` above verifies each segment proof it produces. On CUDA that check
// intermittently rejects a segment this very prover has just proved: reproduced on L40S and on
// B200, at po2 20, 21 and 22, with stock upstream scheduling, an idle card, no profiler attached,
// zero ECC errors and unremarkable VRAM. Execution is deterministic — the same segment proves
// correctly on a later attempt from byte-identical input — so it is the PROVING that is
// nondeterministic.
//
// Since 2026-08-16 every occurrence has produced ONE LOG LINE AND NOTHING ELSE. That is why the
// issue has a well-measured rate and no diagnosis: there has never been an artifact to examine.
// Upstream is not a route either — risc0#3798 has no maintainer reply, and risc0#3781 (a one-line
// CUDA overflow fix, with a test) has sat unmerged since 2026-07-11.
//
// Keeping the failing seal costs nothing on the happy path and makes two things possible that were
// previously out of reach, both WITHOUT a GPU:
//
//   * Re-verify the failing seal with the CPU verifier. If it verifies there, the fault is in the
//     CUDA VERIFIER; if it fails there too, the PROOF is genuinely bad. This question has been open
//     since 2026-08-17 and was costed at 5–16 h of CPU proving per trial, needing ten or more
//     trials for a meaningful negative. On a captured seal it costs seconds — the expensive framing
//     was "re-run the workload on CPU", not "check the artifact on CPU".
//
//   * Diff a failing seal against a passing one for the SAME segment index: same claim, same input,
//     one seal that verifies and one that does not. That is also precisely what risc0#3798 offered
//     upstream and could never send.
//
// Capture is on by default and bounded. It writes only on the failure path, keeps at most
// HAZYNC_119_CAPTURE_MAX (default 4) files per process so a busy prover cannot fill a contributor's
// disk, and is switched off with HAZYNC_119_CAPTURE=off.
//
// ⛔ SELF-TEST, and why it exists. Everything above runs ONLY when #119 fires, which needs a CUDA
// box and a ~6% roll of the dice. A writer that has never executed is exactly the kind of check
// that turns out to be broken on the one occurrence it was built for -- a wrong path, a
// serialization failure, an unwritable directory -- and by then the evidence is gone.
//
// HAZYNC_119_CAPTURE_ALL=1 captures every segment on the SUCCESS path instead, so the writer, the
// metadata and `host verify-segment` can all be exercised end to end on any machine with no GPU and
// no fault. It is a test switch, not an operational one: it writes a file per segment (bounded by
// HAZYNC_119_CAPTURE_MAX like everything else) and must never be set on a prover doing real work.

/// Where captures are written, or `None` when capture is switched off.
fn capture_119_dir() -> Option<std::path::PathBuf> {
    match std::env::var("HAZYNC_119_CAPTURE").as_deref() {
        Ok("off") | Ok("0") => None,
        Ok(d) if !d.is_empty() => Some(std::path::PathBuf::from(d)),
        _ => std::env::var("HOME")
            .ok()
            .map(|h| std::path::PathBuf::from(h).join(".hazync").join("119-captures")),
    }
}

/// Segment indices that produced an invalid proof in this process, so the pass that the retry
/// produces for the same index can be kept as its matched partner.
fn capture_119_failed() -> &'static std::sync::Mutex<std::collections::HashSet<u32>> {
    static FAILED: std::sync::OnceLock<std::sync::Mutex<std::collections::HashSet<u32>>> =
        std::sync::OnceLock::new();
    FAILED.get_or_init(Default::default)
}

/// Write one segment receipt to the capture directory, with a sidecar naming what it is.
///
/// Deliberately infallible from the caller's point of view: this runs on a path that is already
/// failing, and a capture that panicked would turn a recoverable fault into a lost chunk. Every
/// error is reported on stderr and swallowed.
fn capture_119(receipt: &SegmentReceipt, po2: u32, tag: &str, err: Option<&str>) {
    use std::sync::atomic::{AtomicUsize, Ordering};
    static WRITTEN: AtomicUsize = AtomicUsize::new(0);

    let Some(dir) = capture_119_dir() else { return };
    let max = std::env::var("HAZYNC_119_CAPTURE_MAX")
        .ok()
        .and_then(|s| s.parse::<usize>().ok())
        .unwrap_or(4);
    if WRITTEN.load(Ordering::Relaxed) >= max {
        return;
    }

    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let stem = format!("seg{:06}_{tag}_{ts}", receipt.index);

    if let Err(e) = std::fs::create_dir_all(&dir) {
        eprintln!("  [#119] capture: cannot create {}: {e}", dir.display());
        return;
    }

    let seal_path = dir.join(format!("{stem}.segment-receipt.bin"));
    match bincode::serialize(receipt) {
        Ok(bytes) => {
            if let Err(e) = std::fs::write(&seal_path, &bytes) {
                eprintln!("  [#119] capture: cannot write {}: {e}", seal_path.display());
                return;
            }
        }
        Err(e) => {
            eprintln!("  [#119] capture: cannot serialize segment receipt: {e}");
            return;
        }
    }

    // Hand-rolled rather than serde_json: that is a dev-dependency here, and this is not worth
    // moving into the build graph of every prover.
    let meta = format!(
        concat!(
            "{{\n",
            "  \"issue\": \"hazync#119\",\n",
            "  \"tag\": \"{tag}\",\n",
            "  \"unix_time\": {ts},\n",
            "  \"segment_index\": {index},\n",
            "  \"po2\": {po2},\n",
            "  \"hashfn\": \"{hashfn}\",\n",
            "  \"seal_words\": {seal_words},\n",
            "  \"verifier_parameters\": \"{vp}\",\n",
            "  \"claim_pre_state\": \"{pre}\",\n",
            "  \"claim_post_state\": \"{post}\",\n",
            "  \"claim_digest\": \"{claim}\",\n",
            "  \"error\": \"{err}\"\n",
            "}}\n"
        ),
        tag = tag,
        ts = ts,
        index = receipt.index,
        po2 = po2,
        hashfn = receipt.hashfn,
        seal_words = receipt.seal.len(),
        vp = receipt.verifier_parameters,
        pre = receipt.claim.pre.digest(),
        post = receipt.claim.post.digest(),
        claim = receipt.claim.digest(),
        err = err.unwrap_or("").replace('"', "'").replace('\n', " "),
    );
    let meta_path = dir.join(format!("{stem}.json"));
    if let Err(e) = std::fs::write(&meta_path, meta) {
        eprintln!("  [#119] capture: cannot write {}: {e}", meta_path.display());
        return;
    }

    let n = WRITTEN.fetch_add(1, Ordering::Relaxed) + 1;
    // stderr, not `tracing`: this must be visible in a worker log with no RUST_LOG set. An
    // occurrence that is not shouted about is an occurrence nobody goes and looks at.
    eprintln!(
        "  [#119] CAPTURED {tag} segment {} (po2 {po2}, {} seal words) -> {}  [{n}/{max}]",
        receipt.index,
        receipt.seal.len(),
        seal_path.display(),
    );
}

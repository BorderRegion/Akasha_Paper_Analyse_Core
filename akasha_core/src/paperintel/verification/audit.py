"""verification.audit — the audit bundle view (P08, doc 05 P08).

Answers the doc 00 §7.16 questions from the stored trail alone:
- which exact model call created a suspicious claim?
- which pipeline/prompt/model version produced it?
- what evidence did it cite, and what did the verifiers conclude?

The bundle is assembled from immutable records (claims, evidence links,
model calls, verification rows, analysis runs) — nothing is recomputed
from mutable state, so the view can be trusted after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    AnalysisRunRow,
    ClaimEvidenceRow,
    ClaimRow,
    EvidenceRow,
    ExternalProvenanceRow,
    ModelCallRow,
    VerificationRow,
)
from paperintel.errors import DomainError


@dataclass(slots=True)
class AuditBundle:
    """Full provenance chain for one claim."""

    claim_id: str
    paper_id: str
    paper_version_id: str
    claim_type: str
    category: str
    statement: str
    support_state: str
    pipeline_version: str
    #: The run that created the claim.
    created_by_run: dict[str, Any] | None = None
    #: Model call(s) of that run (prompt/model/provider provenance).
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    #: Cited evidence with locators and content hashes.
    evidence: list[dict[str, Any]] = field(default_factory=list)
    #: Verifier verdicts, latest first.
    verifications: list[dict[str, Any]] = field(default_factory=list)
    external_provenance: list[dict[str, Any]] = field(default_factory=list)
    #: Structured warnings (unsupported citations, missing evidence...).
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """A bundle is complete when the provenance chain is unbroken:
        the creating run exists, every evidence link resolves, and at
        least one verification is recorded."""
        return (
            self.created_by_run is not None
            and (len(self.evidence) > 0 or len(self.external_provenance) > 0)
            and len(self.verifications) > 0
            and not self.warnings
        )


def build_claim_audit_bundle(session: Session, claim_id: str) -> AuditBundle:
    """Assemble the audit bundle for one claim."""
    claim = session.get(ClaimRow, claim_id)
    if claim is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown claim ID: {claim_id}",
            details={"claim_id": claim_id},
        )

    bundle = AuditBundle(
        claim_id=claim.claim_id,
        paper_id=claim.paper_id,
        paper_version_id=claim.paper_version_id,
        claim_type=claim.claim_type.value,
        category=claim.category,
        statement=claim.statement,
        support_state=claim.support_state.value,
        pipeline_version=claim.pipeline_version,
    )

    run = session.get(AnalysisRunRow, claim.created_by_run_id)
    if run is None:
        bundle.warnings.append(f"creating run {claim.created_by_run_id} is missing")
    else:
        bundle.created_by_run = {
            "run_id": run.run_id,
            "agent_type": run.agent_type,
            "pipeline_version": run.pipeline_version,
            "spec_version": run.spec_version,
            "prompt_version": run.prompt_version,
            "config_hash": run.config_hash,
            "model_id": run.model_id,
            "provider_id": run.provider_id,
            "status": run.status.value,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "trace_id": run.trace_id,
        }
        model_calls = session.scalars(
            select(ModelCallRow)
            .where(ModelCallRow.run_id == run.run_id)
            .order_by(ModelCallRow.created_at, ModelCallRow.model_call_id)
        ).all()
        for call in model_calls:
            bundle.model_calls.append(
                {
                    "model_call_id": call.model_call_id,
                    "provider_id": call.provider_id,
                    "model_id": call.model_id,
                    "prompt_version_id": call.prompt_version_id,
                    "transport_status": call.transport_status.value,
                    "schema_status": call.schema_status.value,
                    "request_hash": call.request_hash,
                    "response_hash": call.response_hash,
                    "latency_ms": call.latency_ms,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "created_at": call.created_at.isoformat(),
                }
            )

    links = session.scalars(
        select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id == claim.claim_id)
    ).all()
    for link in links:
        row: EvidenceRow | None = session.get(EvidenceRow, link.evidence_id)
        if row is None:
            bundle.warnings.append(f"evidence row {link.evidence_id} is missing")
            bundle.evidence.append(
                {"evidence_id": link.evidence_id, "role": link.role.value, "missing": True}
            )
            continue
        bundle.evidence.append(
            {
                "evidence_id": row.evidence_id,
                "role": link.role.value,
                "evidence_type": row.evidence_type.value,
                "page_start": row.page_start,
                "page_end": row.page_end,
                "section_id": row.section_id,
                "source_method": row.source_method.value,
                "quality_state": row.quality_state.value,
                "ocr_confidence": row.ocr_confidence,
                "content_sha256": row.content_sha256,
                "extraction_run_id": row.extraction_run_id,
                "text_excerpt": (row.text or "")[:300],
            }
        )
    for source in session.scalars(
        select(ExternalProvenanceRow).where(ExternalProvenanceRow.claim_id == claim_id)
    ):
        bundle.external_provenance.append(
            {
                "source_provider": source.source_provider,
                "source_identifier": source.source_identifier,
                "source_url": source.source_url,
                "retrieved_at": source.retrieved_at.isoformat(),
                "content_hash": source.content_hash,
                "citation_text": source.citation_text,
            }
        )
    if not bundle.evidence and not bundle.external_provenance:
        bundle.warnings.append("claim has no evidence links (ledger invariant violated)")

    verifications = session.scalars(
        select(VerificationRow)
        .where(VerificationRow.claim_id == claim.claim_id)
        .order_by(VerificationRow.created_at.desc(), VerificationRow.verification_id.desc())
    ).all()
    for row in verifications:
        bundle.verifications.append(
            {
                "verification_id": row.verification_id,
                "verifier_type": row.verifier_type.value,
                "verdict": row.verdict,
                "reason_summary": row.reason_summary,
                "details": row.details_json,
                "created_by_run_id": row.created_by_run_id,
                "created_at": row.created_at.isoformat(),
            }
        )
    if not bundle.verifications:
        bundle.warnings.append("claim has not been verified yet")

    return bundle


def build_version_audit_bundle(session: Session, paper_version_id: str) -> dict[str, Any]:
    """Audit overview for a whole version: claim states, verification
    coverage, and the warnings aggregated across claims."""
    claims = session.scalars(
        select(ClaimRow)
        .where(ClaimRow.paper_version_id == paper_version_id)
        .order_by(ClaimRow.created_at, ClaimRow.claim_id)
    ).all()
    bundles = [build_claim_audit_bundle(session, claim.claim_id) for claim in claims]

    states: dict[str, int] = {}
    for bundle in bundles:
        states[bundle.support_state] = states.get(bundle.support_state, 0) + 1

    sections: dict[str, list] = {
        name: []
        for name in (
            "high_risk_claims",
            "unsupported_claims",
            "ocr_sensitive_claims",
            "numeric_conflicts",
            "agent_disagreements",
            "single_model_claims",
            "external_inferences",
            "verified_claims",
        )
    }
    for bundle in bundles:
        item = {
            "claim_id": bundle.claim_id,
            "statement": bundle.statement,
            "claim_type": bundle.claim_type,
            "support_state": bundle.support_state,
        }
        latest = {}
        for verification in bundle.verifications:
            latest.setdefault(verification["verifier_type"], verification)

        def adverse(kind, latest=latest):
            return latest.get(kind, {}).get("verdict") in ("FAIL", "WARN")

        if bundle.support_state == "SUPPORTED":
            sections["verified_claims"].append(item)
        if bundle.support_state in ("UNSUPPORTED", "INSUFFICIENT_EVIDENCE"):
            sections["unsupported_claims"].append(item)
        ocr = adverse("verification.ocr_sensitivity") or any(
            e.get("source_method") in ("OCR", "MIXED_NATIVE_OCR")
            and (
                e.get("quality_state") == "DEGRADED"
                or (e.get("ocr_confidence") is not None and e["ocr_confidence"] < 0.8)
            )
            for e in bundle.evidence
        )
        if ocr:
            sections["ocr_sensitive_claims"].append(item)
        if adverse("verification.numeric"):
            sections["numeric_conflicts"].append(item)
        if adverse("verification.independent_consensus") or adverse("verification.contradiction"):
            sections["agent_disagreements"].append(item)
        if len({(c["provider_id"], c["model_id"]) for c in bundle.model_calls}) == 1:
            sections["single_model_claims"].append(item)
        if bundle.claim_type == "EXTERNAL":
            sections["external_inferences"].append(item)
        if (
            bundle.support_state in ("DISPUTED", "UNSUPPORTED", "INSUFFICIENT_EVIDENCE")
            or ocr
            or any(v["verdict"] == "FAIL" for v in latest.values())
        ):
            sections["high_risk_claims"].append(item)

    return {
        **sections,
        "summary": {
            **{
                key: len(sections[key])
                for key in (
                    "verified_claims",
                    "unsupported_claims",
                    "ocr_sensitive_claims",
                    "agent_disagreements",
                    "numeric_conflicts",
                )
            },
            "disputed_claims": states.get("DISPUTED", 0),
        },
        "paper_version_id": paper_version_id,
        "claim_count": len(bundles),
        "support_states": states,
        "unverified_claims": [b.claim_id for b in bundles if not b.verifications],
        "incomplete_bundles": [b.claim_id for b in bundles if not b.complete],
        "evidence_link_total": sum(len(b.evidence) for b in bundles),
        "model_call_total": sum(len(b.model_calls) for b in bundles),
        "verification_total": sum(len(b.verifications) for b in bundles),
    }

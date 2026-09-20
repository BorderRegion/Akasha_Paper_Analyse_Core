"""Claim ledger, verification, and knowledge-entity contracts
(spec doc 03 §1.5-1.7, §3, §14-15; spec doc 01 §11-13)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from paperintel.schemas.common import (
    ClaimId,
    EvidenceId,
    ExternalProvenance,
    FrozenModel,
    LooseEvidenceId,
    PaperId,
    PaperVersionId,
    RunId,
    VerificationId,
)
from paperintel.schemas.enums import (
    ClaimType,
    EvidenceRole,
    QualityDimension,
    SupportState,
    VerifierType,
    VerifierVerdict,
)

# ---------------------------------------------------------------------------
# Canonical claim ledger
# ---------------------------------------------------------------------------


class ClaimEvidenceLink(FrozenModel):
    """Many-to-many claim<->evidence association (spec doc 03 §1.5)."""

    claim_id: ClaimId
    evidence_id: EvidenceId
    role: EvidenceRole


class ConfidenceComponents(FrozenModel):
    """Component scores behind a system confidence value so the combined
    score is explainable (spec doc 03 §13). The exact combination formula is
    configuration-versioned; model self-confidence is never the primary
    system confidence measure (spec doc 00 §7.10)."""

    evidence_quality: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    support_directness: float | None = Field(default=None, ge=0.0, le=1.0)
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    independent_agreement: float | None = Field(default=None, ge=0.0, le=1.0)
    verifier_outcome: float | None = Field(default=None, ge=0.0, le=1.0)
    contradiction_status: float | None = Field(default=None, ge=0.0, le=1.0)
    external_source_reliability: float | None = Field(default=None, ge=0.0, le=1.0)
    formula_version: str | None = None


class Claim(FrozenModel):
    """A canonical claim ledger entry (spec doc 03 §1.5).

    Invariant (spec doc 00 §7.1): no factual or evaluative claim enters the
    ledger without evidence references or explicit EXTERNAL provenance.
    """

    claim_id: ClaimId
    paper_id: PaperId
    paper_version_id: PaperVersionId
    claim_type: ClaimType
    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    normalized_statement: str | None = None
    support_state: SupportState
    #: Confidence the producing agent/model assigned to itself. Recorded for
    #: diagnostics only; never canonical system confidence.
    analysis_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    system_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    system_confidence_components: ConfidenceComponents | None = None
    created_by_run_id: RunId
    pipeline_version: str = Field(min_length=1)
    created_at: datetime
    superseded_by_claim_id: ClaimId | None = None
    #: Required for EXTERNAL claims and externally verified novelty.
    external_provenance: ExternalProvenance | None = None

    @model_validator(mode="after")
    def _provenance_rules(self) -> Claim:
        if self.claim_type is ClaimType.EXTERNAL and self.external_provenance is None:
            raise ValueError("EXTERNAL claims require external_provenance")
        return self


class Verification(FrozenModel):
    """One verifier outcome for one claim (spec doc 03 §1.7)."""

    verification_id: VerificationId
    claim_id: ClaimId
    verifier_type: VerifierType
    status: str = Field(min_length=1)
    verdict: VerifierVerdict
    reason_summary: str = ""
    details_json: dict | None = None
    created_by_run_id: RunId
    created_at: datetime


# ---------------------------------------------------------------------------
# LLM-produced claim candidates (schema-firewall input, spec doc 03 §3)
# ---------------------------------------------------------------------------


class CandidateEvidenceRef(FrozenModel):
    """Evidence reference as produced by an LLM. Prefix-checked only at this
    layer; existence, paper-scope and support rules are enforced by semantic
    and evidence validation before persistence."""

    evidence_id: LooseEvidenceId
    role: EvidenceRole


class ClaimCandidate(FrozenModel):
    """A claim candidate from LLM output (spec doc 03 §3).

    Persistence rules enforced downstream (semantic/evidence layers):
    1. evidence IDs must exist;
    2. evidence must belong to the declared paper version;
    3. statement must not be empty (enforced here);
    4. FACT must have direct supporting evidence;
    5. unsupported factual numbers are rejected;
    6. duplicate/near-duplicate claims are normalized.
    """

    claim_type: ClaimType
    category: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=20000)
    evidence: list[CandidateEvidenceRef] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("evidence")
    @classmethod
    def _unique_evidence(cls, refs: list[CandidateEvidenceRef]) -> list[CandidateEvidenceRef]:
        by_id: dict[str, CandidateEvidenceRef] = {}
        for ref in refs:
            previous = by_id.get(ref.evidence_id)
            if previous is None or previous.role is EvidenceRole.CONTEXT:
                by_id[ref.evidence_id] = ref
            elif ref.role not in (previous.role, EvidenceRole.CONTEXT):
                raise ValueError("One evidence reference cannot both support and contradict a claim")
        return list(by_id.values())

    @model_validator(mode="after")
    def _fact_needs_support(self) -> ClaimCandidate:
        # Rule 4 shape-check: a FACT candidate with no SUPPORT evidence can
        # never become valid; reject at the schema layer already.
        if self.claim_type is ClaimType.FACT and not any(
            ref.role is EvidenceRole.SUPPORT for ref in self.evidence
        ):
            raise ValueError("FACT claim candidates require at least one SUPPORT evidence ref")
        return self


# ---------------------------------------------------------------------------
# Knowledge entities
# ---------------------------------------------------------------------------


class TechniqueEntity(FrozenModel):
    """Experimental/engineering technique as a first-class entity
    (spec doc 01 §9.7, doc 03 §14)."""

    technique_entity_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    problem_addressed: str = ""
    procedure_summary: str = ""
    conditions: str = ""
    reported_effect: str = ""
    cost_or_tradeoff: str = ""
    transferability_tags: list[str] = Field(default_factory=list)
    paper_ids: list[PaperId] = Field(default_factory=list)
    evidence_ids: list[LooseEvidenceId] = Field(default_factory=list)


class QualityDimensionAssessment(FrozenModel):
    """One dimension of the paper quality vector (spec doc 03 §15).

    Do not store unexplained numeric scores only: every assessment carries
    evidence, counter-evidence and system confidence.
    """

    dimension: QualityDimension
    assessment: str = Field(min_length=1)
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_ids: list[LooseEvidenceId] = Field(default_factory=list)
    counter_evidence_ids: list[LooseEvidenceId] = Field(default_factory=list)
    system_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    run_id: RunId


class VenueRecord(FrozenModel):
    """Venue registry entry (spec doc 01 §13).

    LLMs MUST NOT invent venue rank; venue facts are resolved by the
    VenueRegistry and stored with ranking system + year + retrieval time.
    """

    venue_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    type: str | None = None
    publisher: str | None = None
    peer_reviewed: bool | None = None
    ranking_system: str | None = None
    ranking_value: str | None = None
    ranking_year: int | None = None
    source: str | None = None
    source_retrieved_at: datetime | None = None


__all__ = [
    "Claim",
    "ClaimCandidate",
    "ClaimEvidenceLink",
    "ConfidenceComponents",
    "CandidateEvidenceRef",
    "QualityDimensionAssessment",
    "TechniqueEntity",
    "VenueRecord",
    "Verification",
]

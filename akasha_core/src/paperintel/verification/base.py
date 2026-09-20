"""verification.base — verifier contract + registry (P08, doc 01 §10).

A verifier is a deterministic function over one claim (+ its evidence
links + the version's other claims where needed) producing a
VerifierResult with a frozen verdict (PASS/WARN/FAIL/INCONCLUSIVE) and a
human-readable reason. Verifiers NEVER mutate claims directly — the
service applies support-state transitions from the recorded verdicts
(single choke point, auditable).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from paperintel.database.models import ClaimRow
from paperintel.schemas.enums import ResourceTier, VerifierType

#: Tier → verifier set (doc 01 §10: "Not every verifier must run at every
#: tier. T3 must run the complete applicable set."). external_novelty
#: runs only when an external source is configured; otherwise it records
#: INCONCLUSIVE (honest, never silent).
TIER_VERIFIERS: dict[ResourceTier, tuple[VerifierType, ...]] = {
    ResourceTier.T0_INDEX: (VerifierType.EVIDENCE_EXISTENCE,),
    ResourceTier.T1_SCAN: (VerifierType.EVIDENCE_EXISTENCE,),
    ResourceTier.T2_FULL: (
        VerifierType.EVIDENCE_EXISTENCE,
        VerifierType.CITATION,
        VerifierType.NUMERIC,
        VerifierType.CLAIM_SCOPE,
    ),
    ResourceTier.T3_DEEP: (
        VerifierType.EVIDENCE_EXISTENCE,
        VerifierType.CITATION,
        VerifierType.NUMERIC,
        VerifierType.CLAIM_SCOPE,
        VerifierType.CONTRADICTION,
        VerifierType.INDEPENDENT_CONSENSUS,
        VerifierType.FALSIFICATION,
        VerifierType.OCR_SENSITIVITY,
        VerifierType.EXTERNAL_NOVELTY,
    ),
}


@dataclass(slots=True)
class VerifierResult:
    """Outcome of one verifier over one claim."""

    verifier_type: VerifierType
    verdict: str  # VerifierVerdict value
    reason_summary: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"


#: verifier_type → callable(session, claim) → VerifierResult
VERIFIERS: dict[VerifierType, Any] = {}


def register_verifier(verifier_type: VerifierType):
    """Explicit registration — the only way a verifier runs."""

    def _wrap(fn):
        if verifier_type in VERIFIERS:
            raise ValueError(f"Verifier already registered: {verifier_type}")
        VERIFIERS[verifier_type] = fn
        return fn

    return _wrap


def run_verifier(verifier_type: VerifierType, session: Session, claim: ClaimRow) -> VerifierResult:
    """Dispatch one verifier; unregistered types fail loudly (CFG_002)."""
    from paperintel.errors import DomainError

    fn = VERIFIERS.get(verifier_type)
    if fn is None:
        raise DomainError(
            "CFG_002",
            message=f"Verifier not registered: {verifier_type.value}",
            details={"verifier_type": verifier_type.value},
        )
    from paperintel.schemas.enums import ClaimType

    if claim.claim_type is ClaimType.EXTERNAL and verifier_type in (
        VerifierType.EVIDENCE_EXISTENCE,
        VerifierType.CITATION,
        VerifierType.NUMERIC,
    ):
        return _verify_external_record(verifier_type, session, claim)
    return fn(session, claim)


def _verify_external_record(verifier_type, session, claim):
    """Validate a metadata assertion against its retained external source.

    This establishes source attribution, not scientific truth or novelty.
    """
    import json

    from sqlalchemy import select

    from paperintel.database.models import ExternalProvenanceRow

    rows = session.scalars(
        select(ExternalProvenanceRow).where(ExternalProvenanceRow.claim_id == claim.claim_id)
    ).all()
    for row in rows:
        if not (
            row.source_provider
            and row.source_identifier
            and row.retrieved_at
            and row.content_hash
            and row.citation_text
        ):
            continue
        try:
            source = json.loads(row.citation_text)
            key, separator, value = claim.statement.partition(": ")
            matches = separator and key in source and json.loads(value) == source[key]
        except (ValueError, TypeError):
            matches = False
        if matches:
            return VerifierResult(
                verifier_type,
                "PASS",
                "assertion matches retained external metadata",
                {"provenance_id": row.provenance_id},
            )
    return VerifierResult(verifier_type, "FAIL", "external assertion lacks matching provenance", {})

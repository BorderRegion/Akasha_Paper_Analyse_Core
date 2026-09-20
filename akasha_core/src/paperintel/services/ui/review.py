"""services.ui.review — the human review queue (docs/03 §S06, docs/06 §审查).

Design rules encoded here:

- the queue is built from REAL verification outcomes and claim support states,
  never a fabricated "risk score";
- ``group`` says WHY an item is here (numeric/OCR fragility, unsupported
  evidence, contradiction, scope, unverified novelty) and ``NOT_VERIFIED`` is a
  different state from "verified clean" — an audit service that returned nothing
  is not the same as "all items 0";
- both sides are reachable: the claim's own evidence AND the counter-evidence
  (the contradicting claim's evidence, or a critique's counter-evidence links);
- a human decision is recorded separately and NEVER writes support_state.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimEvidenceRow,
    ClaimRow,
    PaperRow,
    VerificationRow,
)
from paperintel.schemas.enums import SupportState, VerifierType
from paperintel.schemas.ui.models import (
    ClaimPreview,
    ReviewItem,
    SourceRef,
    VerificationSummary,
)
from paperintel.services.ui import personal

#: Review groups derived from real verifier outcomes.
GROUP_BY_VERIFIER: dict[str, str] = {
    VerifierType.NUMERIC.value: "NUMERIC_OR_OCR",
    VerifierType.OCR_SENSITIVITY.value: "NUMERIC_OR_OCR",
    VerifierType.EVIDENCE_EXISTENCE.value: "EVIDENCE_UNSUPPORTED",
    VerifierType.CITATION.value: "EVIDENCE_UNSUPPORTED",
    VerifierType.CONTRADICTION.value: "CONTRADICTION",
    VerifierType.FALSIFICATION.value: "CONTRADICTION",
    VerifierType.CLAIM_SCOPE.value: "SCOPE",
    VerifierType.EXTERNAL_NOVELTY.value: "NOVELTY_UNVERIFIED",
    VerifierType.INDEPENDENT_CONSENSUS.value: "EVIDENCE_UNSUPPORTED",
}

RISKY_STATES = (
    SupportState.DISPUTED,
    SupportState.UNSUPPORTED,
    SupportState.PARTIALLY_SUPPORTED,
    SupportState.INSUFFICIENT_EVIDENCE,
)

#: Items shown when the client asks for "all", including the low-risk ones.
REVIEWABLE_STATES = (*RISKY_STATES, SupportState.SUPPORTED, SupportState.UNVERIFIED)


def claim_revision(claim: ClaimRow) -> str:
    """Deterministic revision token for a claim.

    The client echoes it back with a review decision; a claim that changed (or
    was superseded) since the human looked at it produces a different token, so
    the decision is refused instead of being attached to the wrong text.
    """
    material = "|".join(
        [
            claim.claim_id,
            claim.support_state.value,
            claim.statement,
            claim.created_at.isoformat(),
            claim.superseded_by_claim_id or "",
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _evidence_refs(
    session: Session, claim: ClaimRow, *, role: str | None = None
) -> list[SourceRef]:
    stmt = select(ClaimEvidenceRow.evidence_id).where(ClaimEvidenceRow.claim_id == claim.claim_id)
    if role is not None:
        stmt = stmt.where(ClaimEvidenceRow.role == role)
    ids = list(session.scalars(stmt.limit(5)))
    if not ids:
        return []
    return [
        SourceRef(
            paper_id=claim.paper_id,
            paper_version_id=claim.paper_version_id,
            claim_id=claim.claim_id,
            evidence_ids=ids,
        )
    ]


def _verifications(session: Session, claim_id: str) -> list[tuple[VerificationSummary, dict]]:
    """Verifier records with their structured details.

    The summary is what reaches the client; the details are used internally to
    find the counter-evidence (a contradicting claim, a critique) and are not
    shipped wholesale.
    """
    rows = session.scalars(
        select(VerificationRow)
        .where(VerificationRow.claim_id == claim_id)
        .order_by(VerificationRow.created_at.desc(), VerificationRow.verification_id)
    ).all()
    return [
        (
            VerificationSummary(
                verification_id=row.verification_id,
                verifier_type=row.verifier_type.value,
                status=row.status,
                verdict=row.verdict.value if hasattr(row.verdict, "value") else str(row.verdict),
                reason_summary=row.reason_summary or "",
                run_id=row.created_by_run_id,
            ),
            dict(row.details_json or {}),
        )
        for row in rows
    ]


def _group_and_reasons(
    claim: ClaimRow, verifications: list[tuple[VerificationSummary, dict]]
) -> tuple[str, list[str], list[str]]:
    """(group, reasons, counter_claim_ids) from real verifier outcomes."""
    failing = [
        (summary, details) for summary, details in verifications if summary.verdict != "PASS"
    ]
    reasons = [
        f"{summary.verifier_type} → {summary.verdict}: {summary.reason_summary}"
        for summary, _ in failing
    ]
    counter_claim_ids: list[str] = []
    for _, details in verifications:
        for key in ("conflicting_claim_id", "critique_claim_id"):
            value = details.get(key)
            if isinstance(value, str) and value not in counter_claim_ids:
                counter_claim_ids.append(value)
    if claim.claim_type.value == "CRITIQUE":
        reasons.append("人工判断：批评性结论")
    if failing:
        group = GROUP_BY_VERIFIER.get(failing[0][0].verifier_type, "SUPPORT_STATE")
    elif verifications:
        group = "SUPPORT_STATE"
    else:
        # No verification record at all is NOT "verified and clean".
        group = "NOT_VERIFIED"
        reasons.append("审计服务未返回该结论的核查记录（与「核查通过」不同）")
    reasons.append(f"support_state={claim.support_state.value}")
    return group, reasons, counter_claim_ids


def review_item(
    session: Session,
    claim: ClaimRow,
    *,
    paper_title: str,
    decisions: dict[str, str],
    evidence_count: int = 0,
) -> ReviewItem:
    records = _verifications(session, claim.claim_id)
    verifications = [summary for summary, _ in records]
    group, reasons, counter_claim_ids = _group_and_reasons(claim, records)

    counter_evidence: list[SourceRef] = []
    for other_id in counter_claim_ids:
        other = session.get(ClaimRow, other_id)
        if other is None:
            continue
        refs = _evidence_refs(session, other)
        if refs:
            counter_evidence.extend(refs)
    # Counter-evidence role links from the claim's own evidence set are the
    # other side of "this claim vs the critique against it".
    counter_evidence.extend(_evidence_refs(session, claim, role="COUNTER_EVIDENCE"))

    impact = (
        "HIGH"
        if claim.support_state in (SupportState.DISPUTED, SupportState.UNSUPPORTED)
        else "NORMAL"
    )
    return ReviewItem(
        claim=ClaimPreview(
            claim_id=claim.claim_id,
            statement=claim.statement,
            claim_type=claim.claim_type,
            support_state=claim.support_state,
            paper_version_id=claim.paper_version_id,
            evidence_count=evidence_count,
            is_superseded=claim.superseded_by_claim_id is not None,
        ),
        paper_id=claim.paper_id,
        paper_version_id=claim.paper_version_id,
        paper_title=paper_title,
        group=group,
        reasons=reasons,
        impact=impact,
        source_refs=_evidence_refs(session, claim) + counter_evidence,
        original_evidence=_evidence_refs(session, claim),
        counter_evidence=counter_evidence,
        verifications=verifications,
        claim_revision=claim_revision(claim),
        personal_decision=decisions.get(claim.claim_id),
    )


def query_review(
    session: Session,
    *,
    collection_id: str | None = None,
    groups: list[str] | None = None,
    include_seen: bool = False,
    include_all: bool = False,
    paper_version_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ReviewItem], int]:
    """The review queue: risky claims (or ALL claims when the client asks).

    ``include_all`` exists so "no risky items" is not confused with "the audit
    service returned nothing": the caller can show every claim and its real
    verification state. Items a human已经看过 are filtered out unless the client
    asks for them — they are never silently dropped from the data.
    """
    from paperintel.database.models import CollectionPaperRow

    states = REVIEWABLE_STATES if include_all else RISKY_STATES
    stmt = (
        select(ClaimRow)
        .where(ClaimRow.support_state.in_(states))
        .order_by(ClaimRow.created_at.desc(), ClaimRow.claim_id)
    )
    if collection_id:
        stmt = stmt.where(
            ClaimRow.paper_id.in_(
                select(CollectionPaperRow.paper_id).where(
                    CollectionPaperRow.collection_id == collection_id
                )
            )
        )
    if paper_version_id:
        stmt = stmt.where(ClaimRow.paper_version_id == paper_version_id)
    claims = list(session.scalars(stmt))
    titles = {
        row.paper_id: row.canonical_title
        for row in session.scalars(
            select(PaperRow).where(PaperRow.paper_id.in_({claim.paper_id for claim in claims}))
        )
    }
    decisions = personal_decisions(session)
    counts = _evidence_counts(session, [claim.claim_id for claim in claims])

    items: list[ReviewItem] = []
    for claim in claims:
        decision = decisions.get(claim.claim_id)
        if decision == "SEEN" and not include_seen:
            continue
        item = review_item(
            session,
            claim,
            paper_title=titles.get(claim.paper_id, ""),
            decisions=decisions,
            evidence_count=counts.get(claim.claim_id, 0),
        )
        if groups and item.group not in groups:
            continue
        items.append(item)

    # High impact first, then the order the corpus produced them in.
    items.sort(key=lambda item: (item.impact != "HIGH", item.claim.claim_id))
    total = len(items)
    return items[offset : offset + limit], total


def personal_decisions(session: Session) -> dict[str, str]:
    """Latest human decision per claim (support_state is never touched)."""
    return personal.latest_decisions(session)


def _evidence_counts(session: Session, claim_ids: list[str]) -> dict[str, int]:
    from paperintel.database.models import ClaimEvidenceRow as _Link

    if not claim_ids:
        return {}
    from sqlalchemy import func

    rows = session.execute(
        select(_Link.claim_id, func.count())
        .where(_Link.claim_id.in_(claim_ids))
        .group_by(_Link.claim_id)
    ).all()
    return {claim_id: int(count) for claim_id, count in rows}


__all__ = [
    "GROUP_BY_VERIFIER",
    "REVIEWABLE_STATES",
    "RISKY_STATES",
    "claim_revision",
    "personal_decisions",
    "query_review",
    "review_item",
]

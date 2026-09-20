"""triage.service — resource-tier triage (P07, spec doc 03 §16).

Triage is RESOURCE ALLOCATION ONLY, never paper quality. The result
records the recommended tier, the effective tier (manual override always
wins, doc 02 §11), the signals behind the decision, and machine-readable
reason codes. Every decision is explainable after the fact.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.config.fingerprint import analysis_config_hash
from paperintel.database.models import CollectionPaperRow, PaperRow, TriageResultRow
from paperintel.errors import DomainError
from paperintel.ids import new_run_id
from paperintel.schemas.enums import ResourceTier
from paperintel.version import PIPELINE_VERSION

#: Neutral signal defaults when no collection context exists (doc 03 §16
#: signals — recorded explicitly, never invented).
_DEFAULT_SIGNALS: dict[str, float] = {
    "user_relevance": 0.0,
    "novelty_signal": 0.0,
    "method_transferability": 0.0,
    "research_importance": 0.0,
    "uncertainty_value": 0.0,
    "venue_prior": 0.0,
}

#: Pinned papers always analyze at full depth (doc 02 §11 manual override).
_PINNED_TIER = ResourceTier.T2_FULL


def compute_triage(
    session: Session,
    *,
    paper_id: str,
    requested_tier: ResourceTier,
    run_id: str | None = None,
) -> TriageResultRow:
    """Compute + persist one triage decision for a paper.

    Policy (deterministic, no LLM):
    - a collection priority_override_tier wins over everything (manual);
    - a pinned collection membership upgrades to at least T2_FULL;
    - otherwise the requested tier stands.

    Signals default to neutral (0.0) until collection intelligence exists
    (P08+ novelty signals); they are still recorded so the decision is
    explainable.
    """
    paper = session.get(PaperRow, paper_id)
    if paper is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown paper ID: {paper_id}",
            details={"paper_id": paper_id},
        )

    reason_codes: list[str] = []
    memberships = session.scalars(
        select(CollectionPaperRow).where(CollectionPaperRow.paper_id == paper_id)
    ).all()

    override_tier: ResourceTier | None = None
    pinned = False
    for membership in memberships:
        if membership.priority_override_tier is not None:
            override_tier = membership.priority_override_tier
            reason_codes.append("COLLECTION_PRIORITY_OVERRIDE")
        if membership.pinned:
            pinned = True
            reason_codes.append("USER_PINNED")

    recommended_tier = requested_tier
    if pinned and (override_tier is None or _tier_rank(override_tier) < _tier_rank(_PINNED_TIER)):
        recommended_tier = max(requested_tier, _PINNED_TIER, key=_tier_rank)
    if override_tier is not None:
        effective_tier = override_tier
        manual_override = True
    else:
        effective_tier = recommended_tier
        manual_override = False
    if requested_tier != effective_tier and not manual_override:
        reason_codes.append("PINNED_UPGRADE")
    if not reason_codes:
        reason_codes.append("REQUESTED_TIER")

    signals = dict(_DEFAULT_SIGNALS)
    if pinned:
        signals["user_relevance"] = 1.0

    row = TriageResultRow(
        paper_id=paper_id,
        recommended_tier=recommended_tier,
        effective_tier=effective_tier,
        manual_override=manual_override,
        signals=signals,
        reason_codes=reason_codes,
        created_by_run_id=run_id,
    )
    session.add(row)
    if run_id is None:
        # Triage without a workflow run still records who decided.
        from datetime import UTC, datetime

        from paperintel.database.models import AnalysisRunRow
        from paperintel.schemas.enums import TaskState

        triage_run_id = new_run_id()
        now = datetime.now(UTC)
        session.add(
            AnalysisRunRow(
                run_id=triage_run_id,
                paper_id=paper_id,
                agent_type="triage",
                pipeline_version=PIPELINE_VERSION,
                config_hash=analysis_config_hash(stage="triage"),
                model_id="none",
                provider_id="prv_none",
                status=TaskState.SUCCEEDED,
                started_at=now,
                finished_at=now,
            )
        )
        row.created_by_run_id = triage_run_id
    session.flush()
    return row


def latest_triage(session: Session, paper_id: str) -> TriageResultRow | None:
    """The most recent triage decision for a paper (plan_job check)."""
    return session.scalars(
        select(TriageResultRow)
        .where(TriageResultRow.paper_id == paper_id)
        .order_by(TriageResultRow.created_at.desc(), TriageResultRow.triage_id.desc())
    ).first()


def _tier_rank(tier: ResourceTier) -> int:
    from paperintel.schemas.enums import TIER_ORDER

    return TIER_ORDER.index(tier)

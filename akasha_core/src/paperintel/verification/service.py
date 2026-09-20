"""verification.service — the verification runner (P08, doc 05 P08).

Per claim: run the tier's applicable verifier set, persist one
VerificationRow per (claim, verifier) — append-only, never overwritten —
then apply the support-state transition from the recorded verdicts.

Every verification pass creates its own AnalysisRunRow (audit trail), so
"which verification pass supported this claim, and why" is always
answerable (doc 00 §7.16).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.config.fingerprint import analysis_config_hash
from paperintel.database.models import AnalysisRunRow, ClaimRow, VerificationRow
from paperintel.errors import DomainError
from paperintel.ids import new_run_id, new_verification_id
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import ResourceTier, SupportState, TaskState, VerifierType
from paperintel.triage.service import latest_triage
from paperintel.verification.base import TIER_VERIFIERS, run_verifier
from paperintel.verification.support import support_state_for
from paperintel.version import PIPELINE_VERSION


@dataclass(slots=True)
class ClaimVerification:
    claim_id: str
    support_state: str
    verdicts: dict[str, str] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VerificationReport:
    run_id: str
    effective_tier: str
    claims_verified: int
    #: support state → count
    states: dict[str, int] = field(default_factory=dict)
    claims: list[ClaimVerification] = field(default_factory=list)
    #: Claims whose state changed from UNVERIFIED (informational).
    transitions: int = 0


def verifiers_for_tier(tier: ResourceTier) -> tuple[VerifierType, ...]:
    """Applicable verifier set for a tier (doc 01 §10). T3 = complete
    applicable set."""
    try:
        return TIER_VERIFIERS[tier]
    except KeyError as exc:  # pragma: no cover - enum is frozen
        raise DomainError(
            "CFG_002",
            message=f"No verifier set defined for tier {tier.value}",
            details={"tier": tier.value},
        ) from exc


def run_verification(
    session: Session,
    *,
    paper_version_id: str,
    tier: ResourceTier | None = None,
    claim_ids: list[str] | None = None,
) -> VerificationReport:
    """Verify the version's claims at the effective tier.

    ``tier`` defaults to the paper's effective TRIAGE tier (P07): resource
    policy decides verification depth, never an ad-hoc default.
    """
    if tier is None:
        tier = _effective_tier(session, paper_version_id)

    verifier_types = verifiers_for_tier(tier)
    if not verifier_types:
        raise DomainError(
            "CFG_002",
            message=f"Tier {tier.value} has no applicable verifiers.",
            details={"tier": tier.value},
        )

    stmt = select(ClaimRow).where(ClaimRow.paper_version_id == paper_version_id)
    if claim_ids is not None:
        stmt = stmt.where(ClaimRow.claim_id.in_(claim_ids))
    claims = list(session.scalars(
        stmt.order_by(ClaimRow.created_at, ClaimRow.claim_id)
        .with_for_update().execution_options(populate_existing=True)
    ))
    if not claims:
        # No claims is a legitimate outcome (nothing to verify) — the run
        # is still recorded so the stage is explainable.
        run_id = _create_verification_run(session, paper_version_id, tier, 0)
        return VerificationReport(
            run_id=run_id,
            effective_tier=tier.value,
            claims_verified=0,
            states={},
            claims=[],
            transitions=0,
        )

    run_id = _create_verification_run(session, paper_version_id, tier, len(claims))
    report = VerificationReport(
        run_id=run_id, effective_tier=tier.value, claims_verified=len(claims)
    )

    for claim in claims:
        verdicts: dict[VerifierType, str] = {}
        reasons: list[str] = []
        for verifier_type in verifier_types:
            result = run_verifier(verifier_type, session, claim)
            session.add(
                VerificationRow(
                    verification_id=new_verification_id(),
                    claim_id=claim.claim_id,
                    verifier_type=verifier_type,
                    status="COMPLETED",
                    verdict=result.verdict,
                    reason_summary=result.reason_summary,
                    details_json=result.details,
                    created_by_run_id=run_id,
                )
            )
            verdicts[verifier_type] = result.verdict
            if result.verdict in ("FAIL", "WARN"):
                reasons.append(f"{verifier_type.value}: {result.reason_summary}")

        previous = claim.support_state
        new_state = support_state_for(verdicts)
        claim.support_state = new_state
        if previous is not new_state:
            report.transitions += 1

        state_key = new_state.value
        report.states[state_key] = report.states.get(state_key, 0) + 1
        report.claims.append(
            ClaimVerification(
                claim_id=claim.claim_id,
                support_state=state_key,
                verdicts={vt.value: verdict for vt, verdict in verdicts.items()},
                reasons=reasons,
            )
        )

    session.flush()
    return report


def latest_verdicts(session: Session, claim_id: str) -> dict[str, str]:
    """The most recent verdict per verifier for a claim (audit view)."""
    rows = session.scalars(
        select(VerificationRow)
        .where(VerificationRow.claim_id == claim_id)
        .order_by(VerificationRow.created_at, VerificationRow.verification_id)
    ).all()
    verdicts: dict[str, str] = {}
    for row in rows:
        verdicts[row.verifier_type.value] = row.verdict
    return verdicts


def _effective_tier(session: Session, paper_version_id: str) -> ResourceTier:
    """The paper's triage tier decides verification depth; when no triage
    decision exists yet, T2_FULL is the documented default (core verifier
    set) — never a silent no-op."""
    from paperintel.database.models import PaperVersionRow

    version = session.get(PaperVersionRow, paper_version_id)
    paper_id = version.paper_id if version is not None else None
    if paper_id is not None:
        triage = latest_triage(session, paper_id)
        if triage is not None:
            return triage.effective_tier
    return ResourceTier.T2_FULL


def _create_verification_run(
    session: Session, paper_version_id: str, tier: ResourceTier, claim_count: int
) -> str:
    from paperintel.database.models import PaperVersionRow

    version = session.get(PaperVersionRow, paper_version_id)
    run_id = new_run_id()
    now = utcnow()
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_id=version.paper_id if version is not None else None,
            paper_version_id=paper_version_id,
            agent_type="verification",
            pipeline_version=PIPELINE_VERSION,
            config_hash=analysis_config_hash(stage="verification", tier=tier.value),
            model_id="none",
            provider_id="prv_none",
            status=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
        )
    )
    session.flush()
    return run_id


__all__ = [
    "ClaimVerification",
    "SupportState",
    "VerificationReport",
    "latest_verdicts",
    "run_verification",
    "verifiers_for_tier",
]

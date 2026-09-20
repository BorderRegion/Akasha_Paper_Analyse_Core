"""knowledge.claims — the canonical claim ledger writer (P07, doc 03 §1.5).

Part of the KNOWLEDGE layer (doc 01 system layering: claim ledger • tags •
entities • relations • paper profile).

Persistence rule (doc 00 §7.1): no factual or evaluative claim enters the
ledger without evidence references or explicit EXTERNAL provenance. The
firewall already guarantees that upstream; the ledger enforces it AGAIN at
the persistence boundary (defense in depth) and refuses anything else.

Every persisted agent run creates its own AnalysisRunRow (audit trail);
claims are deduplicated within a run by normalized statement. Re-running
an agent on the same version creates a NEW run — earlier claims stay
(history is never rewritten; supersession is P08+ verification scope).

Claim support_state starts UNVERIFIED: verification (P08) decides
SUPPORTED/PARTIALLY_SUPPORTED/DISPUTED/UNSUPPORTED, never the agent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.agents.base import AgentRunOutcome
from paperintel.config.fingerprint import analysis_config_hash
from paperintel.database.models import (
    AnalysisRunRow,
    ClaimEvidenceRow,
    ClaimRow,
    EvidenceRow,
    ModelCallRow,
    PromptVersionRow,
)
from paperintel.errors import DomainError
from paperintel.ids import new_claim_id, new_run_id
from paperintel.schemas.agent import AgentRequest
from paperintel.schemas.claims import ClaimCandidate
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import ClaimType, EvidenceRole, SupportState, TaskState
from paperintel.version import PIPELINE_VERSION

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_statement(statement: str) -> str:
    """Casefold + whitespace-normalized statement form (dedup key)."""
    return _WHITESPACE_RE.sub(" ", statement.strip()).casefold()


@dataclass(slots=True)
class LedgerResult:
    run_id: str
    claims_created: int
    evidence_links_created: int
    #: Normalized statements that already existed in THIS run (deduped).
    duplicates_dropped: int


def persist_agent_result(
    session: Session,
    request: AgentRequest,
    outcome: AgentRunOutcome,
) -> LedgerResult:
    """Persist one agent run + its validated claims into the ledger.

    The claims in ``outcome.result`` are the FIREWALL-VALIDATED set (the
    base agent already dropped unsupported FACT candidates); the ledger
    re-checks the evidence-reference invariant before writing.
    """
    # Revalidate even model_copy/model_construct inputs at the write boundary.
    candidates = [ClaimCandidate.model_validate(c.model_dump()) for c in outcome.result.claims]
    # Validate the entire batch before staging any writes.
    for candidate in candidates:
        _require_evidence_references(candidate, request.agent_type)
        for ref in candidate.evidence:
            evidence = session.get(EvidenceRow, ref.evidence_id)
            if evidence is None:
                raise DomainError("EVIDENCE_001", message="Unknown ledger evidence reference.")
            if evidence.paper_version_id != request.paper_version_id:
                raise DomainError(
                    "EVIDENCE_002", message="Ledger evidence belongs to another version."
                )
    model_call = session.get(ModelCallRow, outcome.model_call_id) if outcome.model_call_id else None
    prompt = session.get(PromptVersionRow, model_call.prompt_version_id) if model_call else None

    run_id = outcome.run_id or new_run_id()
    now = utcnow()
    existing_run = session.get(AnalysisRunRow, run_id) if outcome.run_id else None
    session.merge(
        AnalysisRunRow(
            run_id=run_id,
            paper_id=request.paper_id,
            paper_version_id=request.paper_version_id,
            agent_type=request.agent_type,
            pipeline_version=PIPELINE_VERSION,
            prompt_version=prompt.version if prompt else (existing_run.prompt_version if existing_run else None),
            config_hash=existing_run.config_hash
            if existing_run
            else analysis_config_hash(
                agent=request.agent_type,
                provider=model_call.provider_id if model_call else None,
                model=model_call.model_id if model_call else None,
                prompt=model_call.prompt_version_id if model_call else None,
            ),
            model_id=model_call.model_id if model_call else (existing_run.model_id if existing_run else "none"),
            provider_id=model_call.provider_id if model_call else (existing_run.provider_id if existing_run else "prv_none"),
            status=_run_status_for(outcome),
            started_at=existing_run.started_at if existing_run else now,
            finished_at=now,
            trace_id=request.trace_id,
        )
    )

    seen: set[str] = set()
    claims_created = 0
    links_created = 0
    duplicates = 0

    for candidate in candidates:
        key = normalize_statement(candidate.statement)
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)

        _require_evidence_references(candidate, request.agent_type)

        claim_id = new_claim_id()
        session.add(
            ClaimRow(
                claim_id=claim_id,
                paper_id=request.paper_id,
                paper_version_id=request.paper_version_id,
                claim_type=candidate.claim_type,
                category=candidate.category,
                statement=candidate.statement,
                normalized_statement=key,
                support_state=SupportState.UNVERIFIED,
                created_by_run_id=run_id,
                pipeline_version=PIPELINE_VERSION,
                prompt_version=prompt.version if prompt else None,
            )
        )
        claims_created += 1

        for ref in candidate.evidence:
            session.add(
                ClaimEvidenceRow(
                    claim_id=claim_id,
                    evidence_id=ref.evidence_id,
                    role=ref.role,
                )
            )
            links_created += 1

    session.flush()
    if model_call is not None:
        model_call.run_id = run_id
    from paperintel.operations.tracing import record_trace_event

    record_trace_event(
        session,
        trace_id=request.trace_id,
        kind="canonical.write",
        run_id=run_id,
        task_id=request.task_id,
        model_call_id=outcome.model_call_id,
        data={"claims_created": claims_created, "evidence_links": links_created},
    )
    return LedgerResult(
        run_id=run_id,
        claims_created=claims_created,
        evidence_links_created=links_created,
        duplicates_dropped=duplicates,
    )


def claims_for_version(
    session: Session, paper_version_id: str, *, categories: set[str] | None = None
) -> list[ClaimRow]:
    """Read claims for one version (newest run per statement wins is NOT
    applied here — the caller sees the full ledger; supersession is P08)."""
    stmt = (
        select(ClaimRow)
        .where(ClaimRow.paper_version_id == paper_version_id)
        .order_by(ClaimRow.created_at, ClaimRow.claim_id)
    )
    if categories:
        stmt = stmt.where(ClaimRow.category.in_(categories))
    return list(session.scalars(stmt))


def _require_evidence_references(candidate: ClaimCandidate, agent_type: str) -> None:
    """Ledger boundary invariant (doc 00 §7.1): every claim carries
    evidence references or explicit EXTERNAL provenance."""
    if candidate.claim_type is ClaimType.EXTERNAL:
        raise DomainError(
            "CLAIM_001", message="EXTERNAL claims require a trusted metadata provenance writer."
        )
    if candidate.claim_type is ClaimType.FACT and not any(
        ref.role is EvidenceRole.SUPPORT for ref in candidate.evidence
    ):
        raise DomainError("CLAIM_001", message="FACT requires direct SUPPORT evidence.")
    if candidate.evidence:
        return
    raise DomainError(
        "CLAIM_001",
        message=(
            f"Agent {agent_type} produced a {candidate.claim_type.value} claim "
            "without evidence references — refused at the ledger boundary."
        ),
        details={"statement": candidate.statement[:200]},
    )


def _run_status_for(outcome: AgentRunOutcome) -> TaskState:
    from paperintel.schemas.enums import AgentStatus

    if outcome.result.status is AgentStatus.FAILED:
        return TaskState.FAILED
    if outcome.warnings:
        return TaskState.SUCCEEDED_WITH_WARNINGS
    return TaskState.SUCCEEDED

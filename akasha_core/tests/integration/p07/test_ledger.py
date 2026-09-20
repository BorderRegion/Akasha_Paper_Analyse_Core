"""P07 claim-ledger tests: persistence rules + provenance (doc 03 §1.5).

No factual/evaluative claim enters the ledger without evidence
references (doc 00 §7.1). Claims start UNVERIFIED; verification is P08.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.fixtures.canary import seed_canary_evidence
from tests.fixtures.generators import build_f01_native

from paperintel.database.models import AnalysisRunRow, ClaimEvidenceRow, ClaimRow
from paperintel.errors import DomainError
from paperintel.ingest.service import import_pdf
from paperintel.knowledge.claims import (
    claims_for_version,
    normalize_statement,
    persist_agent_result,
)
from paperintel.schemas.agent import AgentRequest, AgentResult
from paperintel.schemas.enums import AgentStatus, EvidenceRole, SupportState
from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def imported(session, store, data_dir, tmp_path):
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    session.commit()
    return result


def _request(paper_id: str, version_id: str) -> AgentRequest:
    return AgentRequest(
        paper_id=paper_id,
        paper_version_id=version_id,
        agent_type="agents.research_question",
    )


def _outcome_with_claims(*claims) -> object:
    """Build a minimal AgentRunOutcome carrying given claim candidates."""
    from paperintel.agents.base import AgentRunOutcome

    return AgentRunOutcome(
        result=AgentResult(status=AgentStatus.SUCCESS, claims=list(claims)),
        model_call_id=None,
        warnings=[],
    )


@pytest.mark.needs_db
def test_claims_persisted_with_evidence_links(session, imported) -> None:
    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    candidate = {
        "claim_type": "FACT",
        "category": "research.question",
        "statement": "The dataset contains 1,000 samples.",
        "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
        "uncertainties": [],
    }
    from paperintel.schemas.claims import ClaimCandidate

    outcome = _outcome_with_claims(ClaimCandidate.model_validate(candidate))

    ledger = persist_agent_result(
        session, _request(imported.paper_id, imported.paper_version_id), outcome
    )

    assert ledger.claims_created == 1
    assert ledger.evidence_links_created == 1

    claim = session.scalar(select(ClaimRow))
    assert claim.claim_id.startswith("clm_")
    assert claim.support_state is SupportState.UNVERIFIED  # P08 verifies
    assert claim.normalized_statement == normalize_statement(candidate["statement"])
    assert claim.created_by_run_id == ledger.run_id

    link = session.scalar(select(ClaimEvidenceRow))
    assert link.evidence_id == evidence_id
    assert link.role is EvidenceRole.SUPPORT

    run = session.get(AnalysisRunRow, ledger.run_id)
    assert run.agent_type == "agents.research_question"
    assert run.model_id == "none"  # no model call behind a hand-built outcome


@pytest.mark.needs_db
@pytest.mark.parametrize("roles", [
    ["CONTEXT", "SUPPORT"], ["SUPPORT", "CONTEXT"], ["SUPPORT", "SUPPORT"],
])
def test_duplicate_evidence_roles_cannot_crash_a_real_agent_stage(session, imported, roles):
    from paperintel.schemas.claims import ClaimCandidate

    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    candidate = ClaimCandidate.model_validate({
        "claim_type": "FACT", "category": "research.question",
        "statement": "A repeated evidence reference must produce one ledger link.",
        "evidence": [{"evidence_id": evidence_id, "role": role} for role in roles],
    })
    ledger = persist_agent_result(session, _request(imported.paper_id, imported.paper_version_id),
                                  _outcome_with_claims(candidate))
    assert ledger.evidence_links_created == 1
    claim = session.scalar(select(ClaimRow).where(ClaimRow.created_by_run_id == ledger.run_id))
    assert session.get(ClaimEvidenceRow, (claim.claim_id, evidence_id)).role is EvidenceRole.SUPPORT


def test_contradictory_roles_require_schema_repair():
    from pydantic import ValidationError

    from paperintel.schemas.claims import ClaimCandidate

    with pytest.raises(ValidationError, match="both support and contradict"):
        ClaimCandidate.model_validate({
            "claim_type": "FACT", "category": "research.question", "statement": "conflict",
            "evidence": [{"evidence_id": "ev_test", "role": role}
                         for role in ("SUPPORT", "COUNTER_EVIDENCE")],
        })


@pytest.mark.needs_db
def test_claim_without_evidence_refused_at_ledger_boundary(session, imported) -> None:
    """Defense in depth: even if something bypassed the firewall, the
    ledger itself refuses evidence-free non-EXTERNAL claims (doc 00 §7.1)."""
    from paperintel.schemas.claims import ClaimCandidate

    outcome = _outcome_with_claims(
        ClaimCandidate.model_validate(
            {
                "claim_type": "INFERENCE",
                "category": "research.gap",
                "statement": "The gap is obvious.",
            }
        )
    )
    with pytest.raises(DomainError) as excinfo:
        persist_agent_result(
            session, _request(imported.paper_id, imported.paper_version_id), outcome
        )
    assert excinfo.value.code == "CLAIM_001"


@pytest.mark.needs_db
def test_duplicate_statements_deduped_within_run(session, imported) -> None:
    from paperintel.schemas.claims import ClaimCandidate

    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    base = {
        "claim_type": "FACT",
        "category": "research.question",
        "statement": "The dataset contains 1,000 samples.",
        "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
    }
    outcome = _outcome_with_claims(
        ClaimCandidate.model_validate(base),
        ClaimCandidate.model_validate({**base, "statement": base["statement"].upper()}),
    )
    ledger = persist_agent_result(
        session, _request(imported.paper_id, imported.paper_version_id), outcome
    )
    assert ledger.claims_created == 1
    assert ledger.duplicates_dropped == 1


@pytest.mark.needs_db
def test_rerunning_agent_creates_new_run_not_overwrite(session, imported) -> None:
    """Re-running an agent produces a NEW run; earlier claims stay
    (history never rewritten — supersession is P08 verification scope)."""
    from paperintel.schemas.claims import ClaimCandidate

    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    candidate = ClaimCandidate.model_validate(
        {
            "claim_type": "FACT",
            "category": "research.question",
            "statement": "The dataset contains 1,000 samples.",
            "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
        }
    )
    request = _request(imported.paper_id, imported.paper_version_id)
    first = persist_agent_result(session, request, _outcome_with_claims(candidate))
    second = persist_agent_result(session, request, _outcome_with_claims(candidate))

    assert first.run_id != second.run_id
    assert session.scalar(select(func.count()).select_from(ClaimRow)) == 2
    # Both runs recorded.
    assert session.get(AnalysisRunRow, first.run_id) is not None
    assert session.get(AnalysisRunRow, second.run_id) is not None


@pytest.mark.needs_db
def test_insufficient_evidence_run_persists_no_claims(session, imported) -> None:
    outcome = _outcome_with_claims()
    outcome.result.model_copy(update={"status": AgentStatus.INSUFFICIENT_EVIDENCE})
    ledger = persist_agent_result(
        session,
        _request(imported.paper_id, imported.paper_version_id),
        _outcome_with_claims(),
    )
    assert ledger.claims_created == 0
    # The run itself IS recorded (the agent ran and found nothing).
    assert session.get(AnalysisRunRow, ledger.run_id) is not None


@pytest.mark.needs_db
def test_claims_for_version_filters_categories(session, imported) -> None:
    from paperintel.schemas.claims import ClaimCandidate

    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    outcome = _outcome_with_claims(
        ClaimCandidate.model_validate(
            {
                "claim_type": "FACT",
                "category": "experiment.dataset",
                "statement": "The dataset contains 1,000 samples.",
                "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
            }
        )
    )
    persist_agent_result(session, _request(imported.paper_id, imported.paper_version_id), outcome)
    assert len(claims_for_version(session, imported.paper_version_id)) == 1
    filtered = claims_for_version(session, imported.paper_version_id, categories={"result.main"})
    assert filtered == []

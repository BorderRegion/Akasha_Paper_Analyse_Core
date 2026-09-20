"""P08 shared fixtures and claim/evidence builders.

Kept in a non-test module so other P08 test modules can import the
builders without re-importing test-module names (F811).
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from paperintel.database.models import ClaimEvidenceRow, ClaimRow, EvidenceRow, SectionRow
from paperintel.ids import IdPrefix, new_claim_id, new_evidence_id, new_id, new_run_id
from paperintel.schemas.enums import (
    ClaimType,
    DataQualityState,
    EvidenceRole,
    EvidenceType,
    SectionClass,
    SourceMethod,
    SupportState,
)


def make_evidence(
    session: Session,
    version_id: str,
    *,
    text: str,
    section_class: SectionClass = SectionClass.RESULT,
    source_method: SourceMethod = SourceMethod.PDF_NATIVE,
    ocr_confidence: float | None = None,
    run_id: str | None = None,
) -> EvidenceRow:
    """Persist one evidence row (with its section) for a version."""
    run_id = run_id or ensure_run(session, version_id)
    section = SectionRow(
        section_id=new_id(IdPrefix.EVIDENCE).replace("ev_", "sec_"),
        paper_version_id=version_id,
        original_heading=f"{section_class.value} section",
        normalized_class=section_class,
        page_start=1,
        page_end=1,
        parent_section_id=None,
        ordinal=0,
    )
    session.add(section)
    row = EvidenceRow(
        evidence_id=new_evidence_id(),
        paper_version_id=version_id,
        evidence_type=EvidenceType.PARAGRAPH,
        page_start=1,
        page_end=1,
        section_id=section.section_id,
        text=text,
        source_method=source_method,
        ocr_confidence=ocr_confidence,
        quality_state=DataQualityState.GOOD,
        content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        extraction_run_id=run_id,
    )
    session.add(row)
    session.flush()
    return row


def make_claim(
    session: Session,
    version_id: str,
    *,
    statement: str,
    evidence_rows: list[tuple[EvidenceRow, EvidenceRole]],
    category: str = "result.main",
    claim_type: ClaimType = ClaimType.FACT,
    run_id: str | None = None,
) -> ClaimRow:
    run_id = run_id or ensure_run(session, version_id)
    claim = ClaimRow(
        claim_id=new_claim_id(),
        paper_id=_paper_id(session, version_id),
        paper_version_id=version_id,
        claim_type=claim_type,
        category=category,
        statement=statement,
        support_state=SupportState.UNVERIFIED,
        created_by_run_id=run_id,
        pipeline_version="1.0.0",
    )
    session.add(claim)
    for row, role in evidence_rows:
        session.add(
            ClaimEvidenceRow(claim_id=claim.claim_id, evidence_id=row.evidence_id, role=role)
        )
    session.flush()
    return claim


def ensure_run(session: Session, version_id: str) -> str:
    from paperintel.database.models import AnalysisRunRow
    from paperintel.schemas.common import utcnow
    from paperintel.schemas.enums import TaskState

    run_id = new_run_id()
    now = utcnow()
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_id=_paper_id(session, version_id),
            paper_version_id=version_id,
            agent_type="test.fixture",
            pipeline_version="1.0.0",
            config_hash="test",
            model_id="none",
            provider_id="prv_none",
            status=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
        )
    )
    session.flush()
    return run_id


def _paper_id(session: Session, version_id: str) -> str:
    from paperintel.database.models import PaperVersionRow

    version = session.get(PaperVersionRow, version_id)
    assert version is not None
    return version.paper_id

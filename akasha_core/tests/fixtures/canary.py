"""Shared test helpers: canary evidence seeding for mock-LLM agent runs.

The mock LLM provider (spec doc 06 §3) always answers from a FIXED canary
evidence unit. Seeding that unit into a paper version makes mock answers
fully supported end-to-end through the schema firewall (existence, scope,
numeric support) — the same guarantee a real provider gets from real
evidence.
"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from paperintel.database.models import EvidenceRow
from paperintel.ids import new_run_id
from paperintel.providers.mocks.llm import CANARY_EVIDENCE_ID, CANARY_EVIDENCE_TEXT
from paperintel.schemas.enums import (
    DataQualityState,
    EvidenceType,
    SourceMethod,
    TaskState,
)

CANARY_EVIDENCE_SHA = hashlib.sha256(CANARY_EVIDENCE_TEXT.encode()).hexdigest()


def seed_canary_evidence(session: Session, paper_version_id: str) -> str:
    """Insert the mock provider's canary evidence unit for one version.

    Idempotent: re-seeding returns the existing row's ID.
    """
    existing = session.get(EvidenceRow, CANARY_EVIDENCE_ID)
    if existing is not None:
        if existing.paper_version_id != paper_version_id:
            raise ValueError(
                "canary evidence already seeded for a different version — "
                "use a fresh database per module"
            )
        return CANARY_EVIDENCE_ID

    from datetime import UTC, datetime

    from paperintel.database.models import AnalysisRunRow

    run_id = new_run_id()
    now = datetime.now(UTC)
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_version_id=paper_version_id,
            agent_type="extraction",
            pipeline_version="test",
            config_hash="",
            model_id="none",
            provider_id="prv_none",
            status=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
        )
    )
    session.add(
        EvidenceRow(
            evidence_id=CANARY_EVIDENCE_ID,
            paper_version_id=paper_version_id,
            evidence_type=EvidenceType.PARAGRAPH,
            page_start=1,
            page_end=1,
            text=CANARY_EVIDENCE_TEXT,
            source_method=SourceMethod.PDF_NATIVE,
            quality_state=DataQualityState.GOOD,
            content_sha256=CANARY_EVIDENCE_SHA,
            extraction_run_id=run_id,
        )
    )
    session.flush()
    return CANARY_EVIDENCE_ID

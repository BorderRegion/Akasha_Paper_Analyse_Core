"""P01 canonical entity CRUD + evidence immutability enforcement."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from paperintel.database.models import (
    AnalysisRunRow,
    AssetRow,
    ClaimEvidenceRow,
    ClaimRow,
    EvidenceRow,
    JobRow,
    PaperRow,
    PaperVersionRow,
    TaskRow,
    VerificationRow,
)
from paperintel.ids import (
    new_asset_id,
    new_claim_id,
    new_evidence_id,
    new_job_id,
    new_paper_id,
    new_paper_version_id,
    new_run_id,
    new_task_id,
    new_verification_id,
)
from paperintel.schemas.enums import (
    AssetKind,
    ClaimType,
    DataQualityState,
    EvidenceRole,
    EvidenceType,
    PipelineStage,
    RetentionClass,
    SourceMethod,
    SupportState,
    TaskState,
    VerifierType,
    VerifierVerdict,
)

pytestmark = pytest.mark.needs_db

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SHA = "a" * 64


@pytest.fixture()
def paper_tree(session: Session) -> dict[str, str]:
    """Create a minimal Paper -> Asset -> PaperVersion -> Run tree."""
    paper_id = new_paper_id()
    asset_id = new_asset_id()
    pver_id = new_paper_version_id()
    run_id = new_run_id()

    session.add(
        PaperRow(
            paper_id=paper_id,
            canonical_title="Method A for Small Object Detection",
            normalized_title="method a for small object detection",
            doi="10.1000/test.2026.001",
        )
    )
    session.add(
        AssetRow(
            asset_id=asset_id,
            kind=AssetKind.PDF,
            sha256=SHA,
            storage_key=f"pdf/sha256/{SHA[:2]}/{SHA}",
            mime_type="application/pdf",
            size_bytes=1234,
            retention_class=RetentionClass.KEEP,
        )
    )
    session.flush()  # papers + assets must exist before dependent rows
    session.add(
        PaperVersionRow(
            paper_version_id=pver_id,
            paper_id=paper_id,
            version_label="arXiv v1",
            source_type="arxiv",
            asset_id=asset_id,
            page_count=12,
            is_preprint=True,
            content_sha256=SHA,
        )
    )
    session.flush()  # paper_versions must exist before analysis_runs
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_id=paper_id,
            paper_version_id=pver_id,
            agent_type="agents.method",
            pipeline_version="1.0.0",
            config_hash="b" * 64,
            model_id="mock-analyst",
            provider_id="prv_mockllm",
            status=TaskState.SUCCEEDED,
            started_at=NOW,
            finished_at=NOW,
        )
    )
    session.commit()
    return {"paper_id": paper_id, "asset_id": asset_id, "pver_id": pver_id, "run_id": run_id}


def _evidence(
    pver_id: str, run_id: str, text_value: str = "We evaluate on 1,000 samples."
) -> EvidenceRow:
    return EvidenceRow(
        evidence_id=new_evidence_id(),
        paper_version_id=pver_id,
        evidence_type=EvidenceType.PARAGRAPH,
        page_start=4,
        page_end=4,
        text=text_value,
        source_method=SourceMethod.PDF_NATIVE,
        quality_state=DataQualityState.GOOD,
        content_sha256="c" * 64,
        extraction_run_id=run_id,
        created_at=NOW,
    )


def test_create_read_update_allowed_entities(session: Session, paper_tree: dict) -> None:
    """Create/read/update on mutable entities works (papers are mutable
    identity records; evidence is not part of this test)."""
    paper = session.get(PaperRow, paper_tree["paper_id"])
    assert paper is not None
    assert paper.canonical_title == "Method A for Small Object Detection"

    paper.canonical_title = "Method A for Small Object Detection (rev)"
    session.commit()
    refreshed = session.get(PaperRow, paper_tree["paper_id"])
    assert refreshed is not None
    assert refreshed.canonical_title.endswith("(rev)")
    assert refreshed.updated_at >= refreshed.created_at

    version = session.get(PaperVersionRow, paper_tree["pver_id"])
    assert version is not None
    assert version.paper.canonical_title.endswith("(rev)")  # relationship loads


def test_evidence_insert_and_read(session: Session, paper_tree: dict) -> None:
    evidence = _evidence(paper_tree["pver_id"], paper_tree["run_id"])
    session.add(evidence)
    session.commit()
    loaded = session.get(EvidenceRow, evidence.evidence_id)
    assert loaded is not None
    assert loaded.text == "We evaluate on 1,000 samples."
    assert loaded.source_method is SourceMethod.PDF_NATIVE


def test_evidence_content_update_blocked_by_trigger(session: Session, paper_tree: dict) -> None:
    evidence = _evidence(paper_tree["pver_id"], paper_tree["run_id"])
    session.add(evidence)
    session.commit()

    with pytest.raises(ProgrammingError) as excinfo:
        session.execute(
            text("UPDATE evidence SET text = 'tampered' WHERE evidence_id = :eid"),
            {"eid": evidence.evidence_id},
        )
    session.rollback()
    assert "EVIDENCE_IMMUTABLE" in str(excinfo.value)

    # Value unchanged after rollback.
    loaded = session.get(EvidenceRow, evidence.evidence_id)
    assert loaded is not None
    assert loaded.text == "We evaluate on 1,000 samples."


def test_evidence_hash_and_page_update_blocked(session: Session, paper_tree: dict) -> None:
    evidence = _evidence(paper_tree["pver_id"], paper_tree["run_id"])
    session.add(evidence)
    session.commit()

    for column, value in (
        ("content_sha256", "'" + "d" * 64 + "'"),
        ("page_start", "9"),
        ("source_method", "'OCR'"),
    ):
        with pytest.raises(ProgrammingError) as excinfo:
            session.execute(
                text(f"UPDATE evidence SET {column} = {value} WHERE evidence_id = :eid"),  # noqa: S608
                {"eid": evidence.evidence_id},
            )
        session.rollback()
        assert "EVIDENCE_IMMUTABLE" in str(excinfo.value)


def test_evidence_delete_blocked(session: Session, paper_tree: dict) -> None:
    evidence = _evidence(paper_tree["pver_id"], paper_tree["run_id"])
    session.add(evidence)
    session.commit()
    with pytest.raises(ProgrammingError) as excinfo:
        session.execute(
            text("DELETE FROM evidence WHERE evidence_id = :eid"),
            {"eid": evidence.evidence_id},
        )
    session.rollback()
    assert "EVIDENCE_IMMUTABLE" in str(excinfo.value)


def test_evidence_section_and_quality_relink_allowed(session: Session, paper_tree: dict) -> None:
    """Structure linking (section_id) and quality re-assessment are metadata
    operations, not content mutations (documented trigger policy)."""
    evidence = _evidence(paper_tree["pver_id"], paper_tree["run_id"])
    session.add(evidence)
    session.commit()
    session.execute(
        text(
            "UPDATE evidence SET section_id = 'sec_x1', quality_state = 'DEGRADED' "
            "WHERE evidence_id = :eid"
        ),
        {"eid": evidence.evidence_id},
    )
    session.commit()
    loaded = session.get(EvidenceRow, evidence.evidence_id)
    assert loaded is not None
    session.refresh(loaded)  # raw SQL bypassed the identity map
    assert loaded.section_id == "sec_x1"
    assert loaded.quality_state is DataQualityState.DEGRADED


def test_evidence_supersession_creates_new_row(session: Session, paper_tree: dict) -> None:
    """Correction = NEW evidence row with supersedes linkage; the old row is
    untouched (spec doc 03 §1.4)."""
    original = _evidence(paper_tree["pver_id"], paper_tree["run_id"], text_value="825 samples")
    session.add(original)
    session.commit()

    correction = _evidence(
        paper_tree["pver_id"],
        paper_tree["run_id"],
        text_value="1,000 samples (OCR digit corrected)",
    )
    correction.supersedes_evidence_id = original.evidence_id
    correction.source_method = SourceMethod.OCR
    correction.ocr_confidence = 0.93
    session.add(correction)
    session.commit()

    assert session.get(EvidenceRow, original.evidence_id).text == "825 samples"
    assert (
        session.get(EvidenceRow, correction.evidence_id).supersedes_evidence_id
        == original.evidence_id
    )


def test_claim_with_evidence_links_and_verification(session: Session, paper_tree: dict) -> None:
    evidence = _evidence(paper_tree["pver_id"], paper_tree["run_id"])
    session.add(evidence)
    session.flush()  # evidence must exist before claim_evidence links
    claim_id = new_claim_id()
    session.add(
        ClaimRow(
            claim_id=claim_id,
            paper_id=paper_tree["paper_id"],
            paper_version_id=paper_tree["pver_id"],
            claim_type=ClaimType.FACT,
            category="dataset_size",
            statement="The dataset contains 1,000 samples.",
            support_state=SupportState.UNVERIFIED,
            created_by_run_id=paper_tree["run_id"],
            pipeline_version="1.0.0",
            created_at=NOW,
        )
    )
    session.add(
        ClaimEvidenceRow(
            claim_id=claim_id, evidence_id=evidence.evidence_id, role=EvidenceRole.SUPPORT
        )
    )
    session.add(
        VerificationRow(
            verification_id=new_verification_id(),
            claim_id=claim_id,
            verifier_type=VerifierType.NUMERIC,
            status="completed",
            verdict=VerifierVerdict.PASS,
            reason_summary="1,000 matches evidence text",
            created_by_run_id=paper_tree["run_id"],
            created_at=NOW,
        )
    )
    session.commit()

    claim = session.get(ClaimRow, claim_id)
    assert claim is not None
    assert len(claim.evidence_links) == 1
    assert claim.evidence_links[0].role is EvidenceRole.SUPPORT
    assert claim.verifications[0].verdict is VerifierVerdict.PASS


def test_asset_dedup_unique_sha256(session: Session, paper_tree: dict) -> None:
    duplicate = AssetRow(
        asset_id=new_asset_id(),
        kind=AssetKind.PDF,
        sha256=SHA,  # same content as the fixture asset
        storage_key=f"pdf/sha256/{SHA[:2]}/{'e' * 64}",
        mime_type="application/pdf",
        size_bytes=999,
        retention_class=RetentionClass.KEEP,
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_task_idempotency_key_unique(session: Session, paper_tree: dict) -> None:
    job_id = new_job_id()
    session.add(
        JobRow(
            job_id=job_id,
            paper_id=paper_tree["paper_id"],
            paper_version_id=paper_tree["pver_id"],
            requested_tier="T2_FULL",
            effective_tier="T2_FULL",
            state=TaskState.RUNNING,
            current_stage=PipelineStage.EXTRACTED,
        )
    )
    key = f"extraction.native|{paper_tree['pver_id']}|1.0.0|cfg|scope"
    session.add(
        TaskRow(
            task_id=new_task_id(),
            job_id=job_id,
            task_type="extraction.native",
            module_id="extraction.pdf",
            state=TaskState.SUCCEEDED,
            priority=0,
            idempotency_key=key,
            input_manifest={},
            created_at=NOW,
        )
    )
    session.commit()
    session.add(
        TaskRow(
            task_id=new_task_id(),
            job_id=job_id,
            task_type="extraction.native",
            module_id="extraction.pdf",
            state=TaskState.PENDING,
            priority=0,
            idempotency_key=key,  # duplicate canonical output guard
            input_manifest={},
            created_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_execution_state_and_quality_are_separate_columns(
    session: Session, paper_tree: dict
) -> None:
    """doc 02 §10: SUCCEEDED_WITH_WARNINGS + DEGRADED must coexist."""
    job_id = new_job_id()
    session.add(
        JobRow(
            job_id=job_id,
            paper_id=paper_tree["paper_id"],
            paper_version_id=paper_tree["pver_id"],
            requested_tier="T1_SCAN",
            effective_tier="T1_SCAN",
            state=TaskState.SUCCEEDED,
            current_stage=PipelineStage.SEARCH_INDEXED,
        )
    )
    session.add(
        TaskRow(
            task_id=new_task_id(),
            job_id=job_id,
            task_type="extraction.ocr",
            module_id="ocr.primary",
            state=TaskState.SUCCEEDED_WITH_WARNINGS,
            quality_state=DataQualityState.DEGRADED,
            priority=0,
            idempotency_key=f"ocr|{job_id}|p1",
            input_manifest={"page": 1},
            error_code="OCR_003",
            created_at=NOW,
        )
    )
    session.commit()
    task = session.query(TaskRow).filter_by(idempotency_key=f"ocr|{job_id}|p1").one()
    assert task.state is TaskState.SUCCEEDED_WITH_WARNINGS
    assert task.quality_state is DataQualityState.DEGRADED

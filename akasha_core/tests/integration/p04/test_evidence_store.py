"""P04 evidence store integration tests (disposable DB + tmp object store).

Gate requirements (doc 05 P04): evidence cannot be silently mutated;
supersession works; evidence IDs resolve; section tree stable; invalid
page/bbox rejected; extraction-to-evidence idempotency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from tests.fixtures.generators import build_f01_native, build_f02_scanned, build_f04_rich

from paperintel.database.models import (
    AnalysisRunRow,
    EvidenceRow,
    PaperVersionRow,
    SectionRow,
)
from paperintel.errors import DomainError
from paperintel.evidence import retrieval
from paperintel.evidence.store import persist_extraction, record_correction
from paperintel.ids import new_run_id
from paperintel.ingest.service import import_pdf
from paperintel.providers.mocks.ocr import MockOCRProvider
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import (
    DataQualityState,
    EvidenceType,
    PageMode,
    SectionClass,
    SourceMethod,
)
from paperintel.schemas.extraction import (
    ExtractionReport,
    PageClassification,
    PageExtraction,
    QualityChecks,
    TextQualityMetrics,
)
from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def run(coro):
    return asyncio.run(coro)


def import_f01(session, store, data_dir, **kwargs):
    path = data_dir.parent / "f01.pdf"
    path.write_bytes(build_f01_native())
    return run(import_pdf(path, session=session, store=store, data_dir=data_dir, **kwargs))


def evidence_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(EvidenceRow)) or 0


# ---------------------------------------------------------------------------
# persistence + idempotency
# ---------------------------------------------------------------------------


def test_import_persists_evidence_and_sections(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))

    assert result.evidence is not None
    assert result.evidence.evidence_created > 0
    assert result.evidence.sections_created > 0
    assert result.evidence.analysis_run_created is True
    assert result.evidence.excluded == {}
    assert result.evidence.warnings == []

    assert evidence_count(session) == result.evidence.evidence_created
    sections = session.scalars(select(SectionRow)).all()
    assert len(sections) == result.evidence.sections_created
    # Every section references the imported version; tree fields populated.
    for section in sections:
        assert section.paper_version_id == result.paper_version_id
        assert section.page_start >= 1
        assert section.page_end >= section.page_start
    classes = {s.normalized_class for s in sections}
    assert SectionClass.OTHER in classes  # Unknown headings are not fabricated titles.
    assert SectionClass.REFERENCES in classes
    # The extraction run is recorded and resolvable (frozen contract field).
    run_row = session.get(AnalysisRunRow, result.report.run_id)
    assert run_row is not None
    assert run_row.agent_type == "extraction"
    # Evidence rows link to sections and carry provenance.
    rows = session.scalars(select(EvidenceRow)).all()
    assert all(r.extraction_run_id == result.report.run_id for r in rows)
    assert all(r.paper_version_id == result.paper_version_id for r in rows)
    assert all(r.section_id is not None for r in rows)
    assert all(r.source_method is SourceMethod.PDF_NATIVE for r in rows)


def test_persist_extraction_is_idempotent(session, store, data_dir, tmp_path) -> None:
    """Persisting the same report twice (or re-persisting after re-import)
    creates nothing new — identical identity keys reuse rows."""
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    first = result.evidence
    assert first is not None

    second = persist_extraction(session, result.report)
    assert second.evidence_created == 0
    assert second.evidence_reused == first.evidence_created
    assert second.sections_reused is True
    assert second.sections_created == 0
    assert second.analysis_run_created is False  # run row reused too
    assert evidence_count(session) == first.evidence_created


def test_reextraction_same_version_does_not_duplicate(session, store, data_dir, tmp_path) -> None:
    """A re-extraction (new run id) of the same version must reuse evidence
    rows — identity keys do NOT include the run id."""
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    first = result.evidence
    assert first is not None

    reextracted = result.report.model_copy(
        update={"run_id": new_run_id(), "paper_version_id": result.paper_version_id}
    )
    second = persist_extraction(session, reextracted)
    assert second.evidence_created == 0
    assert second.evidence_reused == first.evidence_created
    assert second.analysis_run_created is True  # new run row for new run id
    assert evidence_count(session) == first.evidence_created


def test_dedup_reimport_selfheals_missing_evidence(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    first = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    assert first.evidence is not None

    # Simulate an interrupted store: delete all evidence rows (direct SQL —
    # the trigger forbids it, so drop+recreate the trigger in the test).
    session.execute(text("DROP TRIGGER evidence_immutable_guard ON evidence"))
    session.execute(text("DELETE FROM evidence"))
    session.execute(
        text(
            "CREATE TRIGGER evidence_immutable_guard BEFORE UPDATE OR DELETE ON evidence "
            "FOR EACH ROW EXECUTE FUNCTION evidence_immutable_guard()"
        )
    )
    session.flush()
    assert evidence_count(session) == 0

    again = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    assert again.deduplicated is True
    assert again.evidence is not None
    assert again.evidence.evidence_created == first.evidence.evidence_created
    assert evidence_count(session) == first.evidence.evidence_created


# ---------------------------------------------------------------------------
# exclusions: invalid locators + contract violations
# ---------------------------------------------------------------------------


def test_invalid_page_units_are_rejected(session, store, data_dir, tmp_path) -> None:
    """Units pointing outside the document's page range never become
    evidence rows — rejected BY REASON (invalid_page)."""
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))

    tampered_pages = []
    for page in result.report.pages:
        units = []
        for unit in page.units:
            if unit.unit_type is EvidenceType.PARAGRAPH and unit.page_number == 2:
                unit = unit.model_copy(update={"page_number": 99})
            units.append(unit)
        tampered_pages.append(page.model_copy(update={"units": units}))
    tampered = result.report.model_copy(
        update={"pages": tampered_pages, "paper_version_id": result.paper_version_id}
    )

    with pytest.raises(DomainError) as exc:
        persist_extraction(session, tampered)
    assert exc.value.code == "EXTRACT_001"
    assert exc.value.details["sanity"] == "FAIL"
    # No row was created at page 99.
    rows = session.scalars(select(EvidenceRow).where(EvidenceRow.page_start == 99)).all()
    assert rows == []


class _PlainTranscriptionOCR(MockOCRProvider):
    """Simulates a plain-text transcription backend: real text, NO
    confidence (e.g. PaddleOCR-VL served as plain text)."""

    async def recognize_page(self, image, request):  # noqa: ARG002
        from paperintel.providers.base import OcrLine, OcrPageResult  # noqa: PLC0415

        return OcrPageResult(
            lines=[
                OcrLine(text="OCR line one from the scanned page.", confidence=None),
                OcrLine(text="OCR line two from the scanned page.", confidence=None),
            ],
            full_text="OCR line one from the scanned page.\nOCR line two from the scanned page.",
            mean_confidence=None,
            warnings=["OCR_CONFIDENCE_UNAVAILABLE"],
            provider_id=self.provider_id,
        )


def test_ocr_without_confidence_is_excluded(session, store, data_dir, tmp_path) -> None:
    """Confidence-less OCR units (plain_text backends) cannot become
    canonical evidence — the frozen contract requires ocr_confidence."""
    path = tmp_path / "f02.pdf"
    path.write_bytes(build_f02_scanned())
    result = run(
        import_pdf(
            path,
            session=session,
            store=store,
            data_dir=data_dir,
            ocr_provider=_PlainTranscriptionOCR("prv_plain_text"),
        )
    )
    assert result.evidence is not None
    assert result.evidence.excluded.get("ocr_confidence_missing", 0) > 0
    assert any("confidence" in w for w in result.evidence.warnings)
    # Whatever persisted OCR evidence carries confidence (contract-valid).
    ocr_rows = session.scalars(
        select(EvidenceRow).where(EvidenceRow.source_method == SourceMethod.OCR)
    ).all()
    assert all(row.ocr_confidence is not None for row in ocr_rows)


# ---------------------------------------------------------------------------
# immutability at the service level + supersession
# ---------------------------------------------------------------------------


def test_persisted_evidence_cannot_be_silently_mutated(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    row = session.scalars(select(EvidenceRow)).first()
    assert row is not None
    original_text = row.text
    session.commit()  # stabilize the savepoint before the rejected DML

    with pytest.raises(Exception) as excinfo:  # noqa: B017 - trigger raises DB error
        session.execute(
            text("UPDATE evidence SET text = 'tampered' WHERE evidence_id = :eid"),
            {"eid": row.evidence_id},
        )
    session.rollback()
    assert "EVIDENCE_IMMUTABLE" in str(excinfo.value)
    reloaded = session.get(EvidenceRow, row.evidence_id)
    assert reloaded is not None
    assert reloaded.text == original_text


def test_supersession_creates_new_row_and_links(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    original = session.scalars(select(EvidenceRow).where(EvidenceRow.text.is_not(None))).first()
    assert original is not None

    corrected = record_correction(
        session,
        original.evidence_id,
        corrected_text="Corrected transcription of the same region.",
    )
    assert corrected.evidence_id != original.evidence_id
    assert corrected.supersedes_evidence_id == original.evidence_id
    assert corrected.text == "Corrected transcription of the same region."
    # Original untouched.
    untouched = session.get(EvidenceRow, original.evidence_id)
    assert untouched is not None
    assert untouched.text == original.text

    # Active listing excludes the superseded row.
    active = retrieval.list_evidence(session, result.paper_version_id, include_superseded=False)
    assert original.evidence_id not in {row.evidence_id for row in active}
    assert corrected.evidence_id in {row.evidence_id for row in active}


def test_correction_requires_a_change_and_validates_contract(
    session, store, data_dir, tmp_path
) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    original = session.scalars(select(EvidenceRow)).first()
    assert original is not None
    with pytest.raises(ValueError):
        record_correction(session, original.evidence_id)
    with pytest.raises(DomainError) as excinfo:
        record_correction(session, "ev_UNKNOWN", corrected_text="x")
    assert excinfo.value.code == "EVIDENCE_001"


# ---------------------------------------------------------------------------
# retrieval contracts
# ---------------------------------------------------------------------------


def test_evidence_ids_resolve_or_fail_loudly(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    row = session.scalars(select(EvidenceRow)).first()
    assert row is not None

    resolved = retrieval.get_evidence(session, row.evidence_id)
    assert resolved.evidence_id == row.evidence_id
    with pytest.raises(DomainError) as excinfo:
        retrieval.get_evidence(session, "ev_01UNKNOWN000000000000000000")
    assert excinfo.value.code == "EVIDENCE_001"

    # Scope enforcement.
    in_scope = retrieval.get_evidence_in_scope(session, row.evidence_id, result.paper_version_id)
    assert in_scope.evidence_id == row.evidence_id
    with pytest.raises(DomainError) as scope_exc:
        retrieval.get_evidence_in_scope(session, row.evidence_id, "pver_OTHER000000000000000000")
    assert scope_exc.value.code == "EVIDENCE_002"


def test_contract_materialization_and_list_filters(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f04.pdf"
    path.write_bytes(build_f04_rich())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))

    rows = retrieval.list_evidence(session, result.paper_version_id)
    assert rows
    for row in rows:
        contract = retrieval.evidence_to_contract(row)
        assert contract.evidence_id == row.evidence_id
        assert contract.extraction_run_id == result.report.run_id
        if row.source_method is SourceMethod.OCR:
            assert contract.ocr_confidence is not None

    # Type filter.
    tables = retrieval.list_evidence(
        session, result.paper_version_id, evidence_types={EvidenceType.TABLE}
    )
    assert tables and all(r.evidence_type is EvidenceType.TABLE for r in tables)
    # Page filter.
    page1 = retrieval.list_evidence(session, result.paper_version_id, pages={1})
    assert page1 and all(r.page_start == 1 for r in page1)
    # Section filter resolves to a real section id.
    section = session.scalars(select(SectionRow)).first()
    scoped = retrieval.list_evidence(
        session, result.paper_version_id, section_id=section.section_id
    )
    assert all(r.section_id == section.section_id for r in scoped)
    # Quality filter.
    good = retrieval.list_evidence(
        session, result.paper_version_id, quality_states={DataQualityState.GOOD}
    )
    assert good and all(r.quality_state is DataQualityState.GOOD for r in good)


def test_section_tree_nested_and_counted(session, store, data_dir, tmp_path) -> None:
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))

    tree = retrieval.section_tree(session, result.paper_version_id)
    assert tree
    flat = []

    def visit(node) -> None:
        flat.append(node)
        for child in node.children:
            assert child.parent_section_id == node.section_id
            assert child.level == node.level + 1
            visit(child)

    for root in tree:
        assert root.parent_section_id is None
        visit(root)
    assert all(node.evidence_count >= 0 for node in flat)
    assert sum(node.evidence_count for node in flat) <= evidence_count(session)
    # Stable ordering: root ordinals strictly increasing.
    assert [root.ordinal for root in tree] == sorted(root.ordinal for root in tree)
    # version summary counts every row exactly once.
    version_row = session.get(PaperVersionRow, result.paper_version_id)
    summary = retrieval.version_summary(session, version_row)
    assert summary["evidence_total"] == evidence_count(session)


def test_report_without_version_id_is_rejected(session) -> None:
    page = PageExtraction(
        page_number=1,
        classification=PageClassification(
            page_number=1,
            mode=PageMode.NATIVE_TEXT,
            needs_ocr=False,
            metrics=TextQualityMetrics(
                char_count=1,
                visible_char_ratio=1.0,
                replacement_char_ratio=0.0,
                text_density=0.01,
                repeated_glyph_ratio=0.0,
                image_area_ratio=0.0,
                bbox_sane=True,
            ),
            reasons=["forced"],
        ),
        units=[],
        page_source_method=SourceMethod.PDF_NATIVE,
    )
    report = ExtractionReport(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        page_count=1,
        pages=[page],
        quality_state=DataQualityState.GOOD,
        quality_checks=QualityChecks(
            zero_page_detected=False,
            suspiciously_empty_text=True,
            ocr_confidence_warnings=0,
            page_count_consistent=True,
            location_sanity=True,
        ),
    )
    with pytest.raises(ValueError):
        persist_extraction(session, report)


# ---------------------------------------------------------------------------
# real paper smoke: sections + evidence on k3
# ---------------------------------------------------------------------------



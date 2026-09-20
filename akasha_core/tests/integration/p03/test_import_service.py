"""P03 import service integration tests (disposable DB + tmp object store).

Covers: fingerprint/dedup, identity resolution, version labels, crop assets,
OCR fallback honesty (mock provider), EXTRACT_001 paths leaving no partial
state, and content-addressed report caching.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.fixtures.generators import (
    build_f01_native,
    build_f02_scanned,
    build_f03_mixed,
    build_f04_rich,
    build_f05_corrupt,
    build_f05_zero_page,
    build_f06_truth,
)

from paperintel.database.models import AssetRow, PaperRow, PaperVersionRow
from paperintel.errors import DomainError
from paperintel.ids import IdPrefix
from paperintel.ingest.service import import_pdf
from paperintel.providers.mocks.ocr import MockOCRProvider
from paperintel.schemas.enums import AssetKind, DataQualityState, SourceMethod
from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def write_pdf(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def run(coro):
    return asyncio.run(coro)


def count(session: Session, model) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# ---------------------------------------------------------------------------
# happy path + persistence
# ---------------------------------------------------------------------------


def test_import_f01_creates_rows_store_object_and_report(
    tmp_path, session, store, data_dir
) -> None:
    path = write_pdf(tmp_path, "f01.pdf", build_f01_native())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    assert result.deduplicated is False
    assert result.paper_id.startswith(IdPrefix.PAPER.value)
    assert result.paper_version_id.startswith(IdPrefix.PAPER_VERSION.value)
    assert result.asset_id.startswith(IdPrefix.ASSET.value)
    assert result.version_label == "v1"
    assert result.report.page_count == 2
    assert result.report.quality_state is DataQualityState.GOOD
    assert result.report.paper_version_id == result.paper_version_id
    # Rows persisted (flushed inside the test transaction).
    assert count(session, PaperRow) == 1
    assert count(session, PaperVersionRow) == 1
    assert count(session, AssetRow) == 1  # F01 has no figures/tables → no crops
    paper = session.scalar(select(PaperRow))
    assert paper.canonical_title == "Native Extraction: A Clean Digital Paper"
    version = session.scalar(select(PaperVersionRow))
    assert version.page_count == 2
    assert len(version.content_sha256) == 64
    # Canonical PDF object stored under the content-addressed key.
    asset = session.scalar(select(AssetRow))
    assert asset.kind is AssetKind.PDF
    assert store.exists(asset.storage_key)
    # Report cache written at the content-addressed path.
    report_path = Path(result.report_path)
    assert report_path.is_file()
    assert report_path.parent == data_dir / "cache" / "extraction_reports"
    assert report_path.stem == version.content_sha256
    cached = json.loads(report_path.read_text())
    assert cached["run_id"] == result.report.run_id


def test_reimport_identical_bytes_deduplicates(tmp_path, session, store, data_dir) -> None:
    data = build_f01_native()
    path_a = write_pdf(tmp_path, "a.pdf", data)
    path_b = write_pdf(tmp_path, "b_copy.pdf", data)
    first = run(import_pdf(path_a, session=session, store=store, data_dir=data_dir))
    second = run(import_pdf(path_b, session=session, store=store, data_dir=data_dir))
    assert second.deduplicated is True
    assert second.reused_asset is True
    assert second.paper_id == first.paper_id
    assert second.paper_version_id == first.paper_version_id
    assert second.asset_id == first.asset_id
    assert second.version_label == first.version_label
    # Cached report is reused verbatim (same run_id, not recomputed).
    assert second.report.run_id == first.report.run_id
    # No duplicate rows despite a different filename.
    assert count(session, PaperRow) == 1
    assert count(session, PaperVersionRow) == 1
    assert count(session, AssetRow) == 1


def test_new_content_same_title_creates_new_version(tmp_path, session, store, data_dir) -> None:
    run(
        import_pdf(
            write_pdf(tmp_path, "f01.pdf", build_f01_native()),
            session=session,
            store=store,
            data_dir=data_dir,
            title="The Same Work",
        )
    )
    second = run(
        import_pdf(
            write_pdf(tmp_path, "f06.pdf", build_f06_truth()),
            session=session,
            store=store,
            data_dir=data_dir,
            title="The Same Work",
        )
    )
    assert second.deduplicated is False
    assert second.version_label == "v2"
    assert count(session, PaperRow) == 1  # same intellectual work
    assert count(session, PaperVersionRow) == 2  # versions never overwrite
    assert count(session, AssetRow) == 2 + _crop_asset_count(session, second)


def _crop_asset_count(session: Session, result) -> int:
    """Crops created for the result's report (FIGURE/TABLE assets)."""
    crop_hashes = {
        unit.asset_sha256
        for page in result.report.pages
        for unit in page.units
        if unit.asset_sha256
    }
    # The PDF asset itself is not a crop.
    pdf_asset = session.scalar(select(AssetRow).where(AssetRow.asset_id == result.asset_id))
    crop_hashes.discard(pdf_asset.sha256)
    return len(crop_hashes)


def test_doi_identity_match_and_enrichment(tmp_path, session, store, data_dir) -> None:
    first = run(
        import_pdf(
            write_pdf(tmp_path, "f01.pdf", build_f01_native()),
            session=session,
            store=store,
            data_dir=data_dir,
        )
    )
    paper = session.scalar(select(PaperRow))
    assert paper.doi is None
    second = run(
        import_pdf(
            write_pdf(tmp_path, "f06.pdf", build_f06_truth()),
            session=session,
            store=store,
            data_dir=data_dir,
            title=paper.canonical_title,  # same work identity
            doi="10.1234/paperintel.test",
        )
    )
    assert second.paper_id == first.paper_id
    session.refresh(paper)
    assert paper.doi == "10.1234/paperintel.test"  # enriched, not overwritten


def test_explicit_duplicate_version_label_rejected(tmp_path, session, store, data_dir) -> None:
    run(
        import_pdf(
            write_pdf(tmp_path, "f01.pdf", build_f01_native()),
            session=session,
            store=store,
            data_dir=data_dir,
            title="Label Clash Work",
            version_label="camera-ready",
        )
    )
    with pytest.raises(DomainError) as excinfo:
        run(
            import_pdf(
                write_pdf(tmp_path, "f06.pdf", build_f06_truth()),
                session=session,
                store=store,
                data_dir=data_dir,
                title="Label Clash Work",
                version_label="camera-ready",
            )
        )
    assert excinfo.value.code == "CFG_002"


# ---------------------------------------------------------------------------
# failure paths — no partial state
# ---------------------------------------------------------------------------


def test_zero_page_pdf_raises_extract001_and_persists_nothing(
    tmp_path, session, store, data_dir
) -> None:
    path = write_pdf(tmp_path, "zero.pdf", build_f05_zero_page())
    with pytest.raises(DomainError) as excinfo:
        run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    assert excinfo.value.code == "EXTRACT_001"
    assert count(session, PaperRow) == 0
    assert count(session, PaperVersionRow) == 0
    assert count(session, AssetRow) == 0
    # The diagnostic report is still written (cache path, no rows involved).
    report_path = Path(excinfo.value.details["report_path"])
    assert report_path.is_file()
    assert json.loads(report_path.read_text())["quality_checks"]["zero_page_detected"]


def test_corrupt_pdf_raises_pdf001(tmp_path, session, store, data_dir) -> None:
    path = write_pdf(tmp_path, "corrupt.pdf", build_f05_corrupt())
    with pytest.raises(DomainError) as excinfo:
        run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    assert excinfo.value.code == "PDF_001"
    assert count(session, PaperRow) == 0


def test_missing_file_raises_pdf001(tmp_path, session, store, data_dir) -> None:
    with pytest.raises(DomainError) as excinfo:
        run(
            import_pdf(
                tmp_path / "does_not_exist.pdf",
                session=session,
                store=store,
                data_dir=data_dir,
            )
        )
    assert excinfo.value.code == "PDF_001"


def test_scanned_without_ocr_fails_loudly_no_silent_fallback(
    tmp_path, session, store, data_dir
) -> None:
    """A scan-only PDF with NO OCR provider must not produce empty 'native'
    text: EXTRACT_001 with the report showing OCR_UNAVAILABLE per page."""
    path = write_pdf(tmp_path, "f02.pdf", build_f02_scanned())
    with pytest.raises(DomainError) as excinfo:
        run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    assert excinfo.value.code == "EXTRACT_001"
    report = json.loads(Path(excinfo.value.details["report_path"]).read_text())
    assert report["quality_checks"]["suspiciously_empty_text"] is True
    assert all(
        any(w.startswith("OCR_UNAVAILABLE") for w in page["warnings"]) for page in report["pages"]
    )
    assert count(session, PaperVersionRow) == 0


def test_scanned_with_mock_ocr_imports_successfully(tmp_path, session, store, data_dir) -> None:
    path = write_pdf(tmp_path, "f02.pdf", build_f02_scanned())
    result = run(
        import_pdf(
            path,
            session=session,
            store=store,
            data_dir=data_dir,
            ocr_provider=MockOCRProvider("prv_mock_ocr", seed=2000),
        )
    )
    assert result.report.quality_checks.suspiciously_empty_text is False
    assert result.report.source_method_counts.get("OCR", 0) > 0
    assert result.report.source_method_counts.get("PDF_NATIVE", 0) == 0
    for page in result.report.pages:
        assert page.ocr_used is True
        assert page.page_source_method is SourceMethod.OCR
        assert page.mean_ocr_confidence is not None
    assert result.report.quality_state in {
        DataQualityState.GOOD,
        DataQualityState.DEGRADED,
    }


def test_mixed_pdf_merges_native_and_ocr(tmp_path, session, store, data_dir) -> None:
    path = write_pdf(tmp_path, "f03.pdf", build_f03_mixed())
    result = run(
        import_pdf(
            path,
            session=session,
            store=store,
            data_dir=data_dir,
            ocr_provider=MockOCRProvider("prv_mock_ocr", seed=7),
        )
    )
    pages = {page.page_number: page for page in result.report.pages}
    assert pages[1].page_source_method is SourceMethod.PDF_NATIVE
    assert pages[1].ocr_used is False
    assert pages[2].page_source_method is SourceMethod.OCR
    assert pages[2].ocr_used is True
    assert pages[3].page_source_method is SourceMethod.MIXED_NATIVE_OCR
    assert pages[3].ocr_used is True
    methods = {unit.source_method for unit in pages[3].units}
    assert SourceMethod.PDF_NATIVE in methods
    assert SourceMethod.OCR in methods


def test_no_ocr_flag_skips_provider(tmp_path, session, store, data_dir) -> None:
    path = write_pdf(tmp_path, "f03.pdf", build_f03_mixed())

    class ExplodingOCR(MockOCRProvider):
        async def recognize_page(self, image, request):  # pragma: no cover
            raise AssertionError("OCR must not be called with enable_ocr=False")

    result = run(
        import_pdf(
            path,
            session=session,
            store=store,
            data_dir=data_dir,
            ocr_provider=ExplodingOCR("prv_exploding"),
            enable_ocr=False,
        )
    )
    # F03 page 1 native text keeps the document importable; scanned pages
    # are honestly flagged OCR_UNAVAILABLE and the document is DEGRADED.
    assert any("OCR_UNAVAILABLE" in w for w in result.report.warnings)
    assert result.report.quality_state is DataQualityState.DEGRADED


# ---------------------------------------------------------------------------
# crops + rich content
# ---------------------------------------------------------------------------


def test_f04_crops_persisted_as_canonical_assets(tmp_path, session, store, data_dir) -> None:
    path = write_pdf(tmp_path, "f04.pdf", build_f04_rich())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    crop_units = [
        unit
        for page in result.report.pages
        for unit in page.units
        if unit.unit_type.value in {"FIGURE", "TABLE"}
    ]
    assert crop_units
    kinds = set()
    for unit in crop_units:
        assert unit.asset_sha256, "crop asset hash must be recorded on the unit"
        assert "crop:rendered_png" in unit.flags
        asset = session.scalar(select(AssetRow).where(AssetRow.sha256 == unit.asset_sha256))
        assert asset is not None
        assert store.exists(asset.storage_key)
        kinds.add(asset.kind)
    assert AssetKind.FIGURE in kinds or AssetKind.TABLE_IMAGE in kinds
    # Crop assets are separate rows from the PDF asset.
    assert count(session, AssetRow) == 1 + len({unit.asset_sha256 for unit in crop_units})


def test_persist_crops_disabled_keeps_units_without_asset_rows(
    tmp_path, session, store, data_dir
) -> None:
    path = write_pdf(tmp_path, "f04.pdf", build_f04_rich())
    result = run(
        import_pdf(path, session=session, store=store, data_dir=data_dir, persist_crops=False)
    )
    tables = [
        unit
        for page in result.report.pages
        for unit in page.units
        if unit.unit_type.value == "TABLE"
    ]
    assert tables and tables[0].asset_sha256 is None
    assert count(session, AssetRow) == 1  # PDF only


def test_f06_truth_facts_flow_into_report(tmp_path, session, store, data_dir) -> None:
    path = write_pdf(tmp_path, "f06.pdf", build_f06_truth())
    result = run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    all_text = " ".join(
        " ".join((unit.text or "").split()) for page in result.report.pages for unit in page.units
    )
    assert "exactly 1,000 samples" in all_text
    assert "82.5% accuracy" in all_text
    assert "2.1 percentage points" in all_text
    assert result.report.quality_state is DataQualityState.GOOD

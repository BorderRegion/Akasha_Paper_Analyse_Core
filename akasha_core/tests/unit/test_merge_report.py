"""Merge policy + extraction report/quality-check tests (P03)."""

from __future__ import annotations

import hashlib

import pytest
from tests.fixtures.generators import build_f01_native

from paperintel.extraction.merge import merge_page_units
from paperintel.extraction.pdf_document import inspect_document, open_pdf
from paperintel.extraction.report import build_report, run_quality_checks
from paperintel.ids import new_run_id
from paperintel.ocr.fallback import units_from_ocr_result
from paperintel.providers.base import OcrLine, OcrPageResult
from paperintel.schemas.common import BBox, utcnow
from paperintel.schemas.enums import (
    DataQualityState,
    EvidenceType,
    PageMode,
    SourceMethod,
)
from paperintel.schemas.extraction import ExtractedUnit


def _unit(
    unit_type: EvidenceType = EvidenceType.PARAGRAPH,
    *,
    page: int = 1,
    bbox: tuple[float, float, float, float] | None = (50, 50, 200, 70),
    text: str | None = "native text",
    method: SourceMethod = SourceMethod.PDF_NATIVE,
    confidence: float | None = None,
    flags: list[str] | None = None,
) -> ExtractedUnit:
    content = text or "figure"
    return ExtractedUnit(
        unit_type=unit_type,
        page_number=page,
        bbox=BBox(x0=bbox[0], y0=bbox[1], x1=bbox[2], y1=bbox[3]) if bbox else None,
        text=text,
        source_method=method,
        ocr_confidence=confidence,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        ordinal=0,
        flags=flags or [],
    )


# ---------------------------------------------------------------------------
# merge policy
# ---------------------------------------------------------------------------


def test_native_pages_keep_native_units_only() -> None:
    native = [_unit()]
    merged, method = merge_page_units(PageMode.NATIVE_TEXT, native, [])
    assert merged == native
    assert method is SourceMethod.PDF_NATIVE


def test_scanned_pages_take_ocr_only() -> None:
    # The full-page scan image placement is NOT figure evidence on SCANNED.
    native = [_unit(EvidenceType.FIGURE, text=None, bbox=(0, 0, 595, 842))]
    ocr = [_unit(text="ocr line", method=SourceMethod.OCR, confidence=0.9)]
    merged, method = merge_page_units(PageMode.SCANNED, native, ocr)
    assert [u.text for u in merged] == ["ocr line"]
    assert method is SourceMethod.OCR


def test_broken_text_layer_drops_native_text_keeps_figures() -> None:
    native = [
        _unit(text="garbled t\u0065xt flood"),
        _unit(EvidenceType.FIGURE, text=None, bbox=(300, 300, 500, 500)),
    ]
    ocr = [_unit(text="clean ocr text", method=SourceMethod.OCR, confidence=0.95)]
    merged, method = merge_page_units(PageMode.BROKEN_TEXT_LAYER, native, ocr)
    # Garbled native text dropped; embedded figure object kept; OCR text in.
    # Reading order puts the OCR paragraph (y=50) before the figure (y=300).
    assert [u.unit_type for u in merged] == [EvidenceType.PARAGRAPH, EvidenceType.FIGURE]
    assert merged[0].text == "clean ocr text"
    assert method is SourceMethod.OCR


def test_mixed_page_combines_and_dedups_overlap() -> None:
    native = [_unit(text="native paragraph", bbox=(50, 50, 300, 100))]
    overlapping = _unit(
        text="ocr duplicate", bbox=(60, 60, 200, 80), method=SourceMethod.OCR, confidence=0.9
    )
    fresh = _unit(
        text="ocr from scanned region",
        bbox=(50, 400, 300, 430),
        method=SourceMethod.OCR,
        confidence=0.85,
    )
    merged, method = merge_page_units(PageMode.MIXED, native, [overlapping, fresh])
    texts = [u.text for u in merged]
    assert "native paragraph" in texts
    assert "ocr from scanned region" in texts
    assert "ocr duplicate" not in texts  # center inside native bbox → dropped
    assert method is SourceMethod.MIXED_NATIVE_OCR
    # Reading order + renumbered ordinals.
    assert [u.ordinal for u in merged] == list(range(len(merged)))
    ys = [u.bbox.y0 for u in merged if u.bbox]
    assert ys == sorted(ys)


def test_mixed_page_without_new_ocr_text_stays_native() -> None:
    native = [_unit(text="native paragraph", bbox=(50, 50, 300, 100))]
    overlapping = _unit(
        text="ocr duplicate", bbox=(60, 60, 200, 80), method=SourceMethod.OCR, confidence=0.9
    )
    merged, method = merge_page_units(PageMode.MIXED, native, [overlapping])
    assert [u.text for u in merged] == ["native paragraph"]
    assert method is SourceMethod.PDF_NATIVE


# ---------------------------------------------------------------------------
# OCR unit conversion (coordinate scaling + honesty flags)
# ---------------------------------------------------------------------------


def _ocr_result(lines: list[OcrLine], warnings: list[str] | None = None) -> OcrPageResult:
    confidences = [ln.confidence for ln in lines if ln.confidence is not None]
    return OcrPageResult(
        lines=lines,
        full_text="\n".join(ln.text for ln in lines),
        mean_confidence=(sum(confidences) / len(confidences)) if confidences else None,
        warnings=warnings or [],
        provider_id="prv_test",
    )


def test_ocr_bbox_scaled_from_pixels_to_points() -> None:
    # At 200 dpi the raster scale is 200/72; provider bboxes are pixel space.
    line = OcrLine(text="scaled", confidence=0.9, bbox=BBox(x0=200, y0=400, x1=600, y1=500))
    result = _ocr_result([line])
    units = units_from_ocr_result(
        result, page_number=1, page_width_pt=595, page_height_pt=842, dpi=200
    )
    bbox = units[0].bbox
    scale = 72.0 / 200
    assert bbox is not None
    assert bbox.x0 == pytest.approx(200 * scale)
    assert bbox.y1 == pytest.approx(500 * scale)


def test_ocr_out_of_page_bbox_dropped_with_flag() -> None:
    line = OcrLine(text="offpage", confidence=0.9, bbox=BBox(x0=10, y0=10, x1=5000, y1=50))
    units = units_from_ocr_result(
        _ocr_result([line]), page_number=1, page_width_pt=595, page_height_pt=842, dpi=200
    )
    assert units[0].bbox is None
    assert "bbox_out_of_page:dropped" in units[0].flags


def test_ocr_confidence_none_flagged_never_invented() -> None:
    line = OcrLine(text="no confidence", confidence=None)
    units = units_from_ocr_result(
        _ocr_result([line], warnings=["OCR_CONFIDENCE_UNAVAILABLE: backend"]),
        page_number=1,
        page_width_pt=595,
        page_height_pt=842,
    )
    assert units[0].ocr_confidence is None
    assert "OCR_CONFIDENCE_UNAVAILABLE" in units[0].flags
    assert units[0].source_method is SourceMethod.OCR


# ---------------------------------------------------------------------------
# report + quality checks
# ---------------------------------------------------------------------------


def _report_for_f01() -> tuple:
    doc = open_pdf(build_f01_native())
    try:
        inspection = inspect_document(doc)
    finally:
        doc.close()
    from paperintel.extraction.native import extract_document_units

    doc = open_pdf(build_f01_native())
    try:
        units = extract_document_units(doc)
    finally:
        doc.close()
    from paperintel.extraction.page_classifier import classify_page
    from paperintel.schemas.extraction import PageExtraction

    pages = [
        PageExtraction(
            page_number=p.page_number,
            classification=classify_page(p),
            units=units.get(p.page_number, []),
            page_source_method=SourceMethod.PDF_NATIVE,
        )
        for p in inspection.pages
    ]
    report = build_report(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        inspection=inspection,
        pages=pages,
        ocr_failures={},
        warnings=[],
    )
    return inspection, pages, report


def test_report_good_quality_for_clean_native_document() -> None:
    inspection, pages, report = _report_for_f01()
    assert report.page_count == 2
    assert report.quality_state is DataQualityState.GOOD
    checks = report.quality_checks
    assert checks.zero_page_detected is False
    assert checks.suspiciously_empty_text is False
    assert checks.page_count_consistent is True
    assert checks.location_sanity is True
    assert checks.ocr_confidence_warnings == 0
    assert report.unit_counts["PARAGRAPH"] >= 4
    assert report.source_method_counts == {"PDF_NATIVE": report.unit_total}


def test_report_poor_on_zero_pages() -> None:
    from tests.fixtures.generators import build_f05_zero_page

    doc = open_pdf(build_f05_zero_page())
    try:
        inspection = inspect_document(doc)
    finally:
        doc.close()
    report = build_report(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        inspection=inspection,
        pages=[],
        ocr_failures={},
        warnings=[],
    )
    assert report.quality_checks.zero_page_detected is True
    assert report.quality_state is DataQualityState.POOR
    assert any("zero-page" in w for w in report.warnings)


def test_report_poor_on_suspiciously_empty_text() -> None:
    from paperintel.schemas.extraction import PageClassification, PageExtraction, TextQualityMetrics

    doc = open_pdf(build_f01_native())
    try:
        inspection = inspect_document(doc)
    finally:
        doc.close()
    # Pretend extraction produced nothing usable.
    empty_pages = [
        PageExtraction(
            page_number=p.page_number,
            classification=PageClassification(
                page_number=p.page_number,
                mode=PageMode.SCANNED,
                metrics=TextQualityMetrics(
                    char_count=0,
                    visible_char_ratio=0.0,
                    replacement_char_ratio=0.0,
                    text_density=0.0,
                    bbox_sane=True,
                    repeated_glyph_ratio=0.0,
                    image_area_ratio=0.9,
                ),
                needs_ocr=True,
            ),
            units=[],
            page_source_method=SourceMethod.OCR,
            warnings=["OCR_UNAVAILABLE: no provider"],
        )
        for p in inspection.pages
    ]
    report = build_report(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        inspection=inspection,
        pages=empty_pages,
        ocr_failures={},
        warnings=[],
    )
    assert report.quality_checks.suspiciously_empty_text is True
    assert report.quality_state is DataQualityState.POOR


def test_report_degraded_on_ocr_failures_and_warnings() -> None:
    inspection, pages, _ = _report_for_f01()
    # Rebuild with one failed OCR page and one OCR_003 warning.
    mutated = []
    for i, page in enumerate(pages):
        if i == 0:
            mutated.append(page.model_copy(update={"warnings": [*page.warnings, "OCR_003: low"]}))
        else:
            mutated.append(page)
    report = build_report(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        inspection=inspection,
        pages=mutated,
        ocr_failures={2: "OCR_001"},
        warnings=[],
    )
    assert report.quality_state is DataQualityState.DEGRADED
    assert report.ocr_failures == {2: "OCR_001"}
    assert report.quality_checks.ocr_confidence_warnings >= 1
    assert any("OCR failed" in w for w in report.warnings)


def test_location_sanity_violation_detected() -> None:
    inspection, pages, _ = _report_for_f01()
    bad_unit = _unit(bbox=(0, 0, 99999, 99999))
    mutated = [
        pages[0].model_copy(update={"units": [*pages[0].units, bad_unit]}),
        pages[1],
    ]
    checks = run_quality_checks(inspection, mutated)
    assert checks.location_sanity is False
    report = build_report(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        inspection=inspection,
        pages=mutated,
        ocr_failures={},
        warnings=[],
    )
    assert report.quality_state is DataQualityState.DEGRADED
    assert any("location sanity" in w for w in report.warnings)

"""Page classifier + PDF inspection tests (P03).

All five frozen page modes (doc 01 §7) are exercised on deterministic
fixtures; OCR must be requested ONLY for pages that need it.
"""

from __future__ import annotations

import pytest
from tests.fixtures.generators import (
    build_f01_native,
    build_f02_scanned,
    build_f03_mixed,
    build_f04_rich,
    build_fx_broken_text,
    build_fx_image_heavy,
)

from paperintel.errors import DomainError
from paperintel.extraction.page_classifier import (
    IMAGE_HEAVY_AREA_RATIO,
    MIXED_IMAGE_AREA_RATIO,
    SCANNED_MAX_CHARS,
    classify_page,
    compute_metrics,
)
from paperintel.extraction.pdf_document import (
    inspect_document,
    open_pdf,
    render_clip_png,
    render_page_png,
)
from paperintel.schemas.enums import PageMode


def _classifications(pdf_bytes: bytes):
    doc = open_pdf(pdf_bytes)
    try:
        inspection = inspect_document(doc)
        return [classify_page(page) for page in inspection.pages]
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# open / inspect error policy
# ---------------------------------------------------------------------------


def test_corrupt_pdf_maps_pdf001() -> None:
    from tests.fixtures.generators import build_f05_corrupt

    with pytest.raises(DomainError) as excinfo:
        open_pdf(build_f05_corrupt())
    assert excinfo.value.code == "PDF_001"
    assert excinfo.value.retryable is False


def test_encrypted_pdf_maps_pdf002() -> None:
    from tests.fixtures.generators import build_f05_encrypted

    with pytest.raises(DomainError) as excinfo:
        open_pdf(build_f05_encrypted())
    assert excinfo.value.code == "PDF_002"


def test_zero_page_document_opens_and_is_flagged() -> None:
    from tests.fixtures.generators import build_f05_zero_page

    doc = open_pdf(build_f05_zero_page())
    try:
        inspection = inspect_document(doc)
        assert inspection.page_count == 0
        assert inspection.is_zero_page is True
        assert inspection.pages == ()
    finally:
        doc.close()


def test_inspection_facts_are_raw_and_complete() -> None:
    doc = open_pdf(build_f01_native())
    try:
        inspection = inspect_document(doc)
        assert inspection.page_count == 2
        page1 = inspection.pages[0]
        assert page1.page_number == 1
        assert page1.width_pt == pytest.approx(595.0)
        assert page1.height_pt == pytest.approx(842.0)
        assert "Native Extraction" in page1.native_text
        assert page1.line_bboxes  # text lines carry locations
        assert page1.image_area_ratio == 0.0
        assert page1.is_blank is False
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# classification: all five frozen modes
# ---------------------------------------------------------------------------


def test_native_text_pages_classified() -> None:
    classifications = _classifications(build_f01_native())
    assert [c.mode for c in classifications] == [PageMode.NATIVE_TEXT] * 2
    assert all(c.needs_ocr is False for c in classifications)


def test_scanned_pages_classified_and_need_ocr() -> None:
    classifications = _classifications(build_f02_scanned())
    assert [c.mode for c in classifications] == [PageMode.SCANNED] * 2
    assert all(c.needs_ocr for c in classifications)
    assert all(c.metrics.char_count <= SCANNED_MAX_CHARS for c in classifications)


def test_mixed_document_page_modes() -> None:
    classifications = _classifications(build_f03_mixed())
    modes = [c.mode for c in classifications]
    assert modes == [PageMode.NATIVE_TEXT, PageMode.SCANNED, PageMode.MIXED]
    assert [c.needs_ocr for c in classifications] == [False, True, True]
    mixed = classifications[2]
    assert mixed.metrics.image_area_ratio >= MIXED_IMAGE_AREA_RATIO
    assert mixed.metrics.image_area_ratio < IMAGE_HEAVY_AREA_RATIO


def test_image_heavy_classification() -> None:
    classifications = _classifications(build_fx_image_heavy())
    assert classifications[0].mode is PageMode.IMAGE_HEAVY
    assert classifications[0].metrics.image_area_ratio >= IMAGE_HEAVY_AREA_RATIO
    # Plate pages do not trigger page-level OCR (artwork is not text carrier).
    assert classifications[0].needs_ocr is False


@pytest.mark.parametrize("kind", ["repeat", "replacement"])
def test_broken_text_layer_classification(kind: str) -> None:
    classifications = _classifications(build_fx_broken_text(kind))
    broken = classifications[0]
    assert broken.mode is PageMode.BROKEN_TEXT_LAYER
    assert broken.needs_ocr is True
    assert broken.reasons  # decision trail recorded


def test_rich_scientific_page_is_native() -> None:
    classifications = _classifications(build_f04_rich())
    assert classifications[0].mode is PageMode.NATIVE_TEXT
    assert classifications[1].mode is PageMode.NATIVE_TEXT


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_metrics_ranges_and_density() -> None:
    doc = open_pdf(build_f01_native())
    try:
        inspection = inspect_document(doc)
        metrics = compute_metrics(inspection.pages[0])
        assert metrics.char_count > 500
        assert 0.0 <= metrics.visible_char_ratio <= 1.0
        assert metrics.replacement_char_ratio == 0.0
        assert metrics.repeated_glyph_ratio < 0.1
        assert metrics.text_density > 0.0005
        assert metrics.bbox_sane is True
        assert metrics.image_area_ratio == 0.0
    finally:
        doc.close()


def test_classification_reasons_name_thresholds() -> None:
    classifications = _classifications(build_f02_scanned())
    assert any(str(SCANNED_MAX_CHARS) in reason for reason in classifications[0].reasons)


# ---------------------------------------------------------------------------
# rendering (temporary rasters for OCR / canonical clips for crops)
# ---------------------------------------------------------------------------


def test_render_page_png_produces_png() -> None:
    doc = open_pdf(build_f01_native())
    try:
        png = render_page_png(doc, 1)
        assert png.startswith(b"\x89PNG")
        with pytest.raises(ValueError):
            render_page_png(doc, 99)
    finally:
        doc.close()


def test_render_clip_png_crops_region() -> None:
    doc = open_pdf(build_f04_rich())
    try:
        png = render_clip_png(doc, 1, (56.0, 56.0, 300.0, 200.0))
        assert png.startswith(b"\x89PNG")
        with pytest.raises(ValueError):
            render_clip_png(doc, 1, (10.0, 10.0, 10.0, 10.0))  # empty clip
    finally:
        doc.close()

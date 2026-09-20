"""extraction.report — extraction reports + P03 quality checks.

Quality checks required by spec doc 05 (P03):
- zero-page detection;
- suspiciously empty text;
- OCR confidence warnings;
- page counts;
- evidence location sanity.

Overall DataQualityState (doc 02 §10 — data quality, NOT execution state):
- POOR: zero pages, or a document with pages but no usable text at all;
- DEGRADED: any OCR page failure, OCR unavailable for a page that needs it,
  any OCR_003 low-confidence warning, any confidence-less OCR backend in
  use, location-sanity violations, or broken-text-layer pages;
- GOOD: everything else (all pages natively extracted with sane locations).
"""

from __future__ import annotations

from datetime import datetime

from paperintel.extraction.pdf_document import DocumentInspection
from paperintel.schemas.enums import DataQualityState, PageMode
from paperintel.schemas.extraction import (
    ExtractedUnit,
    ExtractionReport,
    PageExtraction,
    QualityChecks,
)

#: Bboxes may exceed the page box by this many points (raster rounding) and
#: still count as sane locations.
LOCATION_TOLERANCE_PT = 4.0


def _text_unit_count(pages: list[PageExtraction]) -> int:
    return sum(
        1 for page in pages for unit in page.units if unit.text is not None and unit.text.strip()
    )


def run_quality_checks(
    inspection: DocumentInspection,
    pages: list[PageExtraction],
) -> QualityChecks:
    ocr_warnings = 0
    for page in pages:
        ocr_warnings += sum(1 for w in page.warnings if w.startswith("OCR_003"))
        for unit in page.units:
            ocr_warnings += sum(1 for f in unit.flags if "OCR_CONFIDENCE_UNAVAILABLE" in f)
    location_sane = all(
        _unit_location_sane(unit, inspection) for page in pages for unit in page.units
    )
    return QualityChecks(
        zero_page_detected=inspection.is_zero_page,
        suspiciously_empty_text=(not inspection.is_zero_page and _text_unit_count(pages) == 0),
        ocr_confidence_warnings=ocr_warnings,
        page_count_consistent=inspection.page_count == len(pages),
        location_sanity=location_sane,
    )


def _unit_location_sane(unit: ExtractedUnit, inspection: DocumentInspection) -> bool:
    if unit.bbox is None:
        return True  # "bbox where applicable" — absence is legal
    if not 1 <= unit.page_number <= len(inspection.pages):
        return False
    page = inspection.pages[unit.page_number - 1]
    tol = LOCATION_TOLERANCE_PT
    box = unit.bbox
    return (
        box.x1 >= box.x0
        and box.y1 >= box.y0
        and box.x0 >= -tol
        and box.y0 >= -tol
        and box.x1 <= page.width_pt + tol
        and box.y1 <= page.height_pt + tol
    )


def overall_quality_state(
    checks: QualityChecks,
    pages: list[PageExtraction],
    ocr_failures: dict[int, str],
) -> DataQualityState:
    if checks.zero_page_detected or checks.suspiciously_empty_text:
        return DataQualityState.POOR
    degraded = False
    if ocr_failures or checks.ocr_confidence_warnings or not checks.location_sanity:
        degraded = True
    for page in pages:
        if page.classification.mode is PageMode.BROKEN_TEXT_LAYER:
            degraded = True
        if any(w.startswith("OCR_UNAVAILABLE") for w in page.warnings):
            # Pages that needed OCR but had no provider are degraded data,
            # never silently GOOD.
            degraded = True
        if page.ocr_used and page.mean_ocr_confidence is None and page.units:
            # Confidence-less OCR backend contributed units → degraded data,
            # explicitly (never presented as GOOD).
            degraded = True
    return DataQualityState.DEGRADED if degraded else DataQualityState.GOOD


def build_report(
    *,
    run_id: str,
    paper_version_id: str | None,
    started_at: datetime,
    finished_at: datetime,
    inspection: DocumentInspection,
    pages: list[PageExtraction],
    ocr_failures: dict[int, str],
    warnings: list[str],
) -> ExtractionReport:
    """Assemble the machine-written extraction report for one run."""
    checks = run_quality_checks(inspection, pages)
    unit_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}
    for page in pages:
        for unit in page.units:
            key = unit.unit_type.value
            unit_counts[key] = unit_counts.get(key, 0) + 1
            method_key = unit.source_method.value
            method_counts[method_key] = method_counts.get(method_key, 0) + 1
    all_warnings = list(warnings)
    if checks.zero_page_detected:
        all_warnings.append("QUALITY: zero-page PDF detected")
    if checks.suspiciously_empty_text:
        all_warnings.append("QUALITY: suspiciously empty text (no usable text units)")
    if not checks.page_count_consistent:
        all_warnings.append("QUALITY: page count inconsistent between inspection and extraction")
    if not checks.location_sanity:
        all_warnings.append("QUALITY: evidence location sanity violations detected")
    for page_number, code in sorted(ocr_failures.items()):
        all_warnings.append(f"QUALITY: page {page_number} OCR failed with {code}")
    return ExtractionReport(
        run_id=run_id,
        paper_version_id=paper_version_id,
        started_at=started_at,
        finished_at=finished_at,
        page_count=inspection.page_count,
        pages=pages,
        unit_counts=dict(sorted(unit_counts.items())),
        source_method_counts=dict(sorted(method_counts.items())),
        quality_state=overall_quality_state(checks, pages, ocr_failures),
        quality_checks=checks,
        warnings=all_warnings,
        ocr_failures=ocr_failures,
    )

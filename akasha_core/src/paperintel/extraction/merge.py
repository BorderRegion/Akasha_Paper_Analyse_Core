"""extraction.merge — mixed-page merge (doc 01 §7 step 4).

Merge policy per page mode (deterministic, no heuristics beyond overlap):
- NATIVE_TEXT / IMAGE_HEAVY: native units only (OCR was not requested);
- SCANNED: OCR units only — the page's own image placement is the scan
  itself, not figure evidence, so native FIGURE units are dropped;
- BROKEN_TEXT_LAYER: the native TEXT layer is garbage and is dropped;
  native FIGURE units (embedded objects are unaffected by a broken text
  layer) are kept alongside OCR text;
- MIXED: native units + OCR lines that do not land inside an existing
  native text unit's bbox (overlap dedup), preserving both provenances.

The page's effective source method is recorded honestly:
PDF_NATIVE / OCR / MIXED_NATIVE_OCR.
"""

from __future__ import annotations

from paperintel.schemas.enums import PageMode, SourceMethod
from paperintel.schemas.extraction import ExtractedUnit

#: Unit types considered "text carriers" for overlap dedup.
_TEXT_TYPES = frozenset({"PARAGRAPH", "HEADING", "CAPTION", "REFERENCE", "EQUATION"})


def _center(unit: ExtractedUnit) -> tuple[float, float] | None:
    if unit.bbox is None:
        return None
    return (
        (unit.bbox.x0 + unit.bbox.x1) / 2.0,
        (unit.bbox.y0 + unit.bbox.y1) / 2.0,
    )


def _inside_any_text_unit(unit: ExtractedUnit, text_units: list[ExtractedUnit]) -> bool:
    point = _center(unit)
    if point is None:
        return False
    x, y = point
    for other in text_units:
        if other.bbox is None:
            continue
        box = other.bbox
        if box.x0 <= x <= box.x1 and box.y0 <= y <= box.y1:
            return True
    return False


def merge_page_units(
    mode: PageMode,
    native_units: list[ExtractedUnit],
    ocr_units: list[ExtractedUnit],
) -> tuple[list[ExtractedUnit], SourceMethod]:
    """Merge one page's native and OCR units; returns (units, page method)."""
    if mode in {PageMode.NATIVE_TEXT, PageMode.IMAGE_HEAVY}:
        merged = list(native_units)
        method = SourceMethod.PDF_NATIVE
    elif mode is PageMode.SCANNED:
        merged = list(ocr_units)
        method = SourceMethod.OCR
    elif mode is PageMode.BROKEN_TEXT_LAYER:
        kept_native = [unit for unit in native_units if unit.unit_type.value == "FIGURE"]
        merged = kept_native + list(ocr_units)
        method = SourceMethod.OCR if ocr_units else SourceMethod.PDF_NATIVE
    else:  # MIXED
        text_units = [unit for unit in native_units if unit.unit_type.value in _TEXT_TYPES]
        fresh_ocr = [unit for unit in ocr_units if not _inside_any_text_unit(unit, text_units)]
        merged = native_units + fresh_ocr
        if fresh_ocr and text_units:
            method = SourceMethod.MIXED_NATIVE_OCR
        elif fresh_ocr:
            method = SourceMethod.OCR
        else:
            method = SourceMethod.PDF_NATIVE

    merged.sort(key=_reading_order_key)
    return [
        unit.model_copy(update={"ordinal": position}) for position, unit in enumerate(merged)
    ], method


def _reading_order_key(unit: ExtractedUnit) -> tuple[float, float, int]:
    if unit.bbox is None:
        return (float("inf"), 0.0, 1)
    return (round(unit.bbox.y0 / 4.0), unit.bbox.x0, 0)

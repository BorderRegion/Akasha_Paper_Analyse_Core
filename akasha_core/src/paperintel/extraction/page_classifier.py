"""extraction.page_classifier — deterministic page-mode classification.

Implements doc 01 §7 decision policy steps 1–3: evaluate native text
quality (visible character ratio, replacement-character ratio, text density,
bounding-box sanity, suspicious repeated glyphs), classify the page BEFORE
OCR, and request OCR only for pages that need it.

All thresholds are module constants — classification is deterministic and
every decision carries a reason string naming the threshold that fired.
"""

from __future__ import annotations

import unicodedata

from paperintel.extraction.pdf_document import PageInspection
from paperintel.schemas.enums import PageMode
from paperintel.schemas.extraction import PageClassification, TextQualityMetrics

#: Minimum characters per pt² for a page to count as natively text-bearing.
#: A4 ≈ 500k pt²; even sparse plate pages carry ≳ 150 chars → 0.0003.
#: Reported in metrics; classification bands below do the mode decisions.
MIN_TEXT_DENSITY = 0.0003
#: Below this ratio of visible characters the text layer is considered broken.
MIN_VISIBLE_CHAR_RATIO = 0.5
#: Above this U+FFFD ratio the text layer is considered broken.
MAX_REPLACEMENT_CHAR_RATIO = 0.2
#: Above this repeated-glyph ratio the text layer is considered degenerate.
MAX_REPEATED_GLYPH_RATIO = 0.5
#: Image area fraction at/above which a page is IMAGE_HEAVY rather than MIXED.
IMAGE_HEAVY_AREA_RATIO = 0.6
#: Image area fraction at/above which a page with a text layer is MIXED
#: (native text + scanned/raster regions that need OCR).
MIXED_IMAGE_AREA_RATIO = 0.15
#: A page with images but (almost) no text is SCANNED below this char count.
SCANNED_MAX_CHARS = 32
#: bbox tolerance in points for "inside the page" sanity checks.
BBOX_TOLERANCE_PT = 2.0


def compute_metrics(inspection: PageInspection) -> TextQualityMetrics:
    """Derive the doc 01 §7 quality signals from raw inspection facts."""
    text = inspection.native_text
    char_count = len(text)
    if char_count:
        replacement = sum(1 for ch in text if ch == "\ufffd")
        visible = sum(1 for ch in text if not ch.isspace() and unicodedata.category(ch)[0] != "C")
        repeated = _longest_repeat_ratio(text)
    else:
        replacement = visible = 0
        repeated = 0.0
    area = max(1.0, inspection.width_pt * inspection.height_pt)
    return TextQualityMetrics(
        char_count=char_count,
        visible_char_ratio=(visible / char_count) if char_count else 0.0,
        replacement_char_ratio=(replacement / char_count) if char_count else 0.0,
        text_density=char_count / area,
        bbox_sane=_bbox_sane(inspection),
        repeated_glyph_ratio=repeated,
        image_area_ratio=inspection.image_area_ratio,
    )


def _longest_repeat_ratio(text: str) -> float:
    """Longest run of one identical character / total characters.

    Whitespace runs are ignored (paragraph gaps are legitimate); a text
    layer emitting thousands of '.' or '\ufffd' or a single glyph is the
    degenerate pattern this catches.
    """
    best = 0
    current = 0
    previous: str | None = None
    for ch in text:
        if ch.isspace():
            previous = None
            current = 0
            continue
        if ch == previous:
            current += 1
        else:
            current = 1
            previous = ch
        best = max(best, current)
    return (best / len(text)) if text else 0.0


def _bbox_sane(inspection: PageInspection) -> bool:
    tol = BBOX_TOLERANCE_PT
    for x0, y0, x1, y1 in inspection.line_bboxes:
        if x1 < x0 or y1 < y0:  # inverted geometry
            return False
        if x0 < -tol or y0 < -tol:
            return False
        if x1 > inspection.width_pt + tol or y1 > inspection.height_pt + tol:
            return False
    return True


def classify_page(inspection: PageInspection) -> PageClassification:
    """Assign one of the five frozen page modes (doc 01 §7).

    Decision order (first match wins, reasons recorded):
    1. BROKEN_TEXT_LAYER — text exists but is garbage (replacement floods,
       invisible characters, degenerate repeats, insane bboxes);
    2. SCANNED — text layer absent/negligible (images present or blank page);
    3. IMAGE_HEAVY — images dominate the page (figure plates, charts);
    4. MIXED — substantial raster region(s) alongside a native text layer;
    5. NATIVE_TEXT — everything else (healthy or merely sparse text).

    needs_ocr: SCANNED / BROKEN_TEXT_LAYER / MIXED pages. IMAGE_HEAVY pages
    do NOT trigger page-level OCR (their figures are not text carriers; a
    caption is normally native) — if an IMAGE_HEAVY page turns out to have no
    usable text at all, the report flags it (suspiciously empty) instead of
    silently OCR'ing artwork.
    """
    metrics = compute_metrics(inspection)
    reasons: list[str] = []
    has_images = bool(inspection.images)
    text_broken = metrics.char_count > 0 and (
        metrics.replacement_char_ratio > MAX_REPLACEMENT_CHAR_RATIO
        or metrics.visible_char_ratio < MIN_VISIBLE_CHAR_RATIO
        or metrics.repeated_glyph_ratio > MAX_REPEATED_GLYPH_RATIO
        or not metrics.bbox_sane
    )

    if text_broken:
        mode = PageMode.BROKEN_TEXT_LAYER
        if metrics.replacement_char_ratio > MAX_REPLACEMENT_CHAR_RATIO:
            reasons.append(f"replacement_char_ratio>{MAX_REPLACEMENT_CHAR_RATIO}")
        if metrics.visible_char_ratio < MIN_VISIBLE_CHAR_RATIO:
            reasons.append(f"visible_char_ratio<{MIN_VISIBLE_CHAR_RATIO}")
        if metrics.repeated_glyph_ratio > MAX_REPEATED_GLYPH_RATIO:
            reasons.append(f"repeated_glyph_ratio>{MAX_REPEATED_GLYPH_RATIO}")
        if not metrics.bbox_sane:
            reasons.append("bbox_sane=False")
    elif metrics.char_count <= SCANNED_MAX_CHARS:
        mode = PageMode.SCANNED
        if has_images:
            reasons.append(f"char_count<={SCANNED_MAX_CHARS} and images present")
        else:
            # Blank/near-blank vector page: no text carrier at all. OCR is
            # requested so faint content is honestly probed; an empty OCR
            # result confirms the page is blank (report keeps is_blank).
            reasons.append(f"char_count<={SCANNED_MAX_CHARS}, no images (blank page)")
    elif metrics.image_area_ratio >= IMAGE_HEAVY_AREA_RATIO:
        mode = PageMode.IMAGE_HEAVY
        reasons.append(f"image_area_ratio>={IMAGE_HEAVY_AREA_RATIO} (plate/figure page)")
    elif metrics.image_area_ratio >= MIXED_IMAGE_AREA_RATIO:
        mode = PageMode.MIXED
        reasons.append(
            f"image_area_ratio>={MIXED_IMAGE_AREA_RATIO}: native text plus "
            "raster regions that need OCR"
        )
    else:
        mode = PageMode.NATIVE_TEXT
        reasons.append("native text layer present, imagery not substantial")
        if metrics.text_density < MIN_TEXT_DENSITY:
            reasons.append(f"sparse text (density<{MIN_TEXT_DENSITY}) but metrics healthy")

    needs_ocr = mode in {PageMode.SCANNED, PageMode.BROKEN_TEXT_LAYER, PageMode.MIXED}
    return PageClassification(
        page_number=inspection.page_number,
        mode=mode,
        metrics=metrics,
        needs_ocr=needs_ocr,
        reasons=reasons,
    )

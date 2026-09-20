"""ocr.fallback — OCR only for pages/regions that need it (doc 01 §7 step 3).

Temporary rasters are rendered on demand, hashed for the provider request,
and dropped after recognition — they never become canonical objects
(doc 01 §6.3). Provider failures are returned to the caller as catalog
DomainErrors; this module never retries beyond the provider's own bounded
policy and never substitutes empty text for a failed page.
"""

from __future__ import annotations

import hashlib

import pymupdf as fitz

from paperintel.extraction.pdf_document import OCR_RENDER_DPI, render_page_png
from paperintel.providers.base import OcrPageRequest, OCRProvider
from paperintel.schemas.enums import EvidenceType, SourceMethod
from paperintel.schemas.extraction import ExtractedUnit

#: OCR bboxes slightly exceeding the page box by this many points are still
#: accepted (raster rounding); beyond it the location is dropped, not faked.
BBOX_OVERFLOW_TOLERANCE_PT = 4.0


async def ocr_page(
    provider: OCRProvider,
    doc: fitz.Document,
    page_number: int,
    dpi: int = OCR_RENDER_DPI,
):
    """Render + recognize one page. Returns the provider's OcrPageResult."""
    image = render_page_png(doc, page_number, dpi=dpi)
    request = OcrPageRequest(
        image_sha256=hashlib.sha256(image).hexdigest(),
        page_number=page_number,
        mime_type="image/png",
    )
    return await provider.recognize_page(image, request)


def units_from_ocr_result(
    result,
    *,
    page_number: int,
    page_width_pt: float,
    page_height_pt: float,
    dpi: int = OCR_RENDER_DPI,
) -> list[ExtractedUnit]:
    """Convert OCR lines into ExtractedUnits with honest provenance.

    Coordinate mapping: provider bboxes live in raster pixel space; they are
    scaled to PDF points (× 72/dpi). Bboxes that still fall outside the page
    (beyond tolerance) are dropped with an explicit flag — a wrong location
    is worse than no location. Confidence is copied verbatim; when the
    backend reports none, ocr_confidence stays None and the unit is flagged
    OCR_CONFIDENCE_UNAVAILABLE (never invented).
    """
    scale = 72.0 / dpi
    tolerance = BBOX_OVERFLOW_TOLERANCE_PT
    units: list[ExtractedUnit] = []
    for line in result.lines:
        flags: list[str] = ["source:ocr_fallback"]
        bbox = None
        if line.bbox is not None:
            x0 = line.bbox.x0 * scale
            y0 = line.bbox.y0 * scale
            x1 = line.bbox.x1 * scale
            y1 = line.bbox.y1 * scale
            if (
                x0 >= -tolerance
                and y0 >= -tolerance
                and x1 <= page_width_pt + tolerance
                and y1 <= page_height_pt + tolerance
                and x1 >= x0
                and y1 >= y0
            ):
                bbox = {
                    "x0": max(0.0, min(x0, page_width_pt)),
                    "y0": max(0.0, min(y0, page_height_pt)),
                    "x1": max(0.0, min(x1, page_width_pt)),
                    "y1": max(0.0, min(y1, page_height_pt)),
                }
            else:
                flags.append("bbox_out_of_page:dropped")
        if line.confidence is None:
            flags.append("OCR_CONFIDENCE_UNAVAILABLE")
        units.append(
            ExtractedUnit(
                unit_type=EvidenceType.PARAGRAPH,
                page_number=page_number,
                bbox=bbox,
                text=line.text,
                source_method=SourceMethod.OCR,
                ocr_confidence=line.confidence,
                content_sha256=hashlib.sha256(line.text.encode("utf-8")).hexdigest(),
                ordinal=0,  # reassigned after merge
                flags=flags,
            )
        )
    return units

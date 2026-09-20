"""extraction.pdf — PDF open/inspect/render primitives (spec doc 01 §7).

Error policy (frozen catalog):
- unreadable/malformed PDF → PDF_001 (non-retryable);
- encrypted/password-protected without credentials → PDF_002;
- zero-page documents are VALID to open but flagged for the report
  (zero-page detection is a P03 quality check, not an open error).

Rendered page rasters are TEMPORARY data (doc 01 §6.3): they never become
canonical objects; callers pass them straight to the OCR provider and drop
the bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil, isfinite
from typing import Any

import pymupdf as fitz

from paperintel.errors import DomainError

# The frozen stack is plain PyMuPDF (doc 02 §5); pymupdf_layout is an
# optional extra dependency we deliberately do NOT adopt — silence its
# one-time console advertisement for deterministic gate logs.
fitz.no_recommend_layout()

#: Raster DPI for OCR fallback pages. 200 dpi balances scan fidelity against
#: provider payload size; rasters are temporary (never canonical).
OCR_RENDER_DPI = 200
MAX_PDF_BYTES = 100 * 1024 * 1024
MAX_PDF_PAGES = 2000
MAX_RENDER_PIXELS = 25_000_000


@dataclass(frozen=True, slots=True)
class PageInspection:
    """Raw per-page inspection facts (no classification decisions here)."""

    page_number: int  # 1-based
    width_pt: float
    height_pt: float
    native_text: str
    #: (x0, y0, x1, y1) tuples of native text lines in PDF points.
    line_bboxes: tuple[tuple[float, float, float, float], ...] = ()
    #: Embedded image placements: (xref, bbox or None) as reported by the page.
    images: tuple[tuple[int, tuple[float, float, float, float] | None], ...] = ()
    image_area_ratio: float = 0.0
    is_blank: bool = False


@dataclass(frozen=True, slots=True)
class DocumentInspection:
    page_count: int
    pages: tuple[PageInspection, ...]
    metadata: dict[str, Any] = field(default_factory=dict)
    is_zero_page: bool = False


#: MuPDF emits NUL (0x00) for glyphs it cannot map to a character. NUL is
#: unrepresentable in PostgreSQL text fields and invisible everywhere else;
#: the honest translation is U+FFFD REPLACEMENT CHARACTER — the same signal
#: the page classifier's replacement_char_ratio metric counts.
def sanitize_pdf_text(text: str) -> str:
    """Map unmappable-glyph NULs to U+FFFD (never silently dropped)."""
    return text.replace("\x00", "\ufffd") if "\x00" in text else text


def open_pdf(data: bytes) -> fitz.Document:
    """Open PDF bytes; PDF_001 on malformed input, PDF_002 when encrypted.

    Password support is deliberately NOT implemented in P03 (the catalog's
    operator action names import options; supplying credentials is a later
    enhancement) — encrypted files fail loudly instead of half-opening.
    """
    if len(data) > MAX_PDF_BYTES:
        raise DomainError("PDF_001", message="PDF exceeds the 100 MiB size limit.")
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - reshaped as PDF_001
        raise DomainError(
            "PDF_001",
            message="PDF file is unreadable or malformed.",
            details={"reason": type(exc).__name__, "size_bytes": len(data)},
        ) from exc
    if doc.needs_pass:
        doc.close()
        raise DomainError(
            "PDF_002",
            message="PDF is encrypted or password-protected.",
            details={"size_bytes": len(data)},
        )
    if doc.page_count > MAX_PDF_PAGES:
        doc.close()
        raise DomainError("PDF_001", message="PDF exceeds the 2000 page limit.")
    return doc


def inspect_document(doc: fitz.Document) -> DocumentInspection:
    """Collect raw inspection facts for every page (no OCR, no decisions)."""
    page_count = doc.page_count
    pages: list[PageInspection] = []
    for index in range(page_count):
        page = doc[index]
        text_dict = page.get_text("dict", sort=True)
        chunks: list[str] = []
        line_bboxes: list[tuple[float, float, float, float]] = []
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans_text = sanitize_pdf_text(
                    "".join(span.get("text", "") for span in line.get("spans", []))
                )
                chunks.append(spans_text)
                bbox = tuple(float(v) for v in line.get("bbox", (0.0, 0.0, 0.0, 0.0)))
                line_bboxes.append(bbox)  # type: ignore[arg-type]
        native_text = "\n".join(chunks)

        images: list[tuple[int, tuple[float, float, float, float] | None]] = []
        image_area = 0.0
        page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
        for info in page.get_image_info(xrefs=True):
            bbox = info.get("bbox")
            bbox_tuple: tuple[float, float, float, float] | None = None
            if bbox is not None:
                bbox_tuple = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
                width = max(0.0, bbox_tuple[2] - bbox_tuple[0])
                height = max(0.0, bbox_tuple[3] - bbox_tuple[1])
                image_area += width * height
            images.append((int(info.get("xref", 0)), bbox_tuple))

        pages.append(
            PageInspection(
                page_number=index + 1,
                width_pt=float(page.rect.width),
                height_pt=float(page.rect.height),
                native_text=native_text,
                line_bboxes=tuple(line_bboxes),
                images=tuple(images),
                image_area_ratio=min(1.0, image_area / page_area),
                is_blank=not native_text.strip() and not images,
            )
        )

    metadata = {str(k): v for k, v in (doc.metadata or {}).items() if v}
    return DocumentInspection(
        page_count=page_count,
        pages=tuple(pages),
        metadata=metadata,
        is_zero_page=page_count == 0,
    )


def render_page_png(doc: fitz.Document, page_number: int, dpi: int = OCR_RENDER_DPI) -> bytes:
    """Render one page to PNG bytes for the OCR provider (TEMPORARY raster).

    ``page_number`` is 1-based; an out-of-range request is a programming
    error surfaced as ValueError (callers iterate inspected pages).
    """
    if not 1 <= page_number <= doc.page_count:
        raise ValueError(f"page_number {page_number} out of range 1..{doc.page_count}")
    page = doc[page_number - 1]
    _check_render_size(page.rect, dpi)
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    return pixmap.tobytes(output="png")


def render_clip_png(
    doc: fitz.Document,
    page_number: int,
    bbox: tuple[float, float, float, float],
    dpi: int = OCR_RENDER_DPI,
) -> bytes:
    """Render a page region (PDF point bbox) to PNG — used for figure and
    table-image crops, which ARE canonical assets (figures/table_images)."""
    if not 1 <= page_number <= doc.page_count:
        raise ValueError(f"page_number {page_number} out of range 1..{doc.page_count}")
    page = doc[page_number - 1]
    clip = fitz.Rect(*bbox) & page.rect
    if clip.is_empty:
        raise ValueError(f"empty clip bbox {bbox} on page {page_number}")
    _check_render_size(clip, dpi)
    pixmap = page.get_pixmap(dpi=dpi, alpha=False, clip=clip)
    return pixmap.tobytes(output="png")


def _check_render_size(rect: fitz.Rect, dpi: int) -> None:
    dimensions = (rect.width * dpi / 72, rect.height * dpi / 72)
    if dpi <= 0 or any(not isfinite(d) or d <= 0 for d in dimensions):
        raise DomainError("PDF_001", message="Invalid raster dimensions.")
    if (ceil(dimensions[0]) + 1) * (ceil(dimensions[1]) + 1) > MAX_RENDER_PIXELS:
        raise DomainError("PDF_001", message="PDF raster exceeds the 25 megapixel limit.")

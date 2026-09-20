"""extraction.native — native text-layer extraction with provenance.

Extracts evidence candidates from the PDF's native text layer and embedded
objects "where feasible" (spec doc 05 P03): paragraphs, heading candidates,
captions, tables (PyMuPDF find_tables), figures (embedded image placements),
equation candidates (math-font/symbol heuristic), and reference entries
(document-level References-section tracking).

Segmentation is LINE-RUN based, not block based: PDF producers frequently
merge headings into adjacent paragraph blocks, so every line is classified
first and consecutive same-class lines are grouped into units.

Provenance rules (doc 01 §7): every unit carries page, bbox where available,
source_method=PDF_NATIVE, and a content hash. Heuristic classifications are
FLAGGED (flags list) — they are candidates for P04 structure reconstruction,
never silent final answers.
"""

from __future__ import annotations

import hashlib
import re
import statistics
from typing import Any

import pymupdf as fitz

from paperintel.extraction.pdf_document import sanitize_pdf_text
from paperintel.schemas.common import BBox
from paperintel.schemas.enums import EvidenceType, SourceMethod
from paperintel.schemas.extraction import ExtractedUnit

#: Caption pattern: "Figure 3: ...", "Table 2. ...", "Fig. 1 — ..."
_CAPTION_RE = re.compile(
    r"^(figure|fig\.|table|tab\.|listing|algorithm|chart|scheme|plate)\s*[\dIVXivx]+",
    re.IGNORECASE,
)
#: References-section heading pattern.
_REFERENCES_HEADING_RE = re.compile(r"^(references|bibliography|works?\s+cited)$", re.IGNORECASE)
#: Reference entry pattern (bracketed/numbered or author-year leads).
_REFERENCE_ENTRY_RE = re.compile(
    r"^(\[?\d{1,3}\]?\.?\s+\S|doi[:.]|https?://|arxiv[:.])", re.IGNORECASE
)
#: Unicode ranges typical of mathematical typesetting.
_MATH_CODEPOINT_RANGES = (
    (0x2200, 0x22FF),  # mathematical operators
    (0x2A00, 0x2AFF),  # supplemental operators
    (0x0391, 0x03C9),  # Greek
    (0x2190, 0x21FF),  # arrows
    (0x2070, 0x209F),  # super/subscripts
)
#: Font-name fragments that indicate math typesetting fonts.
_MATH_FONT_FRAGMENTS = ("math", "cmsy", "cmex", "cmmi", "symbol", "msbm", "esint")
#: Embedded images smaller than this fraction of page area are decorations
#: (rules, logos) rather than figure evidence.
MIN_FIGURE_AREA_RATIO = 0.02
#: Blocks whose text is dominated by math codepoints/fonts at this ratio are
#: equation candidates.
EQUATION_SYMBOL_RATIO = 0.25
#: Caption units absorb at most this many continuation lines.
MAX_CAPTION_LINES = 4

_LineInfo = tuple[str, list[dict[str, Any]], BBox | None]


def _is_noise_line(text: str) -> bool:
    """A line carrying no mappable characters (only whitespace / U+FFFD)
    carries no evidence and is excluded from units — its glyphs are already
    represented in the page metrics via the replacement-char ratio."""
    return not text.strip().strip("\ufffd").strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bbox_from(value: Any) -> BBox | None:
    """Coerce a PyMuPDF bbox tuple to the contract BBox model.

    Inverted or non-numeric geometry is rejected (None); slight negative
    bleed is clamped to 0 (BBox requires non-negative coordinates).
    """
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if x1 < x0 or y1 < y0:
        return None
    x0 = max(0.0, x0)
    y0 = max(0.0, y0)
    x1 = max(x0, x1)
    y1 = max(y0, y1)
    try:
        return BBox(x0=x0, y0=y0, x1=x1, y1=y1)
    except ValueError:
        return None


def _union_bbox(boxes: list[BBox | None]) -> BBox | None:
    valid = [b for b in boxes if b is not None]
    if not valid:
        return None
    return BBox(
        x0=min(b.x0 for b in valid),
        y0=min(b.y0 for b in valid),
        x1=max(b.x1 for b in valid),
        y1=max(b.y1 for b in valid),
    )


def _math_ratio(spans: list[dict[str, Any]]) -> tuple[float, bool]:
    """(math-codepoint ratio, math-font present) over the given spans."""
    total = 0
    math_chars = 0
    math_font = False
    for span in spans:
        text = span.get("text", "")
        font_name = str(span.get("font", "")).lower()
        if any(fragment in font_name for fragment in _MATH_FONT_FRAGMENTS):
            math_font = True
        for ch in text:
            if ch.isspace():
                continue
            total += 1
            code = ord(ch)
            if any(low <= code <= high for low, high in _MATH_CODEPOINT_RANGES):
                math_chars += 1
    ratio = (math_chars / total) if total else 0.0
    return ratio, math_font


def _collect_body_font_size(doc: fitz.Document, max_pages: int = 30) -> float | None:
    """Median span font size across leading pages = body text size proxy."""
    sizes: list[float] = []
    for index in range(min(doc.page_count, max_pages)):
        page = doc[index]
        for block in page.get_text("dict", sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    size = float(span.get("size", 0.0))
                    text = str(span.get("text", "")).strip()
                    if size > 0 and text:
                        sizes.extend([size] * len(text))
    if not sizes:
        return None
    return statistics.median(sizes)


def _is_heading_line(text: str, spans: list[dict[str, Any]], body_size: float | None) -> bool:
    """Heading heuristic for ONE line: larger-or-bold, short, not a clause.

    Flagged as heuristic downstream; P04 structure reconstruction makes the
    final hierarchy decisions.
    """
    stripped = text.strip()
    if not stripped or len(stripped) > 160:
        return False
    if stripped.endswith((",", ";", ":")):
        return False  # continuation of a sentence, not a heading
    if body_size is None:
        return False
    max_size = max((float(span.get("size", 0.0)) for span in spans), default=0.0)
    bold = any("bold" in str(span.get("font", "")).lower() for span in spans)
    if max_size >= body_size * 1.2:
        return True
    if bold and max_size >= body_size * 1.02 and len(stripped) <= 120:
        return True
    return False


def _classify_line(
    text: str,
    spans: list[dict[str, Any]],
    body_size: float | None,
    include_equations: bool,
) -> tuple[str, list[str]]:
    """Classify one text line: EQUATION / CAPTION / HEADING / BODY.

    Equation check precedes the heading heuristic: display equations often
    use larger math fonts and would otherwise masquerade as headings.
    """
    stripped = text.strip()
    if include_equations:
        ratio, math_font = _math_ratio(spans)
        if ratio >= EQUATION_SYMBOL_RATIO or (math_font and ratio >= 0.1):
            return "EQUATION", ["heuristic:equation", f"math_ratio:{ratio:.2f}"]
    if _CAPTION_RE.match(stripped) and len(stripped) < 1200:
        return "CAPTION", []
    if _is_heading_line(stripped, spans, body_size):
        return "HEADING", ["heuristic:heading"]
    return "BODY", []


def _make_unit(
    unit_type: EvidenceType,
    page_number: int,
    lines: list[_LineInfo],
    flags: list[str],
) -> ExtractedUnit:
    text = "\n".join(line[0].strip() for line in lines).strip()
    return ExtractedUnit(
        unit_type=unit_type,
        page_number=page_number,
        bbox=_union_bbox([line[2] for line in lines]),
        text=text,
        source_method=SourceMethod.PDF_NATIVE,
        content_sha256=_sha256_text(text),
        ordinal=0,  # reassigned by reading-order sort
        flags=list(flags),
    )


def _segment_block_lines(
    line_infos: list[_LineInfo],
    *,
    page_number: int,
    body_size: float | None,
    include_equations: bool,
    in_references: bool,
) -> tuple[list[ExtractedUnit], bool]:
    """Group one block's classified lines into units.

    Returns (units, in_references_after). References tracking: a References
    heading opens the section; any other heading closes it; body lines inside
    the section become REFERENCE entries (new entry at each bracketed/numbered
    lead, continuation lines appended).
    """
    units: list[ExtractedUnit] = []
    classified: list[tuple[str, list[str], _LineInfo]] = []
    for text, spans, bbox in line_infos:
        kind, flags = _classify_line(text, spans, body_size, include_equations)
        classified.append((kind, flags, (text, spans, bbox)))

    i = 0
    while i < len(classified):
        kind, flags, info = classified[i]
        if kind == "HEADING":
            run = [info]
            j = i + 1
            while j < len(classified) and classified[j][0] == "HEADING":
                run.append(classified[j][2])
                j += 1
            unit = _make_unit(EvidenceType.HEADING, page_number, run, flags)
            heading_text = (unit.text or "").strip().rstrip(":.")
            if _REFERENCES_HEADING_RE.match(heading_text):
                in_references = True
            else:
                in_references = False
            units.append(unit)
            i = j
        elif kind == "EQUATION":
            units.append(_make_unit(EvidenceType.EQUATION, page_number, [info], flags))
            i += 1
        elif kind == "CAPTION":
            run = [info]
            j = i + 1
            while (
                j < len(classified) and classified[j][0] == "BODY" and len(run) < MAX_CAPTION_LINES
            ):
                run.append(classified[j][2])
                j += 1
            units.append(_make_unit(EvidenceType.CAPTION, page_number, run, flags))
            i = j
        else:  # BODY run
            run = [info]
            j = i + 1
            while j < len(classified) and classified[j][0] == "BODY":
                run.append(classified[j][2])
                j += 1
            if in_references:
                units.extend(_reference_units(run, page_number))
            else:
                units.append(_make_unit(EvidenceType.PARAGRAPH, page_number, run, []))
            i = j
    return units, in_references


def _reference_units(run: list[_LineInfo], page_number: int) -> list[ExtractedUnit]:
    """Split a body run inside the references section into entries."""
    entries: list[list[_LineInfo]] = []
    for info in run:
        text = info[0].strip()
        if _REFERENCE_ENTRY_RE.match(text) or not entries:
            entries.append([info])
        else:
            entries[-1].append(info)  # wrapped continuation of previous entry
    return [
        _make_unit(EvidenceType.REFERENCE, page_number, entry, ["context:references-section"])
        for entry in entries
    ]


def _table_cell_text(table: Any) -> tuple[str, int, int]:
    """Serialize a detected table deterministically: one row per line,
    cells joined with ' | ' (None cells become '')."""
    extracted = table.extract()
    rows: list[str] = []
    cols = 0
    for row in extracted:
        cells = ["" if cell is None else " ".join(str(cell).split()) for cell in row]
        cols = max(cols, len(cells))
        rows.append(" | ".join(cells))
    return "\n".join(rows), len(rows), cols


def extract_document_units(
    doc: fitz.Document,
    *,
    include_equations: bool = True,
) -> dict[int, list[ExtractedUnit]]:
    """Extract native units for every page.

    Returns {1-based page number: [units in reading order]}. References
    tracking is document-level across pages.
    """
    body_size = _collect_body_font_size(doc)
    in_references = False
    per_page: dict[int, list[ExtractedUnit]] = {}

    for index in range(doc.page_count):
        page = doc[index]
        page_number = index + 1
        page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
        units: list[ExtractedUnit] = []

        # --- tables first: their text blocks are excluded from paragraphs ---
        table_bboxes: list[BBox] = []
        try:
            tables = page.find_tables()
        except Exception:  # noqa: BLE001 - table detection is best-effort
            tables = None
        if tables is not None:
            for table in tables.tables:
                bbox = _bbox_from(table.bbox)
                cell_text, row_count, col_count = _table_cell_text(table)
                if bbox is not None:
                    table_bboxes.append(bbox)
                units.append(
                    ExtractedUnit(
                        unit_type=EvidenceType.TABLE,
                        page_number=page_number,
                        bbox=bbox,
                        text=cell_text,
                        source_method=SourceMethod.PDF_NATIVE,
                        content_sha256=_sha256_text(cell_text),
                        ordinal=0,  # reassigned by reading-order sort below
                        flags=[
                            f"table:{row_count}x{col_count}",
                            "detector:pymupdf.find_tables",
                        ],
                    )
                )

        def _inside_table(bbox: BBox, boxes: list[BBox]) -> bool:
            cx = (bbox.x0 + bbox.x1) / 2
            cy = (bbox.y0 + bbox.y1) / 2
            return any(b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1 for b in boxes)

        # --- text blocks: line-run segmentation ---
        for block in page.get_text("dict", sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            block_bbox = _bbox_from(block.get("bbox"))
            if block_bbox is not None and _inside_table(block_bbox, table_bboxes):
                continue  # already captured by the TABLE unit
            line_infos: list[_LineInfo] = []
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = sanitize_pdf_text("".join(span.get("text", "") for span in spans))
                if not _is_noise_line(text):
                    line_infos.append((text, spans, _bbox_from(line.get("bbox"))))
            if not line_infos:
                continue
            block_units, in_references = _segment_block_lines(
                line_infos,
                page_number=page_number,
                body_size=body_size,
                include_equations=include_equations,
                in_references=in_references,
            )
            units.extend(block_units)

        # --- figures (embedded image placements) ---
        seen_placements: set[tuple[float, float, float, float]] = set()
        for info in page.get_image_info(xrefs=True):
            bbox = _bbox_from(info.get("bbox"))
            if bbox is None:
                continue
            width = bbox.x1 - bbox.x0
            height = bbox.y1 - bbox.y0
            if (width * height) / page_area < MIN_FIGURE_AREA_RATIO:
                continue  # decoration, not figure evidence
            placement = (bbox.x0, bbox.y0, bbox.x1, bbox.y1)
            if placement in seen_placements:
                continue
            seen_placements.add(placement)
            xref = int(info.get("xref", 0))
            asset_sha: str | None = None
            unit_flags = ["detector:image_placement"]
            try:
                if xref:
                    raw = doc.extract_image(xref)
                    asset_sha = _sha256_bytes(raw.get("image", b""))
            except Exception:  # noqa: BLE001 - image extraction is best-effort
                unit_flags.append("image_bytes_unavailable")
            content_key = asset_sha or (
                f"figure:{page_number}:{bbox.x0:.1f},{bbox.y0:.1f},{bbox.x1:.1f},{bbox.y1:.1f}"
            )
            units.append(
                ExtractedUnit(
                    unit_type=EvidenceType.FIGURE,
                    page_number=page_number,
                    bbox=bbox,
                    text=None,
                    source_method=SourceMethod.PDF_NATIVE,
                    asset_sha256=asset_sha,
                    content_sha256=(asset_sha if asset_sha else _sha256_text(str(content_key))),
                    ordinal=0,
                    flags=unit_flags,
                )
            )

        # --- reading-order ordinals (top-to-bottom, left-to-right on ties) ---
        def _sort_key(unit: ExtractedUnit) -> tuple[float, float, int]:
            if unit.bbox is None:
                return (float("inf"), 0.0, 1)
            return (round(unit.bbox.y0 / 4.0), unit.bbox.x0, 0)

        units.sort(key=_sort_key)
        per_page[page_number] = [
            unit.model_copy(update={"ordinal": position}) for position, unit in enumerate(units)
        ]

    return per_page

"""Deterministic fixture PDF generators F01–F06 (spec doc 06 §2).

Fixtures are generated at test time (content-deterministic; byte hashes are
NOT pinned — golden tests compare structured facts, not writing style).

- F01 native text PDF: headings, paragraphs, citations;
- F02 scan-only PDF: image-only pages (no text layer) requiring OCR;
- F03 mixed PDF: native page + scanned page + mixed page;
- F04 rich scientific PDF: table, equation, figure, multi-column text;
- F05 failure PDFs: corrupt bytes, encrypted file, zero-page doc, unusual
  encoding/metadata;
- F06 synthetic truth PDF: purpose-built paper with the exact frozen facts
  (dataset 1,000 / accuracy 82.5% / improvement 2.1 pp / hypothesis /
  limitation / technique / one table).
"""

from __future__ import annotations

import pymupdf as fitz

PAGE_WIDTH = 595.0  # A4 portrait points
PAGE_HEIGHT = 842.0
MARGIN = 56.0

TRUTH_FACTS = {
    "dataset_size": "We evaluate on a dataset of exactly 1,000 samples.",
    "accuracy": "Method A reaches 82.5% accuracy on the benchmark.",
    "improvement": "This is an improvement of exactly 2.1 percentage points over the baseline.",
    "hypothesis": "We hypothesize that method A outperforms method B on the benchmark.",
    "limitation": "Limitation: the synthetic setting may not transfer to real corpora.",
    "technique": "The experiment technique is gradient descent with momentum.",
}


def _new_doc() -> fitz.Document:
    doc = fitz.open()
    doc.set_metadata(
        {
            "format": "PDF 1.7",
            "producer": "paperintel-fixture-generator",
            "creator": "paperintel-fixture-generator",
        }
    )
    return doc


def _insert_lines(
    page: fitz.Page,
    lines: list[tuple[str, float, bool]],
    start_y: float = MARGIN,
) -> float:
    """Insert (text, fontsize, bold) lines; returns the next free y."""
    y = start_y
    for text, size, bold in lines:
        fontname = "hebo" if bold else "helv"
        page.insert_text(
            (MARGIN, y),
            text,
            fontsize=size,
            fontname=fontname,
        )
        y += size * 1.6
    return y


def _paragraph(
    page: fitz.Page, text: str, y: float, width: float | None = None, fontsize: float = 10.5
) -> float:
    rect = fitz.Rect(MARGIN, y, (width or PAGE_WIDTH - MARGIN), y + 200)
    page.insert_textbox(rect, text, fontsize=fontsize, fontname="helv", align=0)
    # insert_textbox returns unused height; approximate consumed height.
    line_count = max(1, len(text) // 78 + text.count("\n") + 1)
    return y + line_count * fontsize * 1.45


# ---------------------------------------------------------------------------
# F01 — native text PDF
# ---------------------------------------------------------------------------


def build_f01_native() -> bytes:
    """Clean digital paper: title, abstract, sections, citations (2 pages)."""
    doc = _new_doc()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = _insert_lines(
        page,
        [
            ("Native Extraction: A Clean Digital Paper", 17.0, True),
            ("Alice Author, Bob Collaborator", 11.0, False),
        ],
    )
    y += 12
    y = _insert_lines(page, [("Abstract", 13.0, True)], start_y=y)
    y = _paragraph(
        page,
        "This paper studies deterministic extraction from clean digital PDF "
        "files. We describe the fixture construction and verify that native "
        "text layers are extracted without OCR (Author and Collaborator, 2024).",
        y,
    )
    y += 16
    y = _insert_lines(page, [("1. Introduction", 13.0, True)], start_y=y)
    y = _paragraph(
        page,
        "Extraction quality gates depend on stable fixtures. This document "
        "provides headings, paragraphs, and citations for the native path. "
        "Prior work established content-addressed storage guarantees [1]. "
        "The remainder of this section describes our contributions in detail "
        "with several sentences of ordinary body text for density purposes.",
        y,
    )
    y += 16
    y = _insert_lines(page, [("2. Method", 13.0, True)], start_y=y)
    _paragraph(
        page,
        "The method renders each fixture deterministically with PyMuPDF. "
        "Font sizes distinguish headings from body text. Paragraph blocks "
        "are separated by vertical space so block detection is stable.",
        y,
    )

    page2 = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = _insert_lines(page2, [("3. Evaluation", 13.0, True)])
    y = _paragraph(
        page2,
        "We evaluate the extractor on this document. Every heading must be "
        "found; every paragraph must keep its page number and bounding box. "
        "The second page proves pagination handling works correctly here.",
        y,
    )
    y += 16
    y = _insert_lines(page2, [("References", 13.0, True)], start_y=y)
    _insert_lines(
        page2,
        [
            (
                "[1] A. Author. Content addressing for documents. Journal of Testing, 2023.",
                10.0,
                False,
            ),
            ("[2] B. Collaborator. Deterministic fixtures. Proc. TestEng, 2024.", 10.0, False),
        ],
        start_y=y,
    )
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


# ---------------------------------------------------------------------------
# F02 — scan-only PDF (image-only pages, no text layer)
# ---------------------------------------------------------------------------

_SCAN_PAGE_TEXT = (
    "Scanned Page {n}\n"
    "This page simulates a paper scan without any text layer.\n"
    "The extractor must classify it SCANNED and request OCR.\n"
    "Sample numbers: 1,000 samples and 82.5% accuracy appear here.\n"
)


def _render_text_png(
    text: str, width_pt: float = PAGE_WIDTH, height_pt: float = PAGE_HEIGHT
) -> bytes:
    """Render text into a PNG (used to build image-only 'scanned' pages)."""
    doc = fitz.open()
    page = doc.new_page(width=width_pt, height=height_pt)
    page.insert_textbox(
        fitz.Rect(40, 40, width_pt - 40, height_pt - 40),
        text,
        fontsize=13,
        fontname="helv",
    )
    pixmap = page.get_pixmap(dpi=150, alpha=False)
    png = pixmap.tobytes(output="png")
    doc.close()
    return png


def build_f02_scanned(pages: int = 2) -> bytes:
    doc = _new_doc()
    for n in range(1, pages + 1):
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        png = _render_text_png(_SCAN_PAGE_TEXT.format(n=n))
        page.insert_image(fitz.Rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT), stream=png)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


# ---------------------------------------------------------------------------
# F03 — mixed PDF (native page + scanned page + mixed page)
# ---------------------------------------------------------------------------


def build_f03_mixed() -> bytes:
    doc = _new_doc()
    # Page 1: native text.
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = _insert_lines(page, [("Mixed Document Fixture", 16.0, True)])
    y = _insert_lines(page, [("1. Native Section", 13.0, True)], start_y=y + 10)
    _paragraph(
        page,
        "This first page carries a healthy native text layer and must be "
        "classified NATIVE_TEXT without any OCR provider calls whatsoever.",
        y,
    )
    # Page 2: scanned (image only).
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    png = _render_text_png(
        "Scanned Section\nThis page has no text layer at all and must be "
        "classified SCANNED so the OCR fallback handles it exclusively."
    )
    page.insert_image(fitz.Rect(0, 0, PAGE_WIDTH, PAGE_HEIGHT), stream=png)
    # Page 3: mixed — native caption text + a large image region.
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    _insert_lines(page, [("2. Plate Section", 13.0, True)])
    png = _render_text_png(
        "Region scan: measured latency 42 ms per page under load.",
        width_pt=400,
        height_pt=300,
    )
    page.insert_image(fitz.Rect(MARGIN, 90, MARGIN + 400, 390), stream=png)
    _paragraph(
        page,
        "Figure 1: The plate above was scanned separately from the body "
        "text, so this page combines a native text layer with an image "
        "region that requires OCR to become searchable text content.",
        420,
    )
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


# ---------------------------------------------------------------------------
# F04 — rich scientific PDF (table, equation, figure, multi-column)
# ---------------------------------------------------------------------------


def _draw_table(
    page: fitz.Page,
    origin: tuple[float, float],
    rows: list[list[str]],
    col_widths: list[float],
    row_height: float = 22.0,
) -> fitz.Rect:
    """Draw a ruled table (find_tables-detectable) with cell text."""
    x0, y0 = origin
    total_width = sum(col_widths)
    total_height = row_height * len(rows)
    for r in range(len(rows) + 1):
        y = y0 + r * row_height
        page.draw_line((x0, y), (x0 + total_width, y), width=0.8)
    c = x0
    for w in col_widths:
        page.draw_line((c, y0), (c, y0 + total_height), width=0.8)
        page.draw_line((c + w, y0), (c + w, y0 + total_height), width=0.8)
        c += w
    for r, row in enumerate(rows):
        cx = x0
        for cell, w in zip(row, col_widths, strict=False):
            page.insert_text((cx + 5, y0 + r * row_height + 15), cell, fontsize=9.5)
            cx += w
    return fitz.Rect(x0, y0, x0 + total_width, y0 + total_height)


def build_f04_rich() -> bytes:
    doc = _new_doc()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = _insert_lines(page, [("Rich Scientific Fixture", 16.0, True)])
    y = _insert_lines(page, [("Table 1: Main results across methods.", 10.0, False)], start_y=y)
    table_rect = _draw_table(
        page,
        (MARGIN, y + 6),
        [
            ["Method", "Accuracy", "Delta"],
            ["Baseline", "80.4%", "-"],
            ["Method A", "82.5%", "+2.1"],
        ],
        [140.0, 140.0, 120.0],
    )
    y = table_rect.y1 + 24
    y = _insert_lines(page, [("3. Formal Analysis", 13.0, True)], start_y=y)
    # Equation: html box keeps unicode math codepoints in the text layer.
    page.insert_htmlbox(
        fitz.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 44),
        "&#8721;<sub>i</sub> &#945;<sub>i</sub>x<sub>i</sub> + &#8747;&#946;(t) dt = &#947; &#8704;x &#8707;y",
    )
    y += 60
    # Figure: vector drawing + raster region.
    page.draw_circle((MARGIN + 80, y + 60), 46, color=(0.1, 0.2, 0.7), width=2)
    page.draw_line((MARGIN + 20, y + 110), (MARGIN + 150, y + 10), color=(0.7, 0.1, 0.1), width=2)
    png = _render_text_png("plot region", width_pt=180, height_pt=140)
    page.insert_image(fitz.Rect(MARGIN + 180, y, MARGIN + 360, y + 140), stream=png)
    y += 160
    _insert_lines(page, [("Figure 1: Vector plot with raster inset.", 10.0, False)], start_y=y)

    # Page 2: two-column body text.
    page2 = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    col_width = (PAGE_WIDTH - 3 * MARGIN) / 2
    for column, x in enumerate((MARGIN, MARGIN * 2 + col_width)):
        page2.insert_textbox(
            fitz.Rect(x, MARGIN, x + col_width, PAGE_HEIGHT - MARGIN),
            (f"Column {column + 1} text. " * 60),
            fontsize=10,
            fontname="helv",
        )
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


# ---------------------------------------------------------------------------
# F05 — failure PDFs
# ---------------------------------------------------------------------------


def build_f05_corrupt() -> bytes:
    """Header-looking bytes with a garbage body: must fail PDF_001."""
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\nthis file was truncated and is not a valid pdf\n"


def build_f05_encrypted() -> bytes:
    """Password-protected document: must fail PDF_002 (needs_pass)."""
    doc = _new_doc()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    _insert_lines(page, [("Encrypted Fixture", 16.0, True)])
    data = doc.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    doc.close()
    return data


def build_f05_zero_page() -> bytes:
    """Valid PDF structure with an empty page tree (zero pages).

    PyMuPDF refuses to SAVE a zero-page document, so the minimal file is
    assembled by hand: catalog + /Pages with /Kids[] /Count 0. MuPDF opens
    it with page_count == 0 — the zero-page detection path.
    """
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[]/Count 0>>"]
    body = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(body))
        body += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(body)
    size = len(offsets) + 1
    body += b"xref\n" + f"0 {size}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        body += f"{off:010d} 00000 n \n".encode()
    body += (
        b"trailer\n<</Size "
        + str(size).encode()
        + b"/Root 1 0 R>>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )
    return body


def build_f05_unusual_encoding() -> bytes:
    """Weird metadata + CJK/RTL/symbol text: must open and extract safely."""
    doc = _new_doc()
    doc.set_metadata(
        {
            "title": "Un\u00eetesing — «weird» tïtlé with NBSP",
            "author": "\u4f5c\u8005\u540d",
            "subject": "",
            "producer": "pymupdf",
        }
    )
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    page.insert_htmlbox(
        fitz.Rect(MARGIN, MARGIN, PAGE_WIDTH - MARGIN, 300),
        "CJK: \u6d4b\u8bd5\u6587\u672c &#183; RTL: \u05e8\u05e7 &#183; Symbols: \u00a9\u00ae\u2122\u00b1\u2264\u2265",
    )
    _insert_lines(
        page,
        [("Unusual encoding fixture body text with enough characters to be dense.", 11.0, False)],
        start_y=340,
    )
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


# ---------------------------------------------------------------------------
# F06 — synthetic truth PDF
# ---------------------------------------------------------------------------


def build_f06_truth() -> bytes:
    """Purpose-built paper with the frozen known facts (doc 06 §2 F06)."""
    doc = _new_doc()
    doc.set_metadata({"title": "Synthetic Truth: A Deterministic Evaluation Paper"})
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = _insert_lines(
        page,
        [
            ("Synthetic Truth: A Deterministic Evaluation Paper", 17.0, True),
            ("PaperIntel Fixture Author", 11.0, False),
        ],
    )
    y += 10
    y = _insert_lines(page, [("Abstract", 13.0, True)], start_y=y)
    y = _paragraph(
        page,
        f"{TRUTH_FACTS['hypothesis']} {TRUTH_FACTS['dataset_size']} "
        f"{TRUTH_FACTS['accuracy']} {TRUTH_FACTS['improvement']}",
        y,
    )
    y += 18
    y = _insert_lines(page, [("1. Method", 13.0, True)], start_y=y)
    y = _paragraph(page, TRUTH_FACTS["technique"], y)
    y += 18
    y = _insert_lines(page, [("2. Results", 13.0, True)], start_y=y)
    y = _insert_lines(page, [("Table 1: Main results.", 10.0, False)], start_y=y)
    table_rect = _draw_table(
        page,
        (MARGIN, y + 6),
        [
            ["Method", "Accuracy"],
            ["Baseline", "80.4%"],
            ["Method A", "82.5%"],
        ],
        [180.0, 160.0],
    )
    y = table_rect.y1 + 24
    y = _insert_lines(page, [("3. Limitations", 13.0, True)], start_y=y)
    y = _paragraph(page, TRUTH_FACTS["limitation"], y)
    y += 18
    y = _insert_lines(page, [("References", 13.0, True)], start_y=y)
    _insert_lines(
        page,
        [
            (
                "[1] Fixture Author. Synthetic evaluation. Journal of Determinism, 2024.",
                10.0,
                False,
            ),
            ("[2] Second Author. Golden files. Proc. TestEng, 2025.", 10.0, False),
        ],
        start_y=y,
    )
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


FIXTURE_BUILDERS = {
    "F01": build_f01_native,
    "F02": build_f02_scanned,
    "F03": build_f03_mixed,
    "F04": build_f04_rich,
    "F06": build_f06_truth,
}


# ---------------------------------------------------------------------------
# Extra classifier fixtures (not part of the F01–F06 gate set; used by unit
# tests for BROKEN_TEXT_LAYER and IMAGE_HEAVY modes)
# ---------------------------------------------------------------------------


def build_fx_broken_text(kind: str = "repeat") -> bytes:
    """A page whose native text layer is garbage.

    kind="repeat": thousands of '.' characters (degenerate glyph flood);
    kind="replacement": U+FFFD flood (broken ToUnicode mapping simulation).
    """
    doc = _new_doc()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    if kind == "repeat":
        flood = "." * 3000
    elif kind == "replacement":
        flood = "\ufffd" * 1200
    else:  # pragma: no cover - test misuse
        raise ValueError(kind)
    page.insert_htmlbox(fitz.Rect(MARGIN, MARGIN, PAGE_WIDTH - MARGIN, 700), flood)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


def build_fx_image_heavy() -> bytes:
    """Full-bleed raster plate page with a small native caption: the image
    dominates (>= IMAGE_HEAVY_AREA_RATIO) while healthy text exists."""
    doc = _new_doc()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    png = _render_text_png("Full page scientific plate artwork.", width_pt=500, height_pt=700)
    page.insert_image(fitz.Rect(40, 40, PAGE_WIDTH - 40, 740), stream=png)
    page.insert_text((MARGIN, 780), "Figure 9: The dominant plate above.", fontsize=10.5)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data

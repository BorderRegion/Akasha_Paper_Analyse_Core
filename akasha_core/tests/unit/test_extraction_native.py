"""Native extraction unit tests (P03): paragraphs, headings, captions,
tables, figures, equation candidates, references, reading order, hashes."""

from __future__ import annotations

import pytest
from tests.fixtures.generators import (
    TRUTH_FACTS,
    build_f01_native,
    build_f04_rich,
    build_f06_truth,
)

from paperintel.extraction.native import extract_document_units
from paperintel.extraction.pdf_document import inspect_document, open_pdf
from paperintel.schemas.enums import EvidenceType, SourceMethod


def _units(pdf_bytes: bytes) -> dict[int, list]:
    doc = open_pdf(pdf_bytes)
    try:
        return extract_document_units(doc)
    finally:
        doc.close()


def _all(units: dict[int, list]) -> list:
    return [unit for page_units in units.values() for unit in page_units]


def _norm(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# F01: clean digital paper
# ---------------------------------------------------------------------------


def test_f01_headings_paragraphs_references() -> None:
    units = _units(build_f01_native())
    all_units = _all(units)
    headings = [u.text for u in all_units if u.unit_type is EvidenceType.HEADING]
    assert any("Native Extraction" in h for h in headings)
    assert any("Introduction" in h for h in headings)
    assert any("Method" in h for h in headings)
    references = [u for u in all_units if u.unit_type is EvidenceType.REFERENCE]
    assert len(references) == 2
    assert all(u.text.startswith("[") for u in references)
    paragraphs = [u for u in all_units if u.unit_type is EvidenceType.PARAGRAPH]
    assert len(paragraphs) >= 4
    # Provenance: native extraction, page numbers, bboxes, hashes.
    for unit in all_units:
        assert unit.source_method is SourceMethod.PDF_NATIVE
        assert unit.page_number in (1, 2)
        assert len(unit.content_sha256) == 64
        if unit.text:
            assert unit.bbox is not None


def test_units_located_on_correct_pages() -> None:
    units = _units(build_f01_native())
    page1_text = " ".join(u.text or "" for u in units[1])
    page2_text = " ".join(u.text or "" for u in units[2])
    assert "Introduction" in page1_text
    assert "Evaluation" in page2_text
    assert "References" in page2_text


def test_reading_order_ordinals_are_sequential() -> None:
    units = _units(build_f01_native())
    for page_units in units.values():
        assert [u.ordinal for u in page_units] == list(range(len(page_units)))
        # reading order: non-decreasing y (within the 4pt band quantization)
        ys = [u.bbox.y0 for u in page_units if u.bbox is not None]
        assert ys == sorted(ys)


def test_bboxes_are_inside_their_pages() -> None:
    doc = open_pdf(build_f01_native())
    try:
        inspection = inspect_document(doc)
        units = extract_document_units(doc)
    finally:
        doc.close()
    for page in inspection.pages:
        for unit in units[page.page_number]:
            if unit.bbox is None:
                continue
            assert unit.bbox.x0 >= 0 and unit.bbox.y0 >= 0
            assert unit.bbox.x1 <= page.width_pt + 2
            assert unit.bbox.y1 <= page.height_pt + 2
            assert unit.bbox.x1 >= unit.bbox.x0


def test_content_hashes_are_deterministic_and_content_bound() -> None:
    first = _all(_units(build_f01_native()))
    second = _all(_units(build_f01_native()))
    assert [u.content_sha256 for u in first] == [u.content_sha256 for u in second]
    # Identical text MUST hash identically (content addressing is the point):
    # both documents contain a "References" heading.
    hashes_first = {u.content_sha256 for u in first}
    hashes_other = {u.content_sha256 for u in _all(_units(build_f06_truth()))}
    import hashlib

    assert hashlib.sha256(b"References").hexdigest() in hashes_first
    assert hashlib.sha256(b"References").hexdigest() in hashes_other
    # Body paragraphs of different documents never collide.
    paragraphs_first = {u.content_sha256 for u in first if u.unit_type is EvidenceType.PARAGRAPH}
    paragraphs_other = {
        u.content_sha256
        for u in _all(_units(build_f06_truth()))
        if u.unit_type is EvidenceType.PARAGRAPH
    }
    assert not (paragraphs_first & paragraphs_other)


# ---------------------------------------------------------------------------
# F04: rich scientific content
# ---------------------------------------------------------------------------


def test_f04_table_detected_with_cells() -> None:
    units = _all(_units(build_f04_rich()))
    tables = [u for u in units if u.unit_type is EvidenceType.TABLE]
    assert len(tables) == 1
    table = tables[0]
    assert "Method" in table.text
    assert "82.5%" in table.text
    assert "80.4%" in table.text
    assert table.bbox is not None
    assert any(f.startswith("detector:pymupdf.find_tables") for f in table.flags)


def test_table_text_not_duplicated_as_paragraphs() -> None:
    units = _all(_units(build_f04_rich()))
    paragraphs = [u.text or "" for u in units if u.unit_type is EvidenceType.PARAGRAPH]
    # Cell text lives in the TABLE unit, not re-extracted as body paragraphs.
    assert not any("82.5%" in p for p in paragraphs)


def test_f04_figure_detected_with_image_hash() -> None:
    units = _all(_units(build_f04_rich()))
    figures = [u for u in units if u.unit_type is EvidenceType.FIGURE]
    assert len(figures) == 1
    assert figures[0].text is None
    assert figures[0].bbox is not None
    assert figures[0].asset_sha256 is None or len(figures[0].asset_sha256) == 64


def test_f04_captions_detected() -> None:
    units = _all(_units(build_f04_rich()))
    captions = [u.text for u in units if u.unit_type is EvidenceType.CAPTION]
    assert any(c.startswith("Table 1") for c in captions)
    assert any(c.startswith("Figure 1") for c in captions)


def test_f04_equation_candidate_flagged_as_heuristic() -> None:
    units = _all(_units(build_f04_rich()))
    equations = [u for u in units if u.unit_type is EvidenceType.EQUATION]
    assert len(equations) == 1
    assert "heuristic:equation" in equations[0].flags
    assert any(f.startswith("math_ratio:") for f in equations[0].flags)


def test_f04_multicolumn_page_yields_text() -> None:
    units = _units(build_f04_rich())
    page2_text = " ".join(u.text or "" for u in units[2])
    assert "Column 1 text." in page2_text
    assert "Column 2 text." in page2_text


# ---------------------------------------------------------------------------
# F06: synthetic truth facts survive extraction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fact_key", sorted(TRUTH_FACTS))
def test_f06_truth_facts_extracted_verbatim(fact_key: str) -> None:
    units = _all(_units(build_f06_truth()))
    all_text = _norm("\n".join(u.text or "" for u in units))
    assert _norm(TRUTH_FACTS[fact_key]) in all_text


def test_f06_table_and_references_present() -> None:
    units = _all(_units(build_f06_truth()))
    tables = [u for u in units if u.unit_type is EvidenceType.TABLE]
    assert len(tables) == 1
    assert "82.5%" in tables[0].text
    references = [u for u in units if u.unit_type is EvidenceType.REFERENCE]
    assert len(references) == 2
    headings = [u.text for u in units if u.unit_type is EvidenceType.HEADING]
    assert any("Limitations" in h for h in headings)

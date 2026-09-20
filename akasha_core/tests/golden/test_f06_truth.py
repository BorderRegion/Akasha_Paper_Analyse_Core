"""Golden test: F06 synthetic truth PDF (spec doc 06 §2).

"Golden tests compare structured facts and evidence linkage, not writing
style." The expectations live in tests/fixtures/expected/f06_truth.json.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.fixtures.generators import build_f06_truth

from paperintel.extraction.native import extract_document_units
from paperintel.extraction.page_classifier import classify_page
from paperintel.extraction.pdf_document import inspect_document, open_pdf
from paperintel.extraction.report import build_report
from paperintel.ids import new_run_id
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import DataQualityState, PageMode, SourceMethod
from paperintel.schemas.extraction import PageExtraction

EXPECTED_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "expected" / "f06_truth.json"


def _norm(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def extraction() -> tuple:
    data = build_f06_truth()
    doc = open_pdf(data)
    try:
        inspection = inspect_document(doc)
        units = extract_document_units(doc)
        classifications = [classify_page(page) for page in inspection.pages]
    finally:
        doc.close()
    pages = [
        PageExtraction(
            page_number=c.page_number,
            classification=c,
            units=units.get(c.page_number, []),
            page_source_method=SourceMethod.PDF_NATIVE,
        )
        for c in classifications
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
    return inspection, units, report


def test_golden_page_count(extraction, expected) -> None:
    inspection, _, report = extraction
    assert inspection.page_count == expected["page_count"]
    assert report.page_count == expected["page_count"]


def test_golden_facts_present_verbatim(extraction, expected) -> None:
    _, units, _ = extraction
    all_text = _norm(
        "\n".join(unit.text or "" for page_units in units.values() for unit in page_units)
    )
    for fact_key, fact in expected["facts"].items():
        assert _norm(fact) in all_text, f"golden fact missing: {fact_key}"


def test_golden_facts_locatable_with_provenance(extraction, expected) -> None:
    """Every fact must be traceable to a unit with page + bbox + hash
    (evidence linkage, doc 06 §2)."""
    _, units, _ = extraction
    for fact in expected["facts"].values():
        carriers = [
            unit
            for page_units in units.values()
            for unit in page_units
            if unit.text and _norm(fact) in _norm(unit.text)
        ]
        assert carriers, f"no carrier unit for fact: {fact[:40]!r}"
        carrier = carriers[0]
        assert carrier.page_number == 1
        assert carrier.bbox is not None
        assert carrier.source_method is SourceMethod.PDF_NATIVE
        assert len(carrier.content_sha256) == 64


def test_golden_expected_units(extraction, expected) -> None:
    _, units, _ = extraction
    flat = [unit for page_units in units.values() for unit in page_units]
    spec = expected["expected_units"]

    tables = [u for u in flat if u.unit_type.value == "TABLE"]
    assert len(tables) == spec["TABLE"]["count"]
    assert tables[0].page_number == spec["TABLE"]["page"]
    for cell in spec["TABLE"]["cells_contain"]:
        assert cell in tables[0].text

    references = [u for u in flat if u.unit_type.value == "REFERENCE"]
    assert len(references) == spec["REFERENCE"]["count"]
    assert all(u.page_number == spec["REFERENCE"]["page"] for u in references)

    captions = [u for u in flat if u.unit_type.value == "CAPTION"]
    assert len(captions) == spec["CAPTION"]["count"]
    assert captions[0].text.startswith(spec["CAPTION"]["text_starts_with"])

    headings = [_norm(u.text or "") for u in flat if u.unit_type.value == "HEADING"]
    for heading in spec["HEADING"]["contains"]:
        assert any(_norm(heading) in h for h in headings), heading


def test_golden_classification_and_quality(extraction, expected) -> None:
    _, _, report = extraction
    assert all(page.classification.mode is PageMode.NATIVE_TEXT for page in report.pages)
    assert report.quality_state.value == expected["quality_state"]
    assert report.quality_state is DataQualityState.GOOD
    assert report.quality_checks.location_sanity is True
    assert report.quality_checks.page_count_consistent is True

"""P04 unit tests: section reconstruction (pure, no database).

Determinism ("section tree stable"), normalized class mapping, hierarchy
from numeric prefixes, front-matter TITLE section, flat fallback.
"""

from __future__ import annotations

import pytest
from tests.fixtures.generators import build_f01_native, build_f04_rich

from paperintel.extraction.native import extract_document_units
from paperintel.extraction.page_classifier import classify_page
from paperintel.extraction.pdf_document import inspect_document, open_pdf
from paperintel.extraction.report import build_report
from paperintel.ids import new_run_id
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import EvidenceType, SectionClass, SourceMethod
from paperintel.schemas.extraction import ExtractedUnit, PageExtraction
from paperintel.structure.reconstruct import (
    heading_level,
    normalize_heading,
    reconstruct_sections,
    unit_key,
)


def build_report_from(data: bytes):
    doc = open_pdf(data)
    try:
        inspection = inspect_document(doc)
        units = extract_document_units(doc)
        pages = [
            PageExtraction(
                page_number=page.page_number,
                classification=classify_page(page),
                units=units.get(page.page_number, []),
                page_source_method=SourceMethod.PDF_NATIVE,
            )
            for page in inspection.pages
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
    finally:
        doc.close()
    return report


def _draft_shape(drafts):
    return [
        (
            d.ordinal,
            d.original_heading,
            d.normalized_class.value,
            d.level,
            d.parent_ordinal,
            d.page_start,
            d.page_end,
        )
        for d in drafts
    ]


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Abstract", SectionClass.ABSTRACT),
        ("1. Introduction", SectionClass.INTRODUCTION),
        ("Introduction", SectionClass.INTRODUCTION),
        ("2. Related Work", SectionClass.RELATED_WORK),
        ("Prior Work", SectionClass.RELATED_WORK),
        ("3. Background", SectionClass.BACKGROUND),
        ("Preliminaries", SectionClass.BACKGROUND),
        ("4. Method", SectionClass.METHOD),
        ("4.2 Proposed Approach", SectionClass.METHOD),
        ("Our Approach", SectionClass.METHOD),
        ("5. Theory", SectionClass.THEORY),
        ("Theoretical Analysis", SectionClass.THEORY),
        ("6. Experiments", SectionClass.EXPERIMENT),
        ("6.1 Experimental Setup", SectionClass.EXPERIMENT),
        ("Evaluation", SectionClass.EXPERIMENT),
        ("7. Results", SectionClass.RESULT),
        ("Findings", SectionClass.RESULT),
        ("8. Discussion", SectionClass.DISCUSSION),
        ("9. Limitations", SectionClass.LIMITATION),
        ("Threats to Validity", SectionClass.LIMITATION),
        ("10. Conclusion", SectionClass.CONCLUSION),
        ("Concluding Remarks", SectionClass.CONCLUSION),
        ("Future Work", SectionClass.CONCLUSION),
        ("Acknowledgements", SectionClass.ACKNOWLEDGEMENT),
        ("Acknowledgments", SectionClass.ACKNOWLEDGEMENT),
        ("References", SectionClass.REFERENCES),
        ("Bibliography", SectionClass.REFERENCES),
        ("Appendix A. Proofs", SectionClass.APPENDIX),
        ("Supplementary Material", SectionClass.SUPPLEMENTARY),
        ("Supplemental Results", SectionClass.SUPPLEMENTARY),
        ("A Weird Custom Heading", SectionClass.OTHER),
    ],
)
def test_normalize_heading_canonical_classes(heading, expected) -> None:
    assert normalize_heading(heading) is expected


@pytest.mark.parametrize(
    ("heading", "level"),
    [
        ("Introduction", 0),
        ("1. Introduction", 0),
        ("1 Introduction", 0),
        ("2.3 Evaluation Setup", 1),
        ("2.3.1 Datasets", 2),
        ("A.2 Appendix Detail", 1),
        ("IV. Results", 0),
    ],
)
def test_heading_level_from_numeric_prefix(heading, level) -> None:
    assert heading_level(heading) == level


def test_f01_section_tree_shape_and_stability() -> None:
    report = build_report_from(build_f01_native())
    drafts, assignment = reconstruct_sections(report)
    assert drafts, "F01 must reconstruct at least one section"
    # Page-one location alone cannot prove that an unknown heading is a title.
    assert drafts[0].normalized_class is SectionClass.OTHER
    assert "Native Extraction" in drafts[0].original_heading
    # Every unit is assigned to exactly one section.
    total_units = sum(len(p.units) for p in report.pages)
    assert len(assignment) == total_units
    # All classes come from the canonical set; headings map to real classes.
    classes = [d.normalized_class for d in drafts]
    assert SectionClass.REFERENCES in classes
    assert SectionClass.METHOD in classes or SectionClass.INTRODUCTION in classes

    # Stability: identical input → byte-identical structure.
    drafts2, assignment2 = reconstruct_sections(report)
    assert _draft_shape(drafts) == _draft_shape(drafts2)
    assert assignment == assignment2


def test_f01_children_follow_numbered_parents() -> None:
    report = build_report_from(build_f04_rich())
    drafts, _ = reconstruct_sections(report)
    for draft in drafts:
        if draft.parent_ordinal is not None:
            parent = next(d for d in drafts if d.ordinal == draft.parent_ordinal)
            assert parent.level < draft.level


def test_f01_page_spans_are_consistent() -> None:
    report = build_report_from(build_f01_native())
    drafts, assignment = reconstruct_sections(report)
    by_ordinal = {d.ordinal: d for d in drafts}
    for (page, _ordinal), section_ordinal in assignment.items():
        draft = by_ordinal[section_ordinal]
        assert draft.page_start <= page
        assert page <= report.page_count


def test_no_headings_falls_back_to_flat_other() -> None:
    """Paragraph-only report (no HEADING units) → one flat OTHER section."""
    from paperintel.schemas.enums import DataQualityState, PageMode
    from paperintel.schemas.extraction import (
        ExtractionReport,
        PageClassification,
        QualityChecks,
        TextQualityMetrics,
    )

    unit = ExtractedUnit(
        unit_type=EvidenceType.PARAGRAPH,
        page_number=1,
        text="Just body text without any heading structure at all.",
        source_method=SourceMethod.PDF_NATIVE,
        content_sha256="a" * 64,
        ordinal=0,
    )
    metrics = TextQualityMetrics(
        char_count=53,
        visible_char_ratio=1.0,
        replacement_char_ratio=0.0,
        text_density=0.01,
        repeated_glyph_ratio=0.0,
        image_area_ratio=0.0,
        bbox_sane=True,
    )
    page = PageExtraction(
        page_number=1,
        classification=PageClassification(
            page_number=1,
            mode=PageMode.NATIVE_TEXT,
            needs_ocr=False,
            metrics=metrics,
            reasons=["forced"],
        ),
        units=[unit],
        page_source_method=SourceMethod.PDF_NATIVE,
    )
    report = ExtractionReport(
        run_id=new_run_id(),
        paper_version_id=None,
        started_at=utcnow(),
        finished_at=utcnow(),
        page_count=1,
        pages=[page],
        quality_state=DataQualityState.GOOD,
        quality_checks=QualityChecks(
            zero_page_detected=False,
            suspiciously_empty_text=False,
            ocr_confidence_warnings=0,
            page_count_consistent=True,
            location_sanity=True,
        ),
    )
    drafts, assignment = reconstruct_sections(report)
    assert len(drafts) == 1
    assert drafts[0].normalized_class is SectionClass.OTHER
    assert assignment == {unit_key(unit): 0}


def test_unit_key_stable() -> None:
    unit = ExtractedUnit(
        unit_type=EvidenceType.PARAGRAPH,
        page_number=2,
        text="x",
        source_method=SourceMethod.PDF_NATIVE,
        content_sha256="b" * 64,
        ordinal=3,
    )
    assert unit_key(unit) == (2, 3)

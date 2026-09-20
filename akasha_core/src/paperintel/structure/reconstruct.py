"""structure.reconstruct — section hierarchy + normalized classes (P04).

Rebuilds the hierarchical semantic structure from extraction units
(doc 01 §8: hierarchy over fixed chunks; original headings retained
alongside normalized classes; subsections arbitrary and hierarchical).

Determinism contract ("section tree stable" gate test): identical input
units always produce an identical section list — no timestamps, no random
ids inside the drafts, no dict-iteration surprises. Section IDs are minted
only at persistence time.

Heading levels come from numeric prefixes ("2.3.1" → level 2 under "2.3"
under "2"); unnumbered headings are top-level. Front matter (units before
the first heading on the opening pages) forms a synthetic OTHER section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from paperintel.schemas.enums import EvidenceType, SectionClass
from paperintel.schemas.extraction import ExtractedUnit, ExtractionReport

#: Numeric heading prefix: "1.", "2.3", "A.2", "IV." etc.
_NUMBERED_HEADING_RE = re.compile(
    r"^(?P<number>(?:\d+|[A-Z]|[IVX]+)(?:\s*[.\-]\s*(?:\d+|[A-Z]|[IVX]+))*)\s*[.\-:]?\s+\S"
)

#: Ordered normalization rules (doc 01 §8 canonical classes). First match
#: wins; unmatched headings keep their original text under OTHER.
_CLASS_RULES: tuple[tuple[SectionClass, re.Pattern[str]], ...] = (
    (SectionClass.ABSTRACT, re.compile(r"^abstract\b", re.IGNORECASE)),
    (
        SectionClass.INTRODUCTION,
        re.compile(r"^introduction\b", re.IGNORECASE),
    ),
    (
        SectionClass.RELATED_WORK,
        re.compile(
            r"^(related\s+work|related\s+literature|prior\s+work|literature\s+review|background\s+and\s+related)",
            re.IGNORECASE,
        ),
    ),
    (
        SectionClass.LIMITATION,
        re.compile(r"^(limitation|limitations|threats?\s+to\s+validity)\b", re.IGNORECASE),
    ),
    (
        SectionClass.ACKNOWLEDGEMENT,
        re.compile(r"^(acknowledge?ments?|acknowledg?ements?|funding)\b", re.IGNORECASE),
    ),
    (
        SectionClass.REFERENCES,
        re.compile(r"^(references|bibliography|works?\s+cited)\b", re.IGNORECASE),
    ),
    (
        SectionClass.APPENDIX,
        re.compile(r"^appendix\b", re.IGNORECASE),
    ),
    (
        SectionClass.SUPPLEMENTARY,
        re.compile(r"^(supplementary|supplemental)\b", re.IGNORECASE),
    ),
    (
        SectionClass.CONCLUSION,
        re.compile(
            r"^(conclusion|conclusions|concluding\s+remarks?|summary\s+and\s+future|future\s+work)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SectionClass.DISCUSSION,
        re.compile(r"^discussion\b", re.IGNORECASE),
    ),
    (
        SectionClass.RESULT,
        re.compile(r"^(results?|findings)\b", re.IGNORECASE),
    ),
    (
        SectionClass.EXPERIMENT,
        re.compile(
            r"^(experiments?|experimental|evaluation|evaluations?|benchmark(s|ing)?|empirical(\s+results?)?|setup)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SectionClass.THEORY,
        re.compile(
            r"^(theory|theoretical(\s+analysis|\s+foundations)?|formulation|proofs?|preliminaries\s+and\s+theory)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SectionClass.METHOD,
        re.compile(
            r"^(method(s|ology)?|approach|proposed(\s+method|\s+approach|\s+model)?|model(\s+architecture)?|system(\s+design|\s+architecture)?|design|implementation|technique(s)?|framework|our\s+approach)\b",
            re.IGNORECASE,
        ),
    ),
    (
        SectionClass.BACKGROUND,
        re.compile(
            r"^(background|preliminaries|primer|overview\s+of|basics\s+of)\b", re.IGNORECASE
        ),
    ),
)


def normalize_heading(heading: str) -> SectionClass:
    """Map an original heading to its canonical class (OTHER when unknown).

    Numeric prefixes are stripped before matching ("3.2 Evaluation Setup" →
    "Evaluation Setup" → EXPERIMENT).
    """
    text = " ".join(heading.split())
    match = _NUMBERED_HEADING_RE.match(text)
    if match:
        text = text[match.end("number") :].lstrip(".-: ").strip()
    for section_class, pattern in _CLASS_RULES:
        if pattern.match(text):
            return section_class
    return SectionClass.OTHER


def heading_level(heading: str) -> int:
    """Nesting depth from the numeric prefix (0 = top level)."""
    text = " ".join(heading.split())
    match = _NUMBERED_HEADING_RE.match(text)
    if not match:
        return 0
    number = match.group("number")
    parts = [p for p in re.split(r"\s*[.\-]\s*", number) if p]
    return max(0, len(parts) - 1)


def unit_key(unit: ExtractedUnit) -> tuple[int, int]:
    """Stable identity of a unit inside its version: (page, ordinal)."""
    return (unit.page_number, unit.ordinal)


@dataclass(slots=True)
class SectionDraft:
    """One reconstructed section (IDs minted at persistence)."""

    ordinal: int
    original_heading: str
    normalized_class: SectionClass
    level: int
    page_start: int
    page_end: int
    parent_ordinal: int | None
    #: unit_key → assigned to this section.
    unit_keys: list[tuple[int, int]] = field(default_factory=list)


def iter_units_in_reading_order(report: ExtractionReport):
    for page in report.pages:
        yield from sorted(page.units, key=lambda u: u.ordinal)


def reconstruct_sections(
    report: ExtractionReport,
) -> tuple[list[SectionDraft], dict[tuple[int, int], int]]:
    """Build the section forest + unit→section assignment.

    Returns (drafts in document order, {unit_key: draft ordinal}). Every
    unit is assigned to exactly one section; front matter belongs to the
    synthetic OTHER section. When a document has no heading units at all,
    a single OTHER section covers everything (flat structure is the honest
    fallback flagged by STRUCTURE_001 at the caller's discretion — the
    reconstruction itself never raises for sparse input).
    """
    drafts: list[SectionDraft] = []
    assignment: dict[tuple[int, int], int] = {}

    def _open_section(heading: str, page: int, level: int) -> SectionDraft:
        parent_ordinal: int | None = None
        if level > 0:
            # Nearest preceding section with a smaller level is the parent.
            for candidate in reversed(drafts):
                if candidate.level < level:
                    parent_ordinal = candidate.ordinal
                    break
        if heading == _FRONT_MATTER_HEADING:
            normalized = SectionClass.OTHER
        else:
            normalized = normalize_heading(heading)
        draft = SectionDraft(
            ordinal=len(drafts),
            original_heading=heading,
            normalized_class=normalized,
            level=level,
            page_start=page,
            page_end=page,
            parent_ordinal=parent_ordinal,
        )
        drafts.append(draft)
        return draft

    current: SectionDraft | None = None
    front_matter: list[ExtractedUnit] = []
    seen_heading = False

    for unit in iter_units_in_reading_order(report):
        is_heading = unit.unit_type is EvidenceType.HEADING
        if is_heading:
            heading_text = " ".join((unit.text or "").split())
            current = _open_section(heading_text, unit.page_number, heading_level(heading_text))
            current.unit_keys.append(unit_key(unit))
            assignment[unit_key(unit)] = current.ordinal
            seen_heading = True
        elif current is None:
            front_matter.append(unit)
        else:
            current.unit_keys.append(unit_key(unit))
            assignment[unit_key(unit)] = current.ordinal
            current.page_end = max(current.page_end, unit.page_number)

    # Unclassified front matter stays OTHER; page position is not title evidence.
    if front_matter:
        first_page = front_matter[0].page_number
        title_draft = SectionDraft(
            ordinal=0,
            original_heading=_FRONT_MATTER_HEADING,
            normalized_class=SectionClass.OTHER,
            level=0,
            page_start=first_page,
            page_end=max(first_page, front_matter[-1].page_number),
            parent_ordinal=None,
            unit_keys=[unit_key(u) for u in front_matter],
        )
        for key in title_draft.unit_keys:
            assignment[key] = 0
        if seen_heading:
            # Shift existing ordinals by 1 and rewire parents/assignment.
            for draft in drafts:
                draft.ordinal += 1
                if draft.parent_ordinal is not None:
                    draft.parent_ordinal += 1
            for key, ordinal in list(assignment.items()):
                assignment[key] = ordinal + 1
            drafts.insert(0, title_draft)
        else:
            drafts.append(title_draft)

    if not drafts:
        # No units at all (empty report): no sections — caller decides.
        return [], {}
    if not seen_heading:
        # Units exist but no headings: one flat OTHER section (honest flat
        # structure; the caller may flag STRUCTURE_001).
        only = drafts[0]
        only.normalized_class = SectionClass.OTHER
        if only.original_heading == _FRONT_MATTER_HEADING:
            only.original_heading = ""

    # page_end is tracked from assigned units; normalize the invariant.
    for draft in drafts:
        draft.page_end = max(draft.page_end, draft.page_start)
    return drafts, assignment


_FRONT_MATTER_HEADING = "(front matter)"

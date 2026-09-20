"""P04 structure/evidence-persistence contracts (spec doc 01 §8, doc 03 §1.4/§5).

SectionNode is the retrieval contract for the reconstructed hierarchy;
PersistResult is the machine-written outcome of extraction→evidence
persistence (idempotency and honest exclusions are first-class fields).
"""

from __future__ import annotations

from pydantic import Field

from paperintel.ids import IdPrefix
from paperintel.schemas.common import FrozenModel, LooseId
from paperintel.schemas.enums import SectionClass


class SectionNode(FrozenModel):
    """One node of the reconstructed section tree (doc 03 §5).

    A paper may contain multiple sections with the same normalized class;
    original headings are retained alongside the normalized class.
    """

    section_id: str
    paper_version_id: LooseId(IdPrefix.PAPER_VERSION)
    parent_section_id: str | None = None
    ordinal: int = Field(ge=0)
    #: Nesting depth: 0 = top-level section.
    level: int = Field(ge=0)
    original_heading: str
    normalized_class: SectionClass
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    evidence_count: int = Field(default=0, ge=0)
    children: list[SectionNode] = Field(default_factory=list)


class PersistResult(FrozenModel):
    """Outcome of persisting one extraction report into the evidence store.

    Idempotent by design: persisting the same report twice reuses existing
    rows (evidence_reused) and creates nothing new. Units that CANNOT be
    canonical evidence (frozen contract violations, e.g. OCR text without
    confidence, or invalid locators) are excluded BY REASON — never forced
    in, never silently dropped.
    """

    run_id: LooseId(IdPrefix.RUN)
    paper_version_id: LooseId(IdPrefix.PAPER_VERSION)
    analysis_run_created: bool
    sections_created: int = Field(ge=0)
    sections_reused: bool = False
    evidence_created: int = Field(ge=0)
    evidence_reused: int = Field(ge=0)
    #: reason → number of excluded units. Reasons are stable strings:
    #: ocr_confidence_missing, no_text_no_asset, invalid_page, unknown_asset.
    excluded: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def excluded_total(self) -> int:
        return sum(self.excluded.values())

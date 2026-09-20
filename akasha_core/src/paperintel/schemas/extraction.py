"""P03 extraction contracts (spec doc 01 §7, doc 03 §1.4/§4, doc 05 P03).

These are the in-flight data contracts between the ingest/extraction/ocr
modules. Evidence ROWS are persisted in P04 (evidence store); P03 produces
:class:`ExtractedUnit` lists inside an :class:`ExtractionReport` that P04
consumes. Provenance rules enforced here:

- every unit knows page number, bbox where applicable, source method,
  content hash, OCR confidence where applicable, and the extraction run;
- OCR confidence is recorded only when the OCR provider reported it —
  never invented (plain_text backends flag OCR_CONFIDENCE_UNAVAILABLE);
- execution quality (DataQualityState) is assessed separately from any
  pipeline/execution state (doc 02 §10).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from paperintel.ids import IdPrefix
from paperintel.schemas.common import BBox, FrozenModel, LooseId, utcnow
from paperintel.schemas.enums import (
    DataQualityState,
    EvidenceType,
    PageMode,
    SourceMethod,
)


class TextQualityMetrics(FrozenModel):
    """Per-page native text-layer quality signals (doc 01 §7 step 2)."""

    char_count: int = Field(ge=0)
    #: Printable non-space characters / all extracted characters (0.0 when empty).
    visible_char_ratio: float = Field(ge=0.0, le=1.0)
    #: U+FFFD replacement characters / all extracted characters.
    replacement_char_ratio: float = Field(ge=0.0, le=1.0)
    #: Characters per page area in pt² (text density).
    text_density: float = Field(ge=0.0)
    #: True when every line bbox is non-inverted and inside the page box.
    bbox_sane: bool
    #: Longest run of one repeated glyph / char_count (degenerate text layers
    #: often emit floods of a single character).
    repeated_glyph_ratio: float = Field(ge=0.0, le=1.0)
    #: Fraction of the page area covered by embedded images.
    image_area_ratio: float = Field(ge=0.0)


class PageClassification(FrozenModel):
    """Page mode decided BEFORE OCR (doc 01 §7: "Each page is classified
    before OCR"). OCR is used only for pages/regions that need it."""

    page_number: int = Field(ge=1)
    mode: PageMode
    metrics: TextQualityMetrics
    needs_ocr: bool
    #: Human-readable decision trail (deterministic, threshold-named).
    reasons: list[str] = Field(default_factory=list)


class ExtractedUnit(FrozenModel):
    """One extracted evidence candidate (persisted as Evidence in P04).

    Carries the full doc 01 §7 provenance set; ``text`` is None only for
    pure image units (FIGURE), whose content hash covers the image bytes.
    """

    unit_type: EvidenceType
    page_number: int = Field(ge=1)
    bbox: BBox | None = None
    text: str | None = None
    source_method: SourceMethod
    #: Mean provider-reported confidence for OCR-derived units; None for
    #: native text and for confidence-less OCR backends (never invented).
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    #: sha256 of embedded image bytes for figure/table-image units.
    asset_sha256: str | None = None
    content_sha256: str = Field(min_length=64, max_length=64)
    #: Reading-order position within the page (0-based).
    ordinal: int = Field(ge=0)
    #: Non-fatal annotations, e.g. "OCR_003: ...", "heuristic:equation",
    #: "OCR_CONFIDENCE_UNAVAILABLE".
    flags: list[str] = Field(default_factory=list)


class PageExtraction(FrozenModel):
    """Extraction outcome for one page."""

    page_number: int = Field(ge=1)
    classification: PageClassification
    units: list[ExtractedUnit] = Field(default_factory=list)
    #: Effective provenance for the page as a whole (doc 01 §7 step 4:
    #: mixed pages combine native text and OCR regions).
    page_source_method: SourceMethod
    ocr_used: bool = False
    #: Provider-reported mean confidence across OCR'd lines; None when no
    #: OCR ran or the backend reports none.
    mean_ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)


class QualityChecks(FrozenModel):
    """P03 gate quality checks (doc 05): zero-page detection, suspiciously
    empty text, OCR confidence warnings, page counts, location sanity."""

    zero_page_detected: bool
    suspiciously_empty_text: bool
    ocr_confidence_warnings: int = Field(ge=0)
    page_count_consistent: bool
    location_sanity: bool


class ExtractionReport(FrozenModel):
    """Machine-written extraction report for one run (cache-class artifact;
    consumed by P04 evidence persistence and by paperctl inspect)."""

    run_id: LooseId(IdPrefix.RUN)
    paper_version_id: LooseId(IdPrefix.PAPER_VERSION) | None = None
    started_at: datetime
    finished_at: datetime
    page_count: int = Field(ge=0)
    pages: list[PageExtraction] = Field(default_factory=list)
    unit_counts: dict[str, int] = Field(default_factory=dict)
    source_method_counts: dict[str, int] = Field(default_factory=dict)
    quality_state: DataQualityState
    quality_checks: QualityChecks
    warnings: list[str] = Field(default_factory=list)
    #: OCR pages that failed with a catalog error (code per page) — failures
    #: are recorded, never swallowed or silently skipped.
    ocr_failures: dict[int, str] = Field(default_factory=dict)

    @property
    def unit_total(self) -> int:
        return sum(self.unit_counts.values())


class ImportResult(FrozenModel):
    """Outcome of one paperctl import invocation."""

    paper_id: LooseId(IdPrefix.PAPER)
    paper_version_id: LooseId(IdPrefix.PAPER_VERSION)
    asset_id: LooseId(IdPrefix.ASSET)
    #: True when an identical content_sha256 version already existed — no
    #: duplicate rows were created (doc 03 §1.2: versions never overwrite).
    deduplicated: bool = False
    reused_asset: bool = False
    version_label: str
    report: ExtractionReport
    report_path: str | None = None
    #: P04 evidence-store outcome (None when persist_evidence=False).
    evidence: Any | None = None
    created_at: datetime = Field(default_factory=utcnow)
    #: Free-form operator-visible notes (e.g. title derived from filename).
    notes: list[str] = Field(default_factory=list)

    def cli_summary(self) -> dict[str, Any]:
        """Compact projection for CLI output (full report lives on disk)."""
        return {
            "paper_id": self.paper_id,
            "paper_version_id": self.paper_version_id,
            "asset_id": self.asset_id,
            "deduplicated": self.deduplicated,
            "reused_asset": self.reused_asset,
            "version_label": self.version_label,
            "page_count": self.report.page_count,
            "quality_state": self.report.quality_state.value,
            "unit_counts": self.report.unit_counts,
            "source_method_counts": self.report.source_method_counts,
            "quality_checks": self.report.quality_checks.model_dump(),
            "evidence": (
                {
                    "evidence_created": self.evidence.evidence_created,
                    "evidence_reused": self.evidence.evidence_reused,
                    "sections_created": self.evidence.sections_created,
                    "excluded": self.evidence.excluded,
                    "warnings": self.evidence.warnings,
                }
                if self.evidence is not None
                else None
            ),
            "warnings": self.report.warnings,
            "ocr_failures": {str(k): v for k, v in self.report.ocr_failures.items()},
            "report_path": self.report_path,
            "notes": self.notes,
        }

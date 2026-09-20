"""Evidence contract (spec doc 03 §1.4, §4).

Evidence is immutable: no UPDATE of canonical evidence content is allowed.
Correction creates a NEW evidence object and records supersession linkage.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from paperintel.schemas.common import (
    AssetId,
    BBox,
    EvidenceId,
    FrozenModel,
    PaperVersionId,
    RunId,
    Sha256Hex,
)
from paperintel.schemas.enums import DataQualityState, EvidenceType, SourceMethod


class Evidence(FrozenModel):
    """One immutable unit of extracted, locatable paper content."""

    evidence_id: EvidenceId
    paper_version_id: PaperVersionId
    evidence_type: EvidenceType
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    bbox: BBox | None = None
    section_id: str | None = None
    text: str | None = None
    asset_id: AssetId | None = None
    source_method: SourceMethod
    ocr_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    quality_state: DataQualityState
    content_sha256: Sha256Hex
    extraction_run_id: RunId
    created_at: datetime

    #: Set only on evidence that corrects an earlier evidence object; the
    #: superseded object itself is never modified (spec doc 03 §1.4).
    supersedes_evidence_id: EvidenceId | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Evidence:
        if self.page_end < self.page_start:
            raise ValueError(f"page_end ({self.page_end}) < page_start ({self.page_start})")
        if self.text is None and self.asset_id is None:
            raise ValueError("evidence must carry text and/or an asset reference")
        if self.source_method is SourceMethod.OCR and self.ocr_confidence is None:
            raise ValueError("OCR-derived evidence must record ocr_confidence")
        return self


__all__ = ["Evidence"]

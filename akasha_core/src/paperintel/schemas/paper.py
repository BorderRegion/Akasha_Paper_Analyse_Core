"""Paper identity, versions, assets, and section structure contracts
(spec doc 03 §1.1-1.3, §5)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from paperintel.schemas.common import (
    AssetId,
    FrozenModel,
    PaperId,
    PaperVersionId,
    Sha256Hex,
)
from paperintel.schemas.enums import (
    AssetKind,
    RetentionClass,
    SectionClass,
)


class Paper(FrozenModel):
    """The intellectual work identity (spec doc 03 §1.1)."""

    paper_id: PaperId
    canonical_title: str = Field(min_length=1)
    normalized_title: str = Field(min_length=1)
    doi: str | None = None
    primary_language: str | None = None
    paper_type: str | None = None
    created_at: datetime
    updated_at: datetime


class PaperVersion(FrozenModel):
    """A concrete version of a paper (spec doc 03 §1.2).

    Different arXiv/conference/camera-ready versions MUST NOT overwrite one
    another; each is a separate PaperVersion row bound to its own asset.
    """

    paper_version_id: PaperVersionId
    paper_id: PaperId
    version_label: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    source_locator: str | None = None
    asset_id: AssetId
    page_count: int | None = Field(default=None, ge=0)
    publication_date: datetime | None = None
    is_preprint: bool = False
    is_camera_ready: bool = False
    is_supplementary: bool = False
    content_sha256: Sha256Hex
    created_at: datetime


class Asset(FrozenModel):
    """A content-addressed binary object (spec doc 03 §1.3)."""

    asset_id: AssetId
    kind: AssetKind
    sha256: Sha256Hex
    storage_key: str = Field(min_length=1)
    mime_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    created_at: datetime
    retention_class: RetentionClass


class Section(FrozenModel):
    """A node of the reconstructed section hierarchy (spec doc 03 §5).

    A paper may contain multiple sections with the same normalized class.
    Original headings are retained alongside normalized classes (doc 01 §8).
    """

    section_id: str = Field(min_length=1)
    paper_version_id: PaperVersionId
    parent_section_id: str | None = None
    ordinal: int = Field(ge=0)
    original_heading: str = ""
    normalized_class: SectionClass
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    evidence_ids: list[str] = Field(default_factory=list)


__all__ = ["Asset", "Paper", "PaperVersion", "Section"]

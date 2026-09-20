"""schemas.ui.models — the workbench read models (frontend spec docs/06).

Field names match `contracts/view-models.ts` one-to-one; `docs/10` requires
F00/F02 contract tests to prove that correspondence.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from paperintel.schemas.common import FrozenModel
from paperintel.schemas.enums import ClaimType, ResourceTier, SupportState


class ReadStateValue(StrEnum):
    """Workbench reading state (frontend spec docs/02).

    UI-owned vocabulary: it is deliberately NOT merged into the frozen core
    enums, because "I have read it" is a personal fact, not a scientific one.
    """

    UNREAD = "UNREAD"
    READING = "READING"
    READ = "READ"
    LATER = "LATER"


MissingReason = Literal[
    "NOT_REPORTED",
    "NOT_EXTRACTED",
    "NOT_MEASURED",
    "NOT_APPLICABLE",
    "UNAVAILABLE",
    "UNKNOWN",
]


class SourceRef(FrozenModel):
    paper_id: str
    paper_version_id: str
    claim_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class FactValue(FrozenModel):
    """A value with its explicit missing reason — null is never 0."""

    value: Any | None = None
    missing_reason: MissingReason | None = None
    source_refs: list[SourceRef] = Field(default_factory=list)


class ClaimPreview(FrozenModel):
    claim_id: str
    statement: str
    claim_type: ClaimType
    support_state: SupportState
    paper_version_id: str
    evidence_count: int = Field(ge=0)
    is_superseded: bool = False


class TagView(FrozenModel):
    id: str
    label: str
    namespace: str
    is_candidate: bool = False


class ReadingAnchor(FrozenModel):
    paper_version_id: str
    page_number: int | None = None
    section_id: str | None = None
    scroll_offset: float | None = None


class PersonalState(FrozenModel):
    """The local user's private state for one paper.

    ``revision`` 0 means "nothing stored yet"; the first accepted write creates
    revision 1. Reporting a fabricated 1 for an empty state would let two
    clients believe they hold the same version and silently overwrite each other
    (the concurrency guard needs a truthful baseline).
    """

    saved: bool
    read_state: ReadStateValue
    revision: int = Field(ge=0)
    reading_anchor: ReadingAnchor | None = None


class AuditCounts(FrozenModel):
    total: int = Field(ge=0)
    by_state: dict[str, int] = Field(default_factory=dict)


class PaperListItem(FrozenModel):
    paper_id: str
    paper_version_id: str
    title: str
    authors: FactValue
    year: FactValue
    venue: FactValue
    takeaway: ClaimPreview | None = None
    tags: list[TagView] = Field(default_factory=list)
    tier: ResourceTier | None = None
    personal: PersonalState
    audit: AuditCounts


class LibraryFilters(FrozenModel):
    collection_ids: list[str] | None = None
    tag_ids: list[str] | None = None
    read_states: list[ReadStateValue] | None = None
    years: list[int] | None = None
    venue_ids: list[str] | None = None
    author_ids: list[str] | None = None
    tiers: list[ResourceTier] | None = None
    audit_states: list[SupportState] | None = None


class LibraryQuery(FrozenModel):
    query: str = ""
    kind: Literal["PAPERS", "CLAIMS", "TECHNIQUES"] = "PAPERS"
    filters: LibraryFilters = Field(default_factory=LibraryFilters)
    sort: Literal["RELEVANCE", "RECENT", "TITLE"] = "RELEVANCE"
    cursor: str | None = None
    #: Page size. The hard cap is 100; when omitted the deployment's
    #: ``ui.library_page_size`` applies (the same value capabilities advertises).
    limit: int | None = Field(default=None, ge=1, le=100)


class LibraryResponse(FrozenModel):
    """Discriminated result set: a claim must never masquerade as a paper."""

    kind: Literal["PAPERS", "CLAIMS", "TECHNIQUES"]
    papers: list[PaperListItem] = Field(default_factory=list)
    claims: list[ClaimPreview] = Field(default_factory=list)


class ModuleView(FrozenModel):
    id: str
    label: str
    availability: Literal["AVAILABLE", "UNAVAILABLE", "DEGRADED"]
    reason: str | None = None
    claims: list[ClaimPreview] = Field(default_factory=list)


class VersionSummary(FrozenModel):
    paper_version_id: str
    version_label: str
    document_sha256: str
    document_available: bool


class Workspace(FrozenModel):
    paper: PaperListItem
    document_sha256: str
    selected_version_id: str
    versions: list[VersionSummary] = Field(default_factory=list)
    modules: list[ModuleView] = Field(default_factory=list)
    analysis_revision: str
    personal: PersonalState
    audit: AuditCounts


class NoteView(FrozenModel):
    note_id: str
    paper_id: str
    paper_version_id: str
    claim_id: str | None = None
    evidence_id: str | None = None
    body: str
    revision: int = Field(ge=1)
    updated_at: datetime


class VerificationSummary(FrozenModel):
    """One verification record, summarised for the review surface.

    The verifier, its verdict and its reason are shown; the full model
    call/prompt detail stays behind an explicit expansion (docs/07 §5).
    """

    verification_id: str
    verifier_type: str
    status: str
    verdict: str
    reason_summary: str = ""
    run_id: str | None = None


class ReviewItem(FrozenModel):
    """One claim worth a human look (docs/03 §S06).

    ``group`` is derived from REAL verification outcomes — never from a
    fabricated risk score — and ``NOT_VERIFIED`` is deliberately distinct from
    "verified and clean": "所有项0" and "审计服务未返回" are different states.
    """

    claim: ClaimPreview
    paper_id: str
    paper_version_id: str
    paper_title: str
    group: Literal[
        "NUMERIC_OR_OCR",
        "EVIDENCE_UNSUPPORTED",
        "CONTRADICTION",
        "SCOPE",
        "NOVELTY_UNVERIFIED",
        "SUPPORT_STATE",
        "NOT_VERIFIED",
    ] = "SUPPORT_STATE"
    reasons: list[str] = Field(default_factory=list)
    impact: Literal["HIGH", "NORMAL", "LOW"] = "NORMAL"
    source_refs: list[SourceRef] = Field(default_factory=list)
    #: The claim's own evidence (verbatim quotes available through /v1/claims).
    original_evidence: list[SourceRef] = Field(default_factory=list)
    #: Counter-evidence: the contradicting claim's evidence or a critique's
    #: counter-evidence links, so both sides are reachable side by side.
    counter_evidence: list[SourceRef] = Field(default_factory=list)
    verifications: list[VerificationSummary] = Field(default_factory=list)
    #: Optimistic-concurrency token for review decisions (server-computed).
    claim_revision: str
    personal_decision: Literal["SEEN", "NEEDS_REVIEW", "RESERVATION"] | None = None


class ReviewDecisionView(FrozenModel):
    decision_id: str
    claim_id: str
    paper_version_id: str
    decision: Literal["SEEN", "NEEDS_REVIEW", "RESERVATION"]
    note: str | None = None
    created_at: datetime


class EvidenceLocator(FrozenModel):
    evidence_id: str
    paper_version_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    page_label: str | None = None
    coordinate_space: Literal["DISPLAY_NORMALIZED_V1"] = "DISPLAY_NORMALIZED_V1"
    rect_norm: list[float] | None = None
    precision: Literal["REGION", "PAGE", "TEXT_ONLY"] = "PAGE"
    source_method: str
    ocr_confidence: float | None = None
    transform_revision: str
    extraction_run_id: str | None = None
    reason: str | None = None


class PageEvidence(FrozenModel):
    paper_version_id: str
    document_sha256: str
    page_number: int = Field(ge=1)
    page_display_width: int = Field(ge=1)
    page_display_height: int = Field(ge=1)
    reference_rotation: Literal[0, 90, 180, 270] = 0
    evidence: list[EvidenceLocator] = Field(default_factory=list)


class ImportItemView(FrozenModel):
    item_id: str
    filename: str
    size_bytes: int = Field(ge=0)
    state: Literal[
        "PENDING", "UPLOADING", "RECEIVED", "QUEUED", "IMPORTED", "DUPLICATE", "FAILED", "CANCELLED"
    ]
    paper_id: str | None = None
    paper_version_id: str | None = None
    job_id: str | None = None
    error_code: str | None = None


class ImportBatchView(FrozenModel):
    batch_id: str
    items: list[ImportItemView] = Field(default_factory=list)


class OperationResultItem(FrozenModel):
    target_id: str
    status: str
    error_code: str | None = None


class OperationView(FrozenModel):
    operation_id: str
    kind: str
    state: Literal["ACCEPTED", "RUNNING", "COMPLETED", "PARTIAL", "FAILED", "CANCELLED"]
    scope: dict[str, Any] = Field(default_factory=dict)
    job_ids: list[str] = Field(default_factory=list)
    results: list[OperationResultItem] = Field(default_factory=list)


class ObservedMetric(FrozenModel):
    value: float | None = None
    unit: str = ""
    state: Literal["FRESH", "STALE", "UNKNOWN"] = "UNKNOWN"
    observed_at: datetime | None = None
    sample_count: int = Field(default=0, ge=0)
    window_seconds: int | None = None
    reason: str | None = None


class WorkerObservation(FrozenModel):
    identity: str
    last_seen: datetime | None = None
    state: str = "UNKNOWN"


class ModuleObservation(FrozenModel):
    module_id: str
    health: str
    observed_at: datetime | None = None
    source: str = ""
    reason: str | None = None


class ProviderObservation(FrozenModel):
    id: str
    configured_limit: int | None = None
    observed_active: ObservedMetric
    latency_p50: ObservedMetric
    latency_p90: ObservedMetric
    error_rate: ObservedMetric


class DiskObservation(FrozenModel):
    """One mount point (docs/03 §S10).

    ``application_bytes`` is what PaperIntel occupies on that mount; ``free_bytes``
    is the mount's free space. They are different numbers and are never stacked
    into one pie chart. ``shares_mount_with`` names the other stores that live on
    the same device, so overlapping statistics are visible instead of implied.
    ``measurable=False`` means the directory could not be read — which is not 0 B.
    """

    mount_id: str
    free_bytes: int | None = None
    total_bytes: int | None = None
    application_bytes: int | None = None
    observed_at: datetime | None = None
    level: str = "UNKNOWN"
    measurable: bool = True
    device: str | None = None
    shares_mount_with: list[str] = Field(default_factory=list)
    reason: str | None = None


class OperationSnapshot(FrozenModel):
    queue: dict[str, int] = Field(default_factory=dict)
    workers: list[WorkerObservation] = Field(default_factory=list)
    modules: list[ModuleObservation] = Field(default_factory=list)
    providers: list[ProviderObservation] = Field(default_factory=list)
    disk: list[DiskObservation] = Field(default_factory=list)
    eta_available: bool = False
    observed_at: datetime | None = None
    unknown_reasons: list[str] = Field(default_factory=list)


class PreferencesView(FrozenModel):
    theme: Literal["LIGHT", "DARK", "SYSTEM"] = "SYSTEM"
    density: Literal["COMFORTABLE", "COMPACT"] = "COMFORTABLE"
    reduce_motion: bool = False
    single_key_shortcuts: bool = True
    reader_font_px: int = Field(default=17, ge=12, le=28)
    focus_default: bool = False
    revision: int = Field(default=1, ge=1)


class SavedSearchView(FrozenModel):
    saved_search_id: str
    name: str
    query: LibraryQuery
    revision: int = Field(ge=1)


__all__ = [
    "AuditCounts",
    "ClaimPreview",
    "DiskObservation",
    "EvidenceLocator",
    "FactValue",
    "ImportBatchView",
    "ImportItemView",
    "LibraryFilters",
    "LibraryQuery",
    "LibraryResponse",
    "ModuleObservation",
    "ModuleView",
    "NoteView",
    "ObservedMetric",
    "OperationResultItem",
    "OperationSnapshot",
    "OperationView",
    "PageEvidence",
    "PaperListItem",
    "PersonalState",
    "PreferencesView",
    "ProviderObservation",
    "ReadingAnchor",
    "ReadStateValue",
    "ReviewDecisionView",
    "ReviewItem",
    "VerificationSummary",
    "SavedSearchView",
    "SourceRef",
    "TagView",
    "VersionSummary",
    "WorkerObservation",
    "Workspace",
]

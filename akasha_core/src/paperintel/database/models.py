"""Canonical ORM models (spec doc 03 data contracts, FROZEN semantics).

Conventions:
- public IDs are opaque prefixed strings (spec doc 02 §2), stored as VARCHAR
  primary keys; sequential IDs are never exposed publicly;
- Python enums are stored by NAME in VARCHAR columns (native_enum=False) to
  keep future enum additions migration-compatible;
- JSONB is used for structured payloads whose shape is versioned elsewhere
  (manifests, components, provenance);
- ``evidence`` content is immutable: a database trigger (installed by the base
  migration) rejects content updates and deletes; only ``section_id`` and
  ``quality_state`` may be re-linked/re-assessed, and corrections are new
  evidence rows with supersession linkage.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum

from paperintel.database.base import Base
from paperintel.schemas.enums import (
    AssetKind,
    ClaimType,
    DataQualityState,
    EntityType,
    EvidenceRole,
    EvidenceType,
    PipelineStage,
    QualityDimension,
    ResourceTier,
    RetentionClass,
    SchemaStatus,
    SectionClass,
    SourceMethod,
    SupportState,
    TaskState,
    TransportStatus,
    VerifierType,
    VerifierVerdict,
)

# Column type aliases -------------------------------------------------------

PublicIdCol = String(32)  # prefix (<= 5) + 26-char ULID payload
ShortStr = String(255)
#: pgvector column width for the configured embedding model (P09); must
#: match migrations/versions/a7c4e91b2f38_p09_search_projection.py.
EMBEDDING_DIMENSIONS = 64
EnumStr = String(48)  # longest frozen enum name: SUCCEEDED_WITH_WARNINGS (25)
TimestampUTC = DateTime(timezone=True)


def _enum(enum_cls: type) -> SAEnum:
    """Store enum members by name in a VARCHAR column."""
    return SAEnum(enum_cls, native_enum=False, length=48, validate_strings=True)


# ---------------------------------------------------------------------------
# Identity & content
# ---------------------------------------------------------------------------


class PaperRow(Base):
    __tablename__ = "papers"

    paper_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    canonical_title: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_title: Mapped[str] = mapped_column(Text, nullable=False)
    doi: Mapped[str | None] = mapped_column(ShortStr, unique=True)
    primary_language: Mapped[str | None] = mapped_column(String(16))
    paper_type: Mapped[str | None] = mapped_column(ShortStr)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()"), onupdate=sa_text("now()")
    )

    versions: Mapped[list[PaperVersionRow]] = relationship(back_populates="paper")
    __table_args__ = (Index("ix_papers_normalized_title", "normalized_title"),)


class AssetRow(Base):
    __tablename__ = "assets"

    asset_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    kind: Mapped[AssetKind] = mapped_column(_enum(AssetKind), nullable=False)
    #: Content-addressed dedup: one canonical asset row per sha256.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    storage_key: Mapped[str] = mapped_column(ShortStr, nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    retention_class: Mapped[RetentionClass] = mapped_column(
        _enum(RetentionClass), nullable=False, server_default=sa_text("'KEEP'")
    )


class PaperVersionRow(Base):
    __tablename__ = "paper_versions"

    paper_version_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), nullable=False, index=True
    )
    version_label: Mapped[str] = mapped_column(ShortStr, nullable=False)
    source_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    source_locator: Mapped[str | None] = mapped_column(Text)
    asset_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("assets.asset_id"), nullable=False
    )
    page_count: Mapped[int | None] = mapped_column(Integer)
    publication_date: Mapped[datetime | None] = mapped_column(TimestampUTC)
    is_preprint: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_camera_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_supplementary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    paper: Mapped[PaperRow] = relationship(back_populates="versions")

    __table_args__ = (
        # Different versions must not overwrite one another; the same binary
        # may not be registered twice for one paper.
        UniqueConstraint("paper_id", "version_label", name="uq_version_label"),
        UniqueConstraint("paper_id", "content_sha256", name="uq_version_content"),
    )


class EvidenceRow(Base):
    """Immutable evidence unit (spec doc 03 §1.4).

    The base migration installs trigger ``evidence_immutable_guard`` which
    raises on UPDATE of any content column and on DELETE. Only ``section_id``
    (structure linking) and ``quality_state`` (re-assessment) may change.
    """

    __tablename__ = "evidence"

    evidence_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    paper_version_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("paper_versions.paper_version_id"), nullable=False, index=True
    )
    evidence_type: Mapped[EvidenceType] = mapped_column(_enum(EvidenceType), nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)
    bbox: Mapped[dict | None] = mapped_column(JSONB)
    section_id: Mapped[str | None] = mapped_column(ShortStr, index=True)
    text: Mapped[str | None] = mapped_column(Text)
    asset_id: Mapped[str | None] = mapped_column(PublicIdCol, ForeignKey("assets.asset_id"))
    source_method: Mapped[SourceMethod] = mapped_column(_enum(SourceMethod), nullable=False)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    quality_state: Mapped[DataQualityState] = mapped_column(
        _enum(DataQualityState), nullable=False, server_default=sa_text("'UNKNOWN'")
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_run_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    supersedes_evidence_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("evidence.evidence_id")
    )


class SectionRow(Base):
    __tablename__ = "sections"

    #: Sections have no frozen public prefix; ``sec_``+ULID is the documented
    #: internal convention (the structure contract only requires a stable
    #: opaque string).
    section_id: Mapped[str] = mapped_column(ShortStr, primary_key=True)
    paper_version_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("paper_versions.paper_version_id"), nullable=False, index=True
    )
    parent_section_id: Mapped[str | None] = mapped_column(
        ShortStr, ForeignKey("sections.section_id")
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    original_heading: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("''")
    )
    normalized_class: Mapped[SectionClass] = mapped_column(_enum(SectionClass), nullable=False)
    page_start: Mapped[int] = mapped_column(Integer, nullable=False)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_sections_pver_ordinal", "paper_version_id", "ordinal"),)


# ---------------------------------------------------------------------------
# Analysis runs, claims, verification
# ---------------------------------------------------------------------------


class AnalysisRunRow(Base):
    __tablename__ = "analysis_runs"

    run_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    paper_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), index=True
    )
    paper_version_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("paper_versions.paper_version_id"), index=True
    )
    collection_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("collections.collection_id"), index=True
    )
    agent_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    spec_version: Mapped[str | None] = mapped_column(ShortStr, default="1.0.0")
    prompt_version: Mapped[str | None] = mapped_column(ShortStr)
    pipeline_version: Mapped[str] = mapped_column(ShortStr, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(ShortStr, nullable=False)
    provider_id: Mapped[str] = mapped_column(ShortStr, nullable=False)
    status: Mapped[TaskState] = mapped_column(_enum(TaskState), nullable=False)
    started_at: Mapped[datetime] = mapped_column(TimestampUTC, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampUTC)
    trace_id: Mapped[str | None] = mapped_column(PublicIdCol, index=True)


class PromptVersionRow(Base):
    __tablename__ = "prompt_versions"

    prompt_version_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    name: Mapped[str] = mapped_column(ShortStr, nullable=False)
    version: Mapped[str] = mapped_column(ShortStr, nullable=False)
    template_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    template_body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_name_version"),)


class ClaimRow(Base):
    __tablename__ = "claims"

    claim_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), nullable=False, index=True
    )
    paper_version_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("paper_versions.paper_version_id"), nullable=False, index=True
    )
    claim_type: Mapped[ClaimType] = mapped_column(_enum(ClaimType), nullable=False)
    spec_version: Mapped[str | None] = mapped_column(ShortStr, default="1.0.0")
    prompt_version: Mapped[str | None] = mapped_column(ShortStr)
    category: Mapped[str] = mapped_column(ShortStr, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_statement: Mapped[str | None] = mapped_column(Text)
    support_state: Mapped[SupportState] = mapped_column(
        _enum(SupportState), nullable=False, server_default=sa_text("'UNVERIFIED'")
    )
    #: Model self-report; diagnostics only, never canonical confidence.
    analysis_confidence: Mapped[float | None] = mapped_column(Float)
    system_confidence: Mapped[float | None] = mapped_column(Float)
    system_confidence_components: Mapped[dict | None] = mapped_column(JSONB)
    created_by_run_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id"), nullable=False
    )
    pipeline_version: Mapped[str] = mapped_column(ShortStr, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    superseded_by_claim_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("claims.claim_id")
    )

    evidence_links: Mapped[list[ClaimEvidenceRow]] = relationship(back_populates="claim")
    verifications: Mapped[list[VerificationRow]] = relationship(back_populates="claim")
    external_provenances: Mapped[list[ExternalProvenanceRow]] = relationship(back_populates="claim")

    __table_args__ = (
        Index("ix_claims_type_state", "claim_type", "support_state"),
        Index("ix_claims_category", "category"),
    )


class ClaimEvidenceRow(Base):
    __tablename__ = "claim_evidence"

    claim_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("claims.claim_id"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("evidence.evidence_id"), primary_key=True
    )
    role: Mapped[EvidenceRole] = mapped_column(_enum(EvidenceRole), nullable=False)

    claim: Mapped[ClaimRow] = relationship(back_populates="evidence_links")


class ExternalProvenanceRow(Base):
    """Provenance for EXTERNAL claims / externally verified facts
    (spec doc 03 §1.6)."""

    __tablename__ = "external_provenances"

    provenance_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("claims.claim_id"), nullable=False, index=True
    )
    source_provider: Mapped[str] = mapped_column(ShortStr, nullable=False)
    source_identifier: Mapped[str] = mapped_column(ShortStr, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime] = mapped_column(TimestampUTC, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    citation_text: Mapped[str | None] = mapped_column(Text)

    claim: Mapped[ClaimRow] = relationship(back_populates="external_provenances")


class VerificationRow(Base):
    __tablename__ = "verifications"

    verification_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    claim_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("claims.claim_id"), nullable=False, index=True
    )
    verifier_type: Mapped[VerifierType] = mapped_column(_enum(VerifierType), nullable=False)
    status: Mapped[str] = mapped_column(ShortStr, nullable=False)
    verdict: Mapped[VerifierVerdict] = mapped_column(_enum(VerifierVerdict), nullable=False)
    reason_summary: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    details_json: Mapped[dict | None] = mapped_column(JSONB)
    created_by_run_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    claim: Mapped[ClaimRow] = relationship(back_populates="verifications")

    __table_args__ = (Index("ix_verifications_type_verdict", "verifier_type", "verdict"),)


class ModelCallRow(Base):
    __tablename__ = "model_calls"

    model_call_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    provider_id: Mapped[str] = mapped_column(ShortStr, nullable=False)
    model_id: Mapped[str] = mapped_column(ShortStr, nullable=False)
    task_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("tasks.task_id"), index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id"), index=True
    )
    trace_id: Mapped[str | None] = mapped_column(PublicIdCol, index=True)
    prompt_version_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("prompt_versions.prompt_version_id"), nullable=False
    )
    #: References immutable evidence IDs + prompt version, never duplicates
    #: full evidence text (spec doc 03 §1.8).
    request_manifest_json: Mapped[dict] = mapped_column(JSONB, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_object_hash: Mapped[str | None] = mapped_column(String(64))
    response_hash: Mapped[str | None] = mapped_column(String(64))
    transport_status: Mapped[TransportStatus] = mapped_column(
        _enum(TransportStatus), nullable=False
    )
    schema_status: Mapped[SchemaStatus] = mapped_column(_enum(SchemaStatus), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    retry_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


# ---------------------------------------------------------------------------
# Jobs & tasks
# ---------------------------------------------------------------------------


class JobRow(Base):
    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), nullable=False, index=True
    )
    paper_version_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("paper_versions.paper_version_id"), nullable=False
    )
    requested_tier: Mapped[ResourceTier] = mapped_column(_enum(ResourceTier), nullable=False)
    effective_tier: Mapped[ResourceTier] = mapped_column(_enum(ResourceTier), nullable=False)
    state: Mapped[TaskState] = mapped_column(_enum(TaskState), nullable=False)
    current_stage: Mapped[PipelineStage] = mapped_column(
        _enum(PipelineStage), nullable=False, server_default=sa_text("'IMPORTED'")
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TimestampUTC)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampUTC)
    trace_id: Mapped[str | None] = mapped_column(PublicIdCol, index=True)

    tasks: Mapped[list[TaskRow]] = relationship(back_populates="job")

    __table_args__ = (Index("ix_jobs_state_priority", "state", "priority"),)


class TaskRow(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    job_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("jobs.job_id"), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    module_id: Mapped[str] = mapped_column(ShortStr, nullable=False)
    state: Mapped[TaskState] = mapped_column(_enum(TaskState), nullable=False)
    #: Execution state and data quality stay separate columns (doc 02 §10).
    quality_state: Mapped[DataQualityState] = mapped_column(
        _enum(DataQualityState), nullable=False, server_default=sa_text("'UNKNOWN'")
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    #: Idempotency guard: one canonical output per stable key (doc 03 §6).
    idempotency_key: Mapped[str] = mapped_column(ShortStr, nullable=False, unique=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("3"))
    next_retry_at: Mapped[datetime | None] = mapped_column(TimestampUTC, index=True)
    input_manifest: Mapped[dict] = mapped_column(JSONB, nullable=False)
    output_manifest: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(16))
    error_details: Mapped[dict | None] = mapped_column(JSONB)
    trace_id: Mapped[str | None] = mapped_column(PublicIdCol, index=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    started_at: Mapped[datetime | None] = mapped_column(TimestampUTC)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampUTC)

    job: Mapped[JobRow] = relationship(back_populates="tasks")

    __table_args__ = (
        Index("ix_tasks_state_priority", "state", "priority"),
        Index("ix_tasks_type_state", "task_type", "state"),
    )


class TraceEventRow(Base):
    __tablename__ = "trace_events"

    #: Internal surrogate key; trace records are not publicly addressable by it.
    event_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(PublicIdCol, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TimestampUTC, nullable=False)
    kind: Mapped[str] = mapped_column(ShortStr, nullable=False)
    module_id: Mapped[str | None] = mapped_column(ShortStr)
    job_id: Mapped[str | None] = mapped_column(PublicIdCol)
    task_id: Mapped[str | None] = mapped_column(PublicIdCol)
    run_id: Mapped[str | None] = mapped_column(PublicIdCol)
    model_call_id: Mapped[str | None] = mapped_column(PublicIdCol)
    level: Mapped[str] = mapped_column(String(16), nullable=False, server_default=sa_text("'INFO'"))
    message: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    data: Mapped[dict | None] = mapped_column(JSONB)

    __table_args__ = (Index("ix_trace_events_trace_time", "trace_id", "occurred_at"),)


# ---------------------------------------------------------------------------
# Knowledge layer
# ---------------------------------------------------------------------------


class SearchDocumentRow(Base):
    """Derived search projection (spec doc 01 §17; P09).

    Searchable forms of immutable records live here so canonical tables
    (evidence, claims) are never UPDATEd for indexing purposes. Rows are
    rebuildable at any time; the FTS vector and the pgvector embedding are
    populated through SQL (to_tsvector / vector casts).
    """

    __tablename__ = "search_documents"

    document_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_type: Mapped[str] = mapped_column(String(16), nullable=False)
    paper_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id", ondelete="CASCADE")
    )
    paper_version_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("paper_versions.paper_version_id", ondelete="CASCADE")
    )
    section_id: Mapped[str | None] = mapped_column(String(32))
    evidence_type: Mapped[str | None] = mapped_column(String(32))
    claim_type: Mapped[str | None] = mapped_column(String(16))
    support_state: Mapped[str | None] = mapped_column(String(32))
    page_start: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    #: PostgreSQL FTS vector (populated in SQL by paperintel.search.index;
    #: never written through the ORM).
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)
    embedding_model_id: Mapped[str | None] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: pgvector embedding (dimension matches the P09 migration).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (
        Index("ix_search_documents_type", "document_type"),
        Index("ix_search_documents_paper", "paper_id"),
        Index("ix_search_documents_version", "paper_version_id"),
        Index("ix_search_documents_claim_state", "claim_type", "support_state"),
    )


class EntityRow(Base):
    __tablename__ = "entities"

    entity_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    entity_type: Mapped[EntityType] = mapped_column(_enum(EntityType), nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSONB)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (
        UniqueConstraint("entity_type", "normalized_name", name="uq_entity_type_name"),
    )


class RelationRow(Base):
    """Generic relation framework (spec doc 01 §16)."""

    __tablename__ = "relations"

    relation_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    source_entity_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("entities.entity_id"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    target_entity_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("entities.entity_id"), nullable=False
    )
    paper_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), index=True
    )
    evidence_ids: Mapped[list | None] = mapped_column(JSONB)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_by_run_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (
        Index("ix_relations_source_type", "source_entity_id", "relation_type"),
        Index("ix_relations_target_type", "target_entity_id", "relation_type"),
    )


class TagRow(Base):
    __tablename__ = "tags"

    tag_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    #: Canonical namespace family, e.g. "method/" (spec doc 01 §15).
    namespace: Mapped[str] = mapped_column(ShortStr, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (UniqueConstraint("namespace", "normalized_name", name="uq_tag_ns_name"),)


class TagAliasRow(Base):
    __tablename__ = "tag_aliases"

    alias_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tag_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("tags.tag_id"), nullable=False, index=True
    )
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_alias: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class PaperTagRow(Base):
    __tablename__ = "paper_tags"

    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(PublicIdCol, ForeignKey("tags.tag_id"), primary_key=True)
    created_by_run_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id")
    )
    #: Free-text model tags are candidates only until normalized (doc 01 §15).
    is_candidate: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class EntityTagRow(Base):
    __tablename__ = "entity_tags"

    entity_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("entities.entity_id"), primary_key=True
    )
    tag_id: Mapped[str] = mapped_column(PublicIdCol, ForeignKey("tags.tag_id"), primary_key=True)


class TechniqueRow(Base):
    """Experimental technique as a first-class entity (spec doc 03 §14)."""

    __tablename__ = "techniques"

    technique_entity_id: Mapped[str] = mapped_column(ShortStr, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    aliases: Mapped[list | None] = mapped_column(JSONB)
    problem_addressed: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("''")
    )
    procedure_summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("''")
    )
    conditions: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    reported_effect: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    cost_or_tradeoff: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("''")
    )
    transferability_tags: Mapped[list | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class TechniquePaperRow(Base):
    __tablename__ = "technique_papers"

    technique_entity_id: Mapped[str] = mapped_column(
        ShortStr, ForeignKey("techniques.technique_entity_id"), primary_key=True
    )
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), primary_key=True
    )


class QualityAssessmentRow(Base):
    """One quality-vector dimension record (spec doc 03 §15).

    Never unexplained numeric scores only: assessment text + evidence refs.
    """

    __tablename__ = "quality_assessments"

    quality_assessment_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), nullable=False, index=True
    )
    dimension: Mapped[QualityDimension] = mapped_column(_enum(QualityDimension), nullable=False)
    assessment: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    evidence_ids: Mapped[list | None] = mapped_column(JSONB)
    counter_evidence_ids: Mapped[list | None] = mapped_column(JSONB)
    system_confidence: Mapped[float | None] = mapped_column(Float)
    run_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (
        # One assessment per dimension per run; reruns create new rows.
        UniqueConstraint("paper_id", "dimension", "run_id", name="uq_quality_paper_dim_run"),
    )


class VenueRegistryRow(Base):
    """Venue facts with ranking system + year (spec doc 01 §13).

    LLMs never invent venue rank; rows come from the VenueRegistry with
    source + retrieval time. Multiple ranking systems coexist.
    """

    __tablename__ = "venue_registry"

    venue_registry_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    venue_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list | None] = mapped_column(JSONB)
    type: Mapped[str | None] = mapped_column(ShortStr)
    publisher: Mapped[str | None] = mapped_column(ShortStr)
    peer_reviewed: Mapped[bool | None] = mapped_column(Boolean)
    ranking_system: Mapped[str | None] = mapped_column(ShortStr)
    ranking_value: Mapped[str | None] = mapped_column(ShortStr)
    ranking_year: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str | None] = mapped_column(ShortStr)
    source_retrieved_at: Mapped[datetime | None] = mapped_column(TimestampUTC)

    __table_args__ = (
        UniqueConstraint(
            "venue_id",
            "ranking_system",
            "ranking_year",
            name="uq_venue_ranking",
        ),
    )


# ---------------------------------------------------------------------------
# Collections & triage
# ---------------------------------------------------------------------------


class CollectionRow(Base):
    __tablename__ = "collections"

    collection_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    name: Mapped[str] = mapped_column(ShortStr, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    research_questions: Mapped[list | None] = mapped_column(JSONB)
    preferred_tags: Mapped[list | None] = mapped_column(JSONB)
    relevance_notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class CollectionPaperRow(Base):
    __tablename__ = "collection_papers"

    collection_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("collections.collection_id"), primary_key=True
    )
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), primary_key=True
    )
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: Manual override always wins over automatic triage (doc 02 §11).
    priority_override_tier: Mapped[ResourceTier | None] = mapped_column(_enum(ResourceTier))
    relevance_note: Mapped[str | None] = mapped_column(Text)
    added_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class TriageResultRow(Base):
    """Resource allocation record (spec doc 03 §16) — not paper quality."""

    __tablename__ = "triage_results"

    triage_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id"), nullable=False, index=True
    )
    recommended_tier: Mapped[ResourceTier] = mapped_column(_enum(ResourceTier), nullable=False)
    effective_tier: Mapped[ResourceTier] = mapped_column(_enum(ResourceTier), nullable=False)
    manual_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signals: Mapped[dict | None] = mapped_column(JSONB)
    reason_codes: Mapped[list | None] = mapped_column(JSONB)
    created_by_run_id: Mapped[str | None] = mapped_column(
        PublicIdCol, ForeignKey("analysis_runs.run_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


# ---------------------------------------------------------------------------
# Workbench (UI) owned state — frontend spec docs/06 「新增持久数据」
# ---------------------------------------------------------------------------
# These tables never change core scientific records: personal reading state,
# notes, human review decisions, saved searches, import batches and operation
# requests are UI-owned; ui_evidence_locators is a REBUILDABLE projection over
# immutable evidence (it stores locators + hashes, never evidence text).


class UiPreferencesRow(Base):
    __tablename__ = "ui_preferences"

    owner_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    theme: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'SYSTEM'")
    )
    density: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'COMFORTABLE'")
    )
    reduce_motion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    single_key_shortcuts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("true")
    )
    reader_font_px: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("17")
    )
    focus_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiPersonalPaperStateRow(Base):
    __tablename__ = "ui_personal_paper_state"

    owner_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id", ondelete="CASCADE"), primary_key=True
    )
    saved: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    read_state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'UNREAD'")
    )
    #: {paper_version_id,page_number,section_id,scroll_offset} — reading
    #: position only; never coupled to analysis completion (docs/02).
    reading_anchor: Mapped[dict | None] = mapped_column(JSONB)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiNoteRow(Base):
    __tablename__ = "ui_notes"

    note_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    paper_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("papers.paper_id", ondelete="CASCADE"), nullable=False
    )
    paper_version_id: Mapped[str] = mapped_column(
        PublicIdCol,
        ForeignKey("paper_versions.paper_version_id", ondelete="CASCADE"),
        nullable=False,
    )
    claim_id: Mapped[str | None] = mapped_column(PublicIdCol)
    evidence_id: Mapped[str | None] = mapped_column(PublicIdCol)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )

    __table_args__ = (Index("ix_ui_notes_paper", "paper_id", "paper_version_id"),)


class UiReviewDecisionRow(Base):
    """A human decision about a claim. It NEVER writes support_state."""

    __tablename__ = "ui_review_decisions"

    decision_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    claim_id: Mapped[str] = mapped_column(
        PublicIdCol, ForeignKey("claims.claim_id", ondelete="CASCADE"), nullable=False, index=True
    )
    paper_version_id: Mapped[str] = mapped_column(PublicIdCol, nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str | None] = mapped_column(String(96), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiSavedSearchRow(Base):
    __tablename__ = "ui_saved_searches"

    saved_search_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(ShortStr, nullable=False)
    #: Full filter structure + schema_version; never a transient cursor.
    query: Mapped[dict] = mapped_column(JSONB, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'1.0.0'")
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiImportBatchRow(Base):
    __tablename__ = "ui_import_batches"

    batch_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    collection_ids: Mapped[list | None] = mapped_column(JSONB)
    requested_tier: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'T2_FULL'")
    )
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiImportItemRow(Base):
    __tablename__ = "ui_import_items"

    item_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        PublicIdCol,
        ForeignKey("ui_import_batches.batch_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'PENDING'")
    )
    paper_id: Mapped[str | None] = mapped_column(PublicIdCol)
    paper_version_id: Mapped[str | None] = mapped_column(PublicIdCol)
    job_id: Mapped[str | None] = mapped_column(PublicIdCol)
    error_code: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(96), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiOperationRequestRow(Base):
    __tablename__ = "ui_operation_requests"

    operation_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'ACCEPTED'")
    )
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    results: Mapped[list | None] = mapped_column(JSONB)
    job_ids: Mapped[list | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str | None] = mapped_column(String(96), unique=True)
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


class UiEvidenceLocatorRow(Base):
    """Rebuildable locator projection over immutable evidence.

    Stores only references (evidence id, document hash, extraction run) and
    derived geometry; evidence text is never copied and evidence rows are never
    updated by the workbench.
    """

    __tablename__ = "ui_evidence_locators"

    evidence_id: Mapped[str] = mapped_column(PublicIdCol, primary_key=True)
    paper_version_id: Mapped[str] = mapped_column(PublicIdCol, nullable=False, index=True)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_number: Mapped[int] = mapped_column(Integer, nullable=False)
    page_label: Mapped[str | None] = mapped_column(String(64))
    coordinate_space: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=sa_text("'DISPLAY_NORMALIZED_V1'")
    )
    rect_norm: Mapped[list | None] = mapped_column(JSONB)
    precision: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=sa_text("'PAGE'")
    )
    source_method: Mapped[str] = mapped_column(String(32), nullable=False)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    transform_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    extraction_run_id: Mapped[str | None] = mapped_column(PublicIdCol)
    reason: Mapped[str | None] = mapped_column(Text)
    built_at: Mapped[datetime] = mapped_column(
        TimestampUTC, nullable=False, server_default=sa_text("now()")
    )


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------
# Agent-run status values are validated by the schema firewall
# (paperintel.schemas.enums.AgentStatus); analysis_runs.status uses TaskState
# for execution semantics, keeping execution and analytical outcomes separate.

__all__ = [n for n in dir() if n.endswith("Row")]

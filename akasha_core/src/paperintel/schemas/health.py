"""Health, provider-status, and sanity-check contracts
(spec doc 03 §10; spec doc 04 §2.1, §6; spec doc 06 §5, §11)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from paperintel.schemas.common import FrozenModel, StrictModel, utcnow
from paperintel.schemas.enums import (
    CanaryState,
    CircuitState,
    DataQualityState,
    ModuleHealthState,
    SanityOutcome,
    TaskState,
)


class ModuleHealthRecord(StrictModel):
    """Module health probe output (spec doc 03 §10).

    Shape follows the frozen example::

        {
          "module_id": "verification.numeric",
          "state": "HEALTHY",
          "version": "1.0.0",
          "checked_at": "...",
          "dependencies": {"database": "HEALTHY"},
          "checks": {"numeric_fixture": "PASS"},
          "metrics": {"processed_total": 1200, "failed_total": 3},
          "last_error": null
        }
    """

    module_id: str = Field(min_length=1)
    state: ModuleHealthState = ModuleHealthState.UNKNOWN
    version: str = "1.0.0"
    checked_at: datetime = Field(default_factory=utcnow)
    dependencies: dict[str, ModuleHealthState] = Field(default_factory=dict)
    checks: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    last_error: dict[str, Any] | None = None


class SanityCheckResult(FrozenModel):
    """One sanity check outcome (spec doc 06 §5).

    WARN normally maps to degraded quality; FAIL blocks downstream canonical
    analysis when data is unsafe.
    """

    name: str = Field(min_length=1)
    outcome: SanityOutcome
    details: str = ""


class DiskBreakdown(FrozenModel):
    objects: int = Field(default=0, ge=0)
    database_estimate: int = Field(default=0, ge=0)
    cache: int = Field(default=0, ge=0)
    temp: int = Field(default=0, ge=0)


class DiskStatus(FrozenModel):
    total_bytes: int = Field(default=0, ge=0)
    used_bytes: int = Field(default=0, ge=0)
    free_bytes: int = Field(default=0, ge=0)
    breakdown: DiskBreakdown = Field(default_factory=DiskBreakdown)


class QueueStatus(FrozenModel):
    pending: int = Field(default=0, ge=0)
    running: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class SystemStatus(StrictModel):
    """GET /v1/system/status payload (spec doc 04 §2.1)."""

    spec_version: str
    pipeline_version: str
    overall_state: ModuleHealthState
    queue: QueueStatus = Field(default_factory=QueueStatus)
    providers: dict[str, Any] = Field(default_factory=dict)
    workers: dict[str, Any] = Field(default_factory=dict)
    disk: DiskStatus = Field(default_factory=DiskStatus)
    pipeline: dict[str, Any] = Field(default_factory=dict)


class LlmProviderStatus(FrozenModel):
    """LLM provider health exposure (spec doc 04 §6).

    Provider status is not only HTTP reachability: a provider returning
    HTTP 200 is not automatically analytically healthy (doc 00 §7.14).
    """

    provider_id: str = Field(min_length=1)
    availability: ModuleHealthState = ModuleHealthState.UNKNOWN
    configured_concurrency: int = Field(default=0, ge=0)
    active_concurrency: int = Field(default=0, ge=0)
    latency_p50: float | None = Field(default=None, ge=0.0)
    latency_p90: float | None = Field(default=None, ge=0.0)
    rate_limit_events: int = Field(default=0, ge=0)
    transport_error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    valid_json_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    schema_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    citation_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    unsupported_claim_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    canary_state: CanaryState = CanaryState.UNKNOWN
    circuit_breaker_state: CircuitState = CircuitState.CLOSED


class OcrProviderStatus(FrozenModel):
    """OCR provider health exposure (spec doc 04 §6)."""

    provider_id: str = Field(min_length=1)
    availability: ModuleHealthState = ModuleHealthState.UNKNOWN
    concurrency: int = Field(default=0, ge=0)
    latency: float | None = Field(default=None, ge=0.0)
    transport_error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    response_validation_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    canary_state: CanaryState = CanaryState.UNKNOWN


class ModelQualityProfile(FrozenModel):
    """Online model quality profile (spec doc 06 §11).

    Used for routing recommendations: a model may be ANALYST_OK but
    VERIFIER_NOT_RECOMMENDED.
    """

    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    valid_json_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    schema_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence_reference_pass_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    unsupported_claim_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    numeric_error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    retry_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    agent_disagreement_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    latency_p50: float | None = Field(default=None, ge=0.0)
    latency_p90: float | None = Field(default=None, ge=0.0)
    routing_recommendations: dict[str, str] = Field(default_factory=dict)


class PipelineStatusResponse(StrictModel):
    """GET /v1/papers/{id}/pipeline payload (spec doc 04 §5)."""

    paper_id: str
    job_id: str | None = None
    requested_tier: str | None = None
    effective_tier: str | None = None
    state: TaskState | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)


class AuditSummary(StrictModel):
    """GET /v1/papers/{id}/audit payload (spec doc 04 §4)."""

    paper_id: str
    summary: dict[str, int] = Field(default_factory=dict)
    high_risk_claims: list[dict[str, Any]] = Field(default_factory=list)
    unsupported_claims: list[dict[str, Any]] = Field(default_factory=list)
    ocr_sensitive_claims: list[dict[str, Any]] = Field(default_factory=list)
    numeric_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    agent_disagreements: list[dict[str, Any]] = Field(default_factory=list)
    single_model_claims: list[dict[str, Any]] = Field(default_factory=list)
    external_inferences: list[dict[str, Any]] = Field(default_factory=list)
    verified_claims: list[dict[str, Any]] = Field(default_factory=list)


class PaperContextResponse(StrictModel):
    """GET /v1/papers/{id}/context payload (spec doc 04 §3).

    Preferred external-AI entry point; must avoid dumping entire paper text.
    """

    paper_id: str
    identity: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    one_sentence_purpose: str | None = None
    research_question: str | None = None
    verified_contributions: list[str] = Field(default_factory=list)
    method_overview: str | None = None
    experiment_overview: str | None = None
    notable_techniques: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    quality_vector_summary: dict[str, Any] = Field(default_factory=dict)
    audit_risk_summary: dict[str, Any] = Field(default_factory=dict)
    high_risk_claims: list[dict[str, Any]] = Field(default_factory=list)
    related_entities: list[dict[str, Any]] = Field(default_factory=list)
    drilldown_tools: list[str] = Field(default_factory=list)


class EvidenceQualityState(FrozenModel):
    """Pairing of execution state and data quality — never merged
    (spec doc 02 §10)."""

    task_state: TaskState
    data_quality: DataQualityState
    reason: str | None = None


__all__ = [
    "AuditSummary",
    "DiskBreakdown",
    "DiskStatus",
    "EvidenceQualityState",
    "LlmProviderStatus",
    "ModelQualityProfile",
    "ModuleHealthRecord",
    "OcrProviderStatus",
    "PaperContextResponse",
    "PipelineStatusResponse",
    "QueueStatus",
    "SanityCheckResult",
    "SystemStatus",
]

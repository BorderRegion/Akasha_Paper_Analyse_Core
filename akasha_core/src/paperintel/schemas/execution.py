"""Execution contracts: model calls, analysis runs, tasks, jobs, traces
(spec doc 03 §1.8-1.9, §6-7; spec doc 04 §14; spec doc 06 §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from paperintel.schemas.common import (
    FrozenModel,
    JobId,
    PaperId,
    PaperVersionId,
    RunId,
    Sha256Hex,
    TaskId,
    TraceId,
    utcnow,
)
from paperintel.schemas.enums import (
    DataQualityState,
    PipelineStage,
    ResourceTier,
    SchemaStatus,
    TaskState,
    TransportStatus,
)

# ---------------------------------------------------------------------------
# Model calls & analysis runs
# ---------------------------------------------------------------------------


class ModelCall(FrozenModel):
    """Fully attributable record of one model call (spec doc 03 §1.8).

    The request manifest references immutable evidence IDs and the prompt
    version rather than duplicating full evidence text.
    """

    model_call_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    task_id: TaskId | None = None
    run_id: RunId | None = None
    trace_id: TraceId | None = None
    prompt_version_id: str = Field(min_length=1)
    request_manifest_json: dict[str, Any] = Field(default_factory=dict)
    request_hash: Sha256Hex
    response_object_hash: Sha256Hex | None = None
    response_hash: Sha256Hex | None = None
    transport_status: TransportStatus
    schema_status: SchemaStatus
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    retry_number: int = Field(default=0, ge=0)
    created_at: datetime


class AnalysisRun(FrozenModel):
    """One agent/verifier/synthesizer run (spec doc 03 §1.9)."""

    run_id: RunId
    paper_id: PaperId | None = None
    paper_version_id: PaperVersionId | None = None
    collection_id: str | None = None
    agent_type: str = Field(min_length=1)
    pipeline_version: str = Field(min_length=1)
    config_hash: Sha256Hex
    model_id: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    status: TaskState
    started_at: datetime
    finished_at: datetime | None = None
    trace_id: TraceId | None = None


# ---------------------------------------------------------------------------
# Tasks & jobs
# ---------------------------------------------------------------------------


class TaskContract(FrozenModel):
    """Task record (spec doc 03 §6).

    The idempotency key must include enough stable dimensions to prevent
    accidental duplicate canonical outputs: task_type, paper_version_id,
    pipeline_version, relevant_config_hash, scope_hash.
    """

    task_id: TaskId
    job_id: JobId
    task_type: str = Field(min_length=1)
    module_id: str = Field(min_length=1)
    state: TaskState
    #: Execution state and data quality are separate concepts (doc 02 §10).
    quality_state: DataQualityState = DataQualityState.UNKNOWN
    priority: int = 0
    idempotency_key: str = Field(min_length=1)
    attempt: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    input_manifest: dict[str, Any] = Field(default_factory=dict)
    output_manifest: dict[str, Any] | None = None
    error_code: str | None = None
    error_details: dict[str, Any] | None = None
    trace_id: TraceId | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @model_validator(mode="after")
    def _attempt_bounds(self) -> TaskContract:
        if self.attempt > self.max_attempts:
            raise ValueError(
                f"attempt ({self.attempt}) exceeds max_attempts ({self.max_attempts}); "
                "retries are always bounded (spec doc 02 §12)"
            )
        return self


class JobContract(FrozenModel):
    """Paper-analysis job record (spec doc 03 §7)."""

    job_id: JobId
    paper_id: PaperId
    paper_version_id: PaperVersionId
    requested_tier: ResourceTier
    effective_tier: ResourceTier
    state: TaskState
    current_stage: PipelineStage = PipelineStage.IMPORTED
    priority: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trace_id: TraceId | None = None


class StageStatus(FrozenModel):
    """Per-stage status exposed by GET /v1/papers/{id}/pipeline
    (spec doc 04 §5)."""

    name: str = Field(min_length=1)
    state: TaskState
    quality: DataQualityState = DataQualityState.UNKNOWN
    subtasks: list[StageStatus] = Field(default_factory=list)


class JobProgress(FrozenModel):
    """Progress and ETA exposure (spec doc 04 §14).

    ETAs are null when insufficient historical data exists — never fabricated
    precision.
    """

    job_id: JobId
    current_stage: PipelineStage | None = None
    completed_weighted_work: float = Field(default=0.0, ge=0.0)
    total_weighted_work: float | None = Field(default=None, gt=0.0)
    pending_tasks: int = Field(default=0, ge=0)
    running_tasks: int = Field(default=0, ge=0)
    retrying_tasks: int = Field(default=0, ge=0)
    blocked_tasks: int = Field(default=0, ge=0)
    eta_p50_seconds: float | None = Field(default=None, ge=0.0)
    eta_p90_seconds: float | None = Field(default=None, ge=0.0)


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


class TraceEvent(FrozenModel):
    """One trace record (spec doc 06 §7).

    trace_id propagates through a paper job and child calls; events reconstruct
    job -> task -> agent run -> model call -> validation -> canonical write.
    """

    trace_id: TraceId
    occurred_at: datetime
    kind: str = Field(min_length=1)
    module_id: str | None = None
    job_id: JobId | None = None
    task_id: TaskId | None = None
    run_id: RunId | None = None
    model_call_id: str | None = None
    level: str = "INFO"
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "AnalysisRun",
    "JobContract",
    "JobProgress",
    "ModelCall",
    "StageStatus",
    "TaskContract",
    "TraceEvent",
]

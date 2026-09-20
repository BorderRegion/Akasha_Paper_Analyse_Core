"""operations.tracing — trace event recording + reading (spec doc 04 §12).

Every workflow lifecycle step (job creation, task dispatch/claim/completion/
failure/retry/cancellation, stage advancement, replay) appends an immutable
TraceEventRow. Trace records are append-only audit facts: they are never
updated or deleted, and they carry no secret material (values pass through
the same redaction used by JSON logs).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import AnalysisRunRow, ModelCallRow, TraceEventRow
from paperintel.operations.debug import redact
from paperintel.schemas.common import utcnow

#: Event kinds (stable strings; consumed by `paperctl trace`).
JOB_CREATED = "job.created"
JOB_COMPLETED = "job.completed"
JOB_CANCELLED = "job.cancelled"
STAGE_ADVANCED = "stage.advanced"
TASK_ENQUEUED = "task.enqueued"
TASK_REUSED = "task.reused"  # duplicate dispatch hit the idempotency key
TASK_CLAIMED = "task.claimed"
TASK_SUCCEEDED = "task.succeeded"
TASK_FAILED = "task.failed"
TASK_RETRYING = "task.retrying"
TASK_SKIPPED = "task.skipped"
TASK_CANCELLED = "task.cancelled"
TASK_REPLAYED = "task.replayed"
TASK_RESUMED = "task.resumed"


def record_trace_event(
    session: Session,
    *,
    trace_id: str | None,
    kind: str,
    message: str = "",
    module_id: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    model_call_id: str | None = None,
    level: str = "INFO",
    data: dict[str, Any] | None = None,
) -> TraceEventRow:
    """Append one trace event (flushed, not committed — caller owns tx)."""
    event = TraceEventRow(
        trace_id=trace_id or "",
        occurred_at=utcnow(),
        kind=kind,
        module_id=module_id,
        job_id=job_id,
        task_id=task_id,
        run_id=run_id,
        level=level,
        message=redact(message),
        data=redact(
            {**(data or {}), **({"model_call_id": model_call_id} if model_call_id else {})}
        ),
    )
    session.add(event)
    session.flush()
    return event


def read_trace(
    session: Session,
    trace_id: str,
    *,
    limit: int = 1000,
) -> list[TraceEventRow]:
    """Chronological events for one trace (doc 04 §12)."""
    events = list(
        session.scalars(
            select(TraceEventRow)
            .where(TraceEventRow.trace_id == trace_id)
            .order_by(TraceEventRow.occurred_at, TraceEventRow.event_id)
        ).all()
    )
    for run in session.scalars(select(AnalysisRunRow).where(AnalysisRunRow.trace_id == trace_id)):
        events.append(
            TraceEventRow(
                trace_id=trace_id,
                occurred_at=run.started_at,
                kind="agent.run",
                run_id=run.run_id,
                message=run.agent_type,
                data={
                    "state": run.status.value,
                    "spec_version": run.spec_version,
                    "prompt_version": run.prompt_version,
                },
            )
        )
    for call in session.scalars(select(ModelCallRow).where(ModelCallRow.trace_id == trace_id)):
        events.append(
            TraceEventRow(
                trace_id=trace_id,
                occurred_at=call.created_at,
                kind="model.call",
                task_id=call.task_id,
                run_id=call.run_id,
                message="Model call audit",
                data={
                    "model_call_id": call.model_call_id,
                    "provider_id": call.provider_id,
                    "model_id": call.model_id,
                    "prompt_version_id": call.prompt_version_id,
                    "schema_status": call.schema_status.value,
                    "transport_status": call.transport_status.value,
                    "request_hash": call.request_hash,
                    "response_hash": call.response_hash,
                },
            )
        )
    return sorted(events, key=lambda event: (event.occurred_at, event.event_id or 0))[:limit]

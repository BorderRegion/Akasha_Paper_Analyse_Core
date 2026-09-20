"""workflow.engine — job/task lifecycle, idempotency, resume, replay (P05).

The engine is a synchronous, transaction-scoped core: every function takes
the caller's Session, flushes, and never commits. Celery workers and the
CLI wrap this core (workflow.celery_app / paperctl) — no process-local
state participates in correctness (doc 02 §3).

Contracts enforced here:
- Task state machine (doc 03 §9): all transitions validated; invalid ones
  raise INTERNAL_002 domain errors.
- Pipeline stage machine (doc 03 §8): monotonic advancement only; a job
  that stops early because of tier/build scope records SKIPPED tasks
  naming the missing module — never an unexplained gap.
- Idempotency (doc 03 §6): one canonical output per
  (task_type, paper_version_id, pipeline_version, config_hash, scope_hash)
  key; duplicate dispatch returns the EXISTING task (no duplicate rows, no
  duplicate canonical outputs). Replays bump a replay ordinal so
  intentionally regenerated outputs get their own key.
- Bounded retries: attempt/max_attempts on the row; exhaustion → FAILED.
- Cancellation: cancelled jobs never dispatch new work.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from paperintel.database.models import JobRow, TaskRow
from paperintel.errors import DomainError
from paperintel.ids import new_job_id, new_task_id, new_trace_id
from paperintel.operations import tracing
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import (
    PIPELINE_STAGE_ORDER,
    TERMINAL_TASK_STATES,
    DataQualityState,
    PipelineStage,
    ResourceTier,
    TaskState,
    is_valid_task_transition,
    validate_task_transition,
)
from paperintel.version import PIPELINE_VERSION

#: Stages owned by built modules so far; every other stage is planned as a
#: SKIPPED task naming the phase that will deliver it (visible, honest).
IMPLEMENTED_STAGES: dict[PipelineStage, str] = {
    PipelineStage.EXTRACTED: "extract.rebuild_report",
    PipelineStage.STRUCTURED: "structure.reconstruct_sections",
    PipelineStage.EVIDENCE_INDEXED: "evidence.persist",
    PipelineStage.TRIAGED: "triage.paper",
    PipelineStage.ANALYZED: "agents.run_suite",
    PipelineStage.METADATA_RESOLVED: "metadata.resolve",
    PipelineStage.VERIFIED: "verification.run",
    PipelineStage.SYNTHESIZED: "agents.synthesize",
    PipelineStage.LINKED: "graph.link_entities",
    PipelineStage.SEARCH_INDEXED: "search.index",
}

#: Stages already satisfied by the import service (P03/P04) for a version.
IMPORT_SERVICE_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.IMPORTED,
    PipelineStage.FINGERPRINTED,
    PipelineStage.PDF_INSPECTED,
)

#: The phase that will deliver each not-yet-implemented stage.
FUTURE_STAGE_OWNER: dict[PipelineStage, str] = {
    PipelineStage.CORPUS_READY: "P12",
}

#: Reverse map: task type → the stage its completion advances.
STAGE_FOR_TASK: dict[str, PipelineStage] = {
    task_type: stage for stage, task_type in IMPLEMENTED_STAGES.items()
}

MAX_ATTEMPTS_DEFAULT = 3


def build_idempotency_key(
    task_type: str,
    paper_version_id: str,
    *,
    config_hash: str,
    scope_hash: str,
    replay: int = 0,
) -> str:
    """Stable key over the dimensions doc 03 §6 requires (plus a replay
    ordinal so intentional replays regenerate outputs under their own key)."""
    dimensions = (
        f"{task_type}|{paper_version_id}|{PIPELINE_VERSION}|{config_hash}|{scope_hash}|r{replay}"
    )
    digest = hashlib.sha256(dimensions.encode("utf-8")).hexdigest()[:40]
    return f"{task_type}:{digest}"


# ---------------------------------------------------------------------------
# job lifecycle
# ---------------------------------------------------------------------------


def create_job(
    session: Session,
    *,
    paper_id: str,
    paper_version_id: str,
    requested_tier: ResourceTier = ResourceTier.T1_SCAN,
    priority: int = 0,
    trace_id: str | None = None,
    current_stage: PipelineStage = PipelineStage.IMPORTED,
) -> JobRow:
    """Create a job at its current stage (import already reached
    IMPORTED→PDF_INSPECTED→EXTRACTED for imported versions)."""
    trace = trace_id or new_trace_id()
    job = JobRow(
        job_id=new_job_id(),
        paper_id=paper_id,
        paper_version_id=paper_version_id,
        requested_tier=requested_tier,
        effective_tier=requested_tier,
        state=TaskState.PENDING,
        current_stage=current_stage,
        priority=priority,
        trace_id=trace,
    )
    session.add(job)
    session.flush()
    tracing.record_trace_event(
        session,
        trace_id=trace,
        kind=tracing.JOB_CREATED,
        message=f"job created at stage {current_stage.value}",
        job_id=job.job_id,
        data={"paper_id": paper_id, "paper_version_id": paper_version_id},
    )
    return job


def _set_task_state(
    session: Session,
    task: TaskRow,
    to_state: TaskState,
    *,
    trace_id: str | None,
    kind: str,
    message: str,
    level: str = "INFO",
    data: dict[str, Any] | None = None,
) -> TaskRow:
    """Validated transition + trace event (single choke point)."""
    validate_task_transition(task.state, to_state, machine="task")
    task.state = to_state
    session.flush()
    tracing.record_trace_event(
        session,
        trace_id=trace_id,
        kind=kind,
        message=message,
        job_id=task.job_id,
        task_id=task.task_id,
        module_id=task.module_id,
        level=level,
        data=data,
    )
    return task


def enqueue_task(
    session: Session,
    job: JobRow,
    *,
    task_type: str,
    module_id: str,
    input_manifest: dict[str, Any],
    idempotency_key: str,
    priority: int = 0,
    max_attempts: int = MAX_ATTEMPTS_DEFAULT,
) -> tuple[TaskRow, bool]:
    """Register a task for dispatch. Idempotent: an existing task with the
    same key is returned (created=False) — duplicate dispatches never
    duplicate rows or canonical outputs.

    Refuses to enqueue new work for a cancelled job (no exceptions to
    cancellation, ever).
    """
    if job.state is TaskState.CANCELLED:
        raise DomainError(
            "INTERNAL_002",
            message="Cancelled jobs do not accept new work.",
            details={"job_id": job.job_id, "state": job.state.value},
        )
    existing = session.scalar(select(TaskRow).where(TaskRow.idempotency_key == idempotency_key))
    if existing is not None:
        tracing.record_trace_event(
            session,
            trace_id=job.trace_id,
            kind=tracing.TASK_REUSED,
            message=(
                f"duplicate dispatch for {task_type} reused task "
                f"{existing.task_id} (idempotency key match)"
            ),
            job_id=job.job_id,
            task_id=existing.task_id,
            module_id=module_id,
            data={"idempotency_key": idempotency_key},
        )
        return existing, False

    task = TaskRow(
        task_id=new_task_id(),
        job_id=job.job_id,
        task_type=task_type,
        module_id=module_id,
        state=TaskState.PENDING,
        priority=priority,
        idempotency_key=idempotency_key,
        attempt=0,
        max_attempts=max_attempts,
        input_manifest=input_manifest,
        trace_id=job.trace_id,
    )
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        # Concurrent dispatch lost the race: the winner's row IS the
        # canonical task — return it (never a duplicate).
        existing_after_race = session.scalar(
            select(TaskRow).where(TaskRow.idempotency_key == idempotency_key)
        )
        if existing_after_race is not None:
            tracing.record_trace_event(
                session,
                trace_id=job.trace_id,
                kind=tracing.TASK_REUSED,
                message=(
                    f"concurrent dispatch for {task_type} resolved to existing "
                    f"task {existing_after_race.task_id}"
                ),
                job_id=job.job_id,
                task_id=existing_after_race.task_id,
                module_id=module_id,
                data={"idempotency_key": idempotency_key},
            )
            return existing_after_race, False
        raise
    _set_task_state(
        session,
        task,
        TaskState.QUEUED,
        trace_id=job.trace_id,
        kind=tracing.TASK_ENQUEUED,
        message=f"{task_type} queued",
        data={"idempotency_key": idempotency_key},
    )
    return task, True


def skip_task(
    session: Session,
    job: JobRow,
    *,
    task_type: str,
    module_id: str,
    reason: str,
    input_manifest: dict[str, Any],
    idempotency_key: str,
) -> TaskRow:
    """PENDING → SKIPPED with the owning module/phase named (doc 03 §8:
    an early terminal scope is 'completed for that tier', never an
    unexplained missing stage).

    Idempotent by key: re-planning a job (a normal operation — `paperctl
    rerun` re-plans) reuses the existing skipped task instead of
    duplicating it or crashing on the unique key. A concurrent racer is
    resolved through a savepoint, exactly like enqueue_task.
    """
    if job.state is TaskState.CANCELLED:
        raise DomainError("INTERNAL_002", message="Cancelled jobs do not accept new work.")
    existing = session.scalar(select(TaskRow).where(TaskRow.idempotency_key == idempotency_key))
    if existing is not None:
        return existing

    task = TaskRow(
        task_id=new_task_id(),
        job_id=job.job_id,
        task_type=task_type,
        module_id=module_id,
        state=TaskState.PENDING,
        idempotency_key=idempotency_key,
        input_manifest=input_manifest,
        trace_id=job.trace_id,
    )
    try:
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        # A concurrent planner won the race — reuse its row.
        winner = session.scalar(select(TaskRow).where(TaskRow.idempotency_key == idempotency_key))
        if winner is None:  # pragma: no cover - defensive
            raise
        return winner
    return _set_task_state(
        session,
        task,
        TaskState.SKIPPED,
        trace_id=job.trace_id,
        kind=tracing.TASK_SKIPPED,
        message=reason,
        data={"task_type": task_type},
    )


# ---------------------------------------------------------------------------
# task execution lifecycle
# ---------------------------------------------------------------------------


def claim_task(session: Session, task_id: str) -> TaskRow | None:
    """QUEUED → RUNNING (claim). Returns None when the task cannot be
    claimed (already claimed/terminal) — duplicate execution is thereby
    impossible: exactly one worker wins the transition."""
    task = session.scalar(
        select(TaskRow)
        .where(TaskRow.task_id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None:
        raise DomainError(
            "QUEUE_001",
            message=f"Unknown task ID: {task_id}",
            details={"task_id": task_id},
        )
    if not is_valid_task_transition(task.state, TaskState.RUNNING):
        return None
    if task.next_retry_at is not None and task.next_retry_at > utcnow():
        return None
    task.next_retry_at = None
    task.attempt += 1
    task.started_at = utcnow()
    task.finished_at = None
    return _set_task_state(
        session,
        task,
        TaskState.RUNNING,
        trace_id=task.trace_id,
        kind=tracing.TASK_CLAIMED,
        message=f"attempt {task.attempt}/{task.max_attempts}",
        data={"attempt": task.attempt},
    )


def complete_task(
    session: Session,
    task_id: str,
    *,
    output_manifest: dict[str, Any],
    quality_state: DataQualityState = DataQualityState.GOOD,
    warnings: list[str] | None = None,
) -> TaskRow:
    """RUNNING → SUCCEEDED / SUCCEEDED_WITH_WARNINGS."""
    task = _require_task(session, task_id)
    warnings = warnings or []
    to_state = (
        TaskState.SUCCEEDED_WITH_WARNINGS
        if warnings or quality_state is DataQualityState.DEGRADED
        else TaskState.SUCCEEDED
    )
    validate_task_transition(task.state, to_state)
    task.output_manifest = output_manifest
    task.quality_state = quality_state
    task.finished_at = utcnow()
    result = _set_task_state(
        session,
        task,
        to_state,
        trace_id=task.trace_id,
        kind=tracing.TASK_SUCCEEDED,
        message=f"{task.task_type} {to_state.value}",
        data={"warnings": warnings, "quality_state": quality_state.value},
    )
    _advance_job_stage_for_task(session, task)
    _refresh_job_after_task(session, task)
    return result


def fail_task(
    session: Session,
    task_id: str,
    *,
    code: str,
    details: dict[str, Any] | None = None,
) -> TaskRow:
    """RUNNING → RETRYING → QUEUED while attempts remain (bounded);
    exhaustion → FAILED. The failure itself is always recorded."""
    task = _require_task(session, task_id)
    validate_task_transition(
        task.state, TaskState.RETRYING if task.attempt < task.max_attempts else TaskState.FAILED
    )
    task.error_code = code
    task.error_details = details or {}
    task.finished_at = utcnow()

    if task.attempt < task.max_attempts:
        delay = min(300, 2 ** min(max(task.attempt - 1, 0), 9))
        task.next_retry_at = utcnow() + timedelta(seconds=delay)
        _set_task_state(
            session,
            task,
            TaskState.RETRYING,
            trace_id=task.trace_id,
            kind=tracing.TASK_RETRYING,
            message=f"attempt {task.attempt}/{task.max_attempts} failed",
            data={"code": code},
        )
        result = _set_task_state(
            session,
            task,
            TaskState.QUEUED,
            trace_id=task.trace_id,
            kind=tracing.TASK_ENQUEUED,
            message="retry scheduled with bounded exponential backoff",
            data={
                "code": code,
                "delay_seconds": delay,
                "next_retry_at": task.next_retry_at.isoformat(),
            },
        )
        _refresh_job_after_task(session, task)
        return result

    result = _set_task_state(
        session,
        task,
        TaskState.FAILED,
        trace_id=task.trace_id,
        kind=tracing.TASK_FAILED,
        message=f"{task.task_type} failed permanently: retry budget exhausted",
        level="ERROR",
        data={"code": code, "attempt": task.attempt, "max_attempts": task.max_attempts},
    )
    _refresh_job_after_task(session, task)
    return result


def cancel_job(session: Session, job_id: str, *, reason: str = "") -> JobRow:
    """Cancel a job: every non-terminal task → CANCELLED, then the job
    itself. Cancellation is final: enqueue/dispatch refuse afterwards."""
    job = _require_job(session, job_id)
    if job.state in TERMINAL_TASK_STATES:
        raise DomainError(
            "QUEUE_001",
            message=f"Job {job_id} already terminal ({job.state.value}).",
            details={"job_id": job_id, "state": job.state.value},
        )
    for task in job_tasks(session, job_id):
        if task.state in TERMINAL_TASK_STATES:
            continue
        if task.state is TaskState.RUNNING:
            # RUNNING → CANCELLED is legal; the worker's completion for a
            # cancelled task is rejected by claim/complete validation.
            _set_task_state(
                session,
                task,
                TaskState.CANCELLED,
                trace_id=task.trace_id,
                kind=tracing.TASK_CANCELLED,
                message=f"cancelled: {reason}",
                level="WARNING",
            )
        elif is_valid_task_transition(task.state, TaskState.CANCELLED):
            _set_task_state(
                session,
                task,
                TaskState.CANCELLED,
                trace_id=task.trace_id,
                kind=tracing.TASK_CANCELLED,
                message=f"cancelled: {reason}",
                level="WARNING",
            )
    job.state = TaskState.CANCELLED
    job.finished_at = utcnow()
    session.flush()
    tracing.record_trace_event(
        session,
        trace_id=job.trace_id,
        kind=tracing.JOB_CANCELLED,
        message=f"job cancelled: {reason}" if reason else "job cancelled",
        job_id=job.job_id,
        level="WARNING",
    )
    return job


def advance_stage(session: Session, job_id: str, stage: PipelineStage) -> JobRow:
    """Monotonic stage progression (doc 03 §8). Equal stage = no-op;
    backwards jumps raise INTERNAL_002."""
    job = _require_job(session, job_id)
    current = job.current_stage
    if stage is current:
        return job
    current_index = PIPELINE_STAGE_ORDER.index(current)
    target_index = PIPELINE_STAGE_ORDER.index(stage)
    if target_index < current_index:
        raise DomainError(
            "INTERNAL_002",
            message=(f"Pipeline stage cannot move backwards ({current.value} → {stage.value})."),
            details={"job_id": job_id, "from": current.value, "to": stage.value},
        )
    job.current_stage = stage
    session.flush()
    tracing.record_trace_event(
        session,
        trace_id=job.trace_id,
        kind=tracing.STAGE_ADVANCED,
        message=f"stage {current.value} → {stage.value}",
        job_id=job.job_id,
    )
    return job


def job_tasks(session: Session, job_id: str) -> list[TaskRow]:
    return list(
        session.scalars(
            select(TaskRow)
            .where(TaskRow.job_id == job_id)
            .order_by(TaskRow.created_at, TaskRow.task_id)
        ).all()
    )


def job_progress(session: Session, job_id: str) -> dict[str, Any]:
    """Checkpoint view of a job (resume/debug surface)."""
    job = _require_job(session, job_id)
    tasks = job_tasks(session, job_id)
    by_state: dict[str, int] = {}
    for task in tasks:
        by_state[task.state.value] = by_state.get(task.state.value, 0) + 1
    eta_p50, eta_p90 = _estimate_remaining(session, tasks)
    return {
        "job_id": job.job_id,
        "state": job.state.value,
        "current_stage": job.current_stage.value,
        "trace_id": job.trace_id,
        "tasks_total": len(tasks),
        "tasks_by_state": dict(sorted(by_state.items())),
        "terminal": job.state in TERMINAL_TASK_STATES,
        "eta_p50": eta_p50,
        "eta_p90": eta_p90,
    }


def _estimate_remaining(
    session: Session, tasks: list[TaskRow]
) -> tuple[float | None, float | None]:
    """Serial-work estimate from the most recent 100 successful peers per type.

    Require five observations for every outstanding task type. Missing history
    remains unknown; failed/blocked work cannot receive a reliable ETA.
    """
    import math

    pending = [t for t in tasks if t.state not in TERMINAL_TASK_STATES]
    if not pending:
        return (0.0, 0.0) if tasks else (None, None)
    if any(t.state is TaskState.BLOCKED for t in pending):
        return None, None
    estimates = {}
    for task_type in {t.task_type for t in pending}:
        peers = session.scalars(
            select(TaskRow)
            .where(
                TaskRow.task_type == task_type,
                TaskRow.state.in_([TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS]),
                TaskRow.started_at.is_not(None),
                TaskRow.finished_at.is_not(None),
            )
            .order_by(TaskRow.finished_at.desc())
            .limit(100)
        ).all()
        durations = sorted(
            (p.finished_at - p.started_at).total_seconds()
            for p in peers
            if p.finished_at >= p.started_at
        )
        if len(durations) < 5:
            return None, None
        estimates[task_type] = (
            durations[math.ceil(len(durations) * 0.5) - 1],
            durations[math.ceil(len(durations) * 0.9) - 1],
        )
    total50 = total90 = 0.0
    for task in pending:
        elapsed = (
            max(0, (utcnow() - task.started_at).total_seconds())
            if (task.state is TaskState.RUNNING and task.started_at)
            else 0
        )
        p50, p90 = estimates[task.task_type]
        total50 += max(0, p50 - elapsed)
        total90 += max(0, p90 - elapsed)
    return total50, total90


def _advance_job_stage_for_task(session: Session, task: TaskRow) -> None:
    """A completed stage task moves the job's current_stage FORWARD only
    (never backwards — parallel stage completions keep the furthest stage)."""
    stage = STAGE_FOR_TASK.get(task.task_type)
    if stage is None:
        return
    job = session.get(JobRow, task.job_id)
    if job is None:
        return
    current_index = PIPELINE_STAGE_ORDER.index(job.current_stage)
    target_index = PIPELINE_STAGE_ORDER.index(stage)
    if target_index > current_index:
        advance_stage(session, job.job_id, stage)


def _refresh_job_after_task(session: Session, task: TaskRow) -> None:
    """Derive job state from tasks: all terminal → job terminal."""
    job = session.get(JobRow, task.job_id)
    if job is None or job.state in TERMINAL_TASK_STATES:
        return
    # Replays supersede state, never the historical audit rows.
    latest_by_type = {t.task_type: t for t in job_tasks(session, job.job_id)}
    tasks = list(latest_by_type.values())
    if not tasks:
        return
    if any(t.state is TaskState.FAILED for t in tasks):
        job.state = TaskState.FAILED
    elif all(t.state in TERMINAL_TASK_STATES for t in tasks):
        if any(
            t.state in (TaskState.SKIPPED, TaskState.SUCCEEDED_WITH_WARNINGS)
            or t.quality_state is DataQualityState.DEGRADED
            for t in tasks
        ):
            job.state = TaskState.SUCCEEDED_WITH_WARNINGS
        else:
            job.state = TaskState.SUCCEEDED
    else:
        job.state = TaskState.RUNNING
    from paperintel.schemas.common import utcnow  # noqa: PLC0415

    if job.state in TERMINAL_TASK_STATES:
        job.finished_at = utcnow()
        tracing.record_trace_event(
            session,
            trace_id=job.trace_id,
            kind=tracing.JOB_COMPLETED,
            message=f"job {job.state.value}",
            job_id=job.job_id,
            level="WARNING"
            if job.state in (TaskState.FAILED, TaskState.SUCCEEDED_WITH_WARNINGS)
            else "INFO",
        )
    else:
        job.started_at = job.started_at or utcnow()
    session.flush()


# ---------------------------------------------------------------------------
# resume + replay
# ---------------------------------------------------------------------------


def resume_job(session: Session, job_id: str) -> list[TaskRow]:
    """Worker-interruption recovery. Stale RUNNING tasks transition
    RUNNING→RETRYING→QUEUED (legal path per doc 03 §9) with the
    interruption recorded; WAITING tasks re-queue. CANCELLED jobs refuse.

    Returns the re-queued tasks (checkpoints live in the manifests, so
    re-execution resumes from the recorded state).
    """
    job = _require_job(session, job_id)
    if job.state is TaskState.CANCELLED:
        raise DomainError(
            "INTERNAL_002",
            message="Cancelled jobs cannot be resumed.",
            details={"job_id": job_id, "state": job.state.value},
        )
    requeued: list[TaskRow] = []
    for task in job_tasks(session, job_id):
        if task.state is TaskState.RUNNING:
            # A task left RUNNING by a dead worker: attempt already counted,
            # interruption reason recorded, legal RETRYING→QUEUED path back.
            _set_task_state(
                session,
                task,
                TaskState.RETRYING,
                trace_id=task.trace_id,
                kind=tracing.TASK_RESUMED,
                message="worker interruption detected; task recovered",
                level="WARNING",
                data={"recovered_attempt": task.attempt},
            )
            requeued.append(
                _set_task_state(
                    session,
                    task,
                    TaskState.QUEUED,
                    trace_id=task.trace_id,
                    kind=tracing.TASK_RESUMED,
                    message="recovered task re-queued",
                )
            )
        elif task.state is TaskState.WAITING:
            requeued.append(
                _set_task_state(
                    session,
                    task,
                    TaskState.QUEUED,
                    trace_id=task.trace_id,
                    kind=tracing.TASK_RESUMED,
                    message="waiting task re-queued on resume",
                )
            )
    if job.state is not TaskState.RUNNING and job.state not in TERMINAL_TASK_STATES:
        job.state = TaskState.RUNNING
        session.flush()
    return requeued


def replay_stage(
    session: Session,
    job_id: str,
    stage: PipelineStage,
    *,
    input_manifest: dict[str, Any] | None = None,
) -> TaskRow:
    """Replay EXACTLY ONE stage (doc 01: "Can only this stage be replayed?").

    Creates a fresh task for the stage with replay ordinal +1 on the
    idempotency key — the original task stays untouched (audit trail), and
    the regenerated canonical output supersedes where the store supports
    supersession. Tasks of other stages are never touched.
    """
    job = _require_job(session, job_id)
    if job.state is TaskState.CANCELLED:
        raise DomainError("INTERNAL_002", message="Cancelled jobs cannot be replayed.")
    task_type = IMPLEMENTED_STAGES.get(stage)
    if task_type is None:
        raise DomainError(
            "CFG_002",
            message=(
                f"Stage {stage.value} has no implemented module yet "
                f"({FUTURE_STAGE_OWNER.get(stage, 'unknown phase')})."
            ),
            details={"stage": stage.value},
        )
    previous = [
        t
        for t in job_tasks(session, job_id)
        if t.task_type == task_type and t.state in TERMINAL_TASK_STATES
    ]
    replay_ordinal = len(previous)
    original = previous[-1] if previous else None
    if original is not None:
        manifest = dict(original.input_manifest)
    elif input_manifest is not None:
        # Stage satisfied outside the workflow (import path) — the caller
        # supplies the canonical manifest (report path, hashes, data dir).
        manifest = dict(input_manifest)
    else:
        raise DomainError(
            "CFG_002",
            message=(
                f"Stage {stage.value} was satisfied without a task and no "
                "input manifest was given for the replay."
            ),
            details={"stage": stage.value, "job_id": job_id},
        )
    key = build_idempotency_key(
        task_type,
        job.paper_version_id,
        config_hash=str(manifest.get("config_hash", "")),
        scope_hash=str(manifest.get("scope_hash", "")),
        replay=replay_ordinal,
    )
    task, _created = enqueue_task(
        session,
        job,
        task_type=task_type,
        module_id=task_type,
        input_manifest=manifest,
        idempotency_key=key,
        priority=job.priority,
    )
    if task.state not in TERMINAL_TASK_STATES:
        job.state = TaskState.QUEUED
        job.finished_at = None
        session.flush()
    tracing.record_trace_event(
        session,
        trace_id=job.trace_id,
        kind=tracing.TASK_REPLAYED,
        message=f"stage {stage.value} replay requested",
        job_id=job.job_id,
        task_id=task.task_id,
        module_id=task_type,
        data={"stage": stage.value, "replay_ordinal": replay_ordinal},
    )
    return task


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def build_base_manifest(
    *,
    paper_version_id: str,
    content_sha256: str,
    report_path: str,
    data_dir: str,
) -> dict[str, Any]:
    """The canonical task input manifest for a version.

    Shared by plan_job, the CLI rerun path, the selftest and tests so a
    replay always carries the same manifest shape (report cache location,
    content hash, config/scope hashes).
    """
    from paperintel.config.fingerprint import analysis_config_hash

    config_hash = analysis_config_hash()
    return {
        "paper_version_id": paper_version_id,
        "content_sha256": content_sha256,
        "config_hash": config_hash,
        "scope_hash": content_sha256[:16],
        "report_path": report_path,
        "data_dir": str(data_dir),
    }


def plan_job(
    session: Session,
    job: JobRow,
    *,
    data_dir: str,
    content_sha256: str,
    report_path: str,
) -> dict[str, Any]:
    """Plan the job's tasks from the version's ACTUAL progress.

    Stages already satisfied (report cached, sections/evidence persisted by
    the import path) produce no task; implemented-but-unsatisfied stages
    get enqueued tasks (idempotent keys); stages owned by future phases get
    SKIPPED tasks naming the phase — the tier's terminal scope stays
    explicit (doc 03 §8), never an unexplained missing stage.
    """
    from pathlib import Path

    plan_result: dict[str, Any] = {
        "job_id": job.job_id,
        "enqueued": [],
        "skipped": [],
        "satisfied": [],
    }
    version_id = job.paper_version_id
    from paperintel.config.fingerprint import analysis_config_hash

    config_hash = analysis_config_hash(tier=job.effective_tier.value)

    def _base_manifest() -> dict[str, Any]:
        return {
            "paper_version_id": version_id,
            "content_sha256": content_sha256,
            "config_hash": config_hash,
            "scope_hash": content_sha256[:16],
            "report_path": report_path,
            "data_dir": str(data_dir),
        }

    from paperintel.database.models import (  # noqa: PLC0415
        AnalysisRunRow,
        EvidenceRow,
        RelationRow,
        SearchDocumentRow,
        SectionRow,
        TriageResultRow,
    )
    from paperintel.operations.disk import disk_status, require_capacity  # noqa: PLC0415
    from paperintel.schemas.enums import ResourceTier  # noqa: PLC0415
    from paperintel.triage.budget import (  # noqa: PLC0415
        budget_for_tier,
        stage_allowed,
        tier_skip_reason,
    )
    from paperintel.triage.service import latest_triage  # noqa: PLC0415

    # Analysis budget policy (P10, doc 07 §2): the paper's effective tier
    # decides which stages may run. Without a triage decision the documented
    # T2_FULL default applies (never an ad-hoc choice).
    triage_row = latest_triage(session, job.paper_id)
    effective_tier = triage_row.effective_tier if triage_row else ResourceTier.T2_FULL
    budget = budget_for_tier(effective_tier)
    plan_result["effective_tier"] = effective_tier.value
    plan_result["budget_scope"] = budget.description

    # Low-disk protection (P10, doc 07 §8): a critical disk blocks deep
    # expansion outright (RESOURCE_001); a warning degrades and is recorded.
    disk = disk_status(data_dir)
    plan_result["disk_level"] = disk.level
    if disk.level == "CRITICAL":
        require_capacity(tier=effective_tier, operation="job planning", path=data_dir)
        plan_result.setdefault("warnings", []).append(
            "disk critical: nonessential cache growth is reduced"
        )

    report_cached = Path(report_path).is_file()
    sections_exist = (
        session.scalar(
            select(SectionRow.section_id).where(SectionRow.paper_version_id == version_id).limit(1)
        )
        is not None
    )
    evidence_exists = (
        session.scalar(
            select(EvidenceRow.evidence_id)
            .where(EvidenceRow.paper_version_id == version_id)
            .limit(1)
        )
        is not None
    )

    triaged = (
        session.scalar(
            select(TriageResultRow.triage_id)
            .where(TriageResultRow.paper_id == job.paper_id)
            .limit(1)
        )
        is not None
    )
    metadata_task_done = (
        session.scalar(
            select(TaskRow.task_id)
            .where(
                TaskRow.job_id == job.job_id,
                TaskRow.task_type == "metadata.resolve",
                TaskRow.state.in_([TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS]),
            )
            .limit(1)
        )
        is not None
    )
    verification_run = session.scalar(
        select(AnalysisRunRow.run_id)
        .where(
            AnalysisRunRow.paper_version_id == version_id,
            AnalysisRunRow.agent_type == "verification",
            AnalysisRunRow.status.in_([TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS]),
        )
        .limit(1)
    )
    synthesis_run = session.scalar(
        select(AnalysisRunRow.run_id)
        .where(
            AnalysisRunRow.paper_version_id == version_id,
            AnalysisRunRow.agent_type == "agents.synthesizer",
            AnalysisRunRow.status.in_([TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS]),
        )
        .limit(1)
    )
    graph_linked = (
        session.scalar(
            select(RelationRow.relation_id).where(RelationRow.paper_id == job.paper_id).limit(1)
        )
        is not None
    )
    search_indexed = (
        session.scalar(
            select(SearchDocumentRow.document_id)
            .where(SearchDocumentRow.paper_version_id == version_id)
            .limit(1)
        )
        is not None
    )
    from paperintel.agents.base import AGENTS
    from paperintel.agents.builtin import SynthesizerAgent
    from paperintel.triage.budget import agent_allowed

    required_agents = {
        name for name in AGENTS
        if name != SynthesizerAgent.agent_type and agent_allowed(effective_tier, name)
    }
    completed_agents = set(session.scalars(
        select(AnalysisRunRow.agent_type).where(
            AnalysisRunRow.paper_version_id == version_id,
            AnalysisRunRow.agent_type.in_(required_agents),
            AnalysisRunRow.status.in_([TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS]),
        )
    ))
    claims_analyzed = bool(required_agents) and required_agents <= completed_agents

    stage_state = {
        PipelineStage.EXTRACTED: report_cached,
        PipelineStage.STRUCTURED: sections_exist,
        PipelineStage.EVIDENCE_INDEXED: evidence_exists,
        PipelineStage.TRIAGED: triaged,
        PipelineStage.ANALYZED: claims_analyzed,
        # METADATA_RESOLVED is satisfied by its own task outcome (external
        # sources are optional; a degraded-but-recorded outcome counts).
        PipelineStage.METADATA_RESOLVED: metadata_task_done,
        PipelineStage.VERIFIED: verification_run is not None,
        PipelineStage.SYNTHESIZED: synthesis_run is not None,
        PipelineStage.LINKED: graph_linked,
        PipelineStage.SEARCH_INDEXED: search_indexed,
    }

    for stage in PIPELINE_STAGE_ORDER:
        if stage in IMPORT_SERVICE_STAGES:
            # Satisfied by the import service (P03) before the job existed.
            plan_result["satisfied"].append(stage.value)
            continue
        task_type = IMPLEMENTED_STAGES.get(stage)
        if task_type is not None:
            if stage_state.get(stage):
                plan_result["satisfied"].append(stage.value)
                continue
            if not stage_allowed(effective_tier, stage):
                # Policy scope: the tier does not run this stage. Recorded as
                # an explicit SKIPPED task with the reason (doc 03 §8), never
                # an unexplained missing stage.
                skip_task(
                    session,
                    job,
                    task_type=f"stage.{stage.value.lower()}",
                    module_id=f"tier:{effective_tier.value}",
                    reason=tier_skip_reason(effective_tier, stage),
                    input_manifest=_base_manifest(),
                    idempotency_key=build_idempotency_key(
                        f"stage.{stage.value.lower()}",
                        version_id,
                        config_hash=config_hash,
                        scope_hash=content_sha256[:16],
                    ),
                )
                plan_result["skipped"].append(stage.value)
                continue
            task, _created = enqueue_task(
                session,
                job,
                task_type=task_type,
                module_id=task_type,
                input_manifest=_base_manifest(),
                idempotency_key=build_idempotency_key(
                    task_type,
                    version_id,
                    config_hash=config_hash,
                    scope_hash=content_sha256[:16],
                ),
                priority=job.priority,
            )
            plan_result["enqueued"].append({"stage": stage.value, "task_id": task.task_id})
        elif stage in FUTURE_STAGE_OWNER:
            skip_task(
                session,
                job,
                task_type=f"stage.{stage.value.lower()}",
                module_id=f"future:{FUTURE_STAGE_OWNER[stage]}",
                reason=(
                    f"stage {stage.value} module not built yet "
                    f"(delivered by {FUTURE_STAGE_OWNER[stage]})"
                ),
                input_manifest=_base_manifest(),
                idempotency_key=build_idempotency_key(
                    f"stage.{stage.value.lower()}",
                    version_id,
                    config_hash=config_hash,
                    scope_hash=content_sha256[:16],
                ),
            )
            plan_result["skipped"].append(stage.value)

    _refresh_job_after_task(session, _first_task_or_stub(session, job))
    return plan_result


def _first_task_or_stub(session: Session, job: JobRow) -> TaskRow:
    tasks = job_tasks(session, job.job_id)
    if tasks:
        return tasks[0]
    # Job with no tasks yet: fabricate a transient row-like stub for the
    # state refresh without persisting it.
    stub = TaskRow(
        task_id=new_task_id(),
        job_id=job.job_id,
        task_type="noop",
        module_id="noop",
        state=TaskState.SUCCEEDED,
        idempotency_key=f"noop:{job.job_id}",
        input_manifest={},
    )
    return stub


def _require_task(session: Session, task_id: str) -> TaskRow:
    task = session.scalar(
        select(TaskRow).where(TaskRow.task_id == task_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if task is None:
        raise DomainError(
            "QUEUE_001",
            message=f"Unknown task ID: {task_id}",
            details={"task_id": task_id},
        )
    return task


def _require_job(session: Session, job_id: str) -> JobRow:
    job = session.get(JobRow, job_id)
    if job is None:
        raise DomainError(
            "QUEUE_001",
            message=f"Unknown job ID: {job_id}",
            details={"job_id": job_id},
        )
    return job

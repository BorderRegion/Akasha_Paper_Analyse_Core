"""workflow.celery_app — Celery dispatch + synchronous task runner (P05).

Dispatch model (doc 02 §3: api/worker must not depend on process-local
state): the engine core (workflow.engine) is synchronous and
transaction-scoped; this module is the ONLY place that talks to Celery.
Workers execute one task per invocation via run_task_once(), which is the
same code path the CLI and tests use — there is no worker-specific logic.

Bounded retries are owned by the task ROW (attempt/max_attempts): a failed
execution returns the task to QUEUED (RETRYING→QUEUED) and the dispatcher
re-submits; retry exhaustion marks the task FAILED. Celery's own retry
machinery is deliberately unused (single source of truth).

Eager mode: PAPERINTEL_TASKS_EAGER=1 (or settings) executes dispatched
tasks synchronously in-process — used by tests and paperctl rerun; it is
an explicit mode, never a silent fallback (the flag is recorded in the
dispatch trace event).
"""

from __future__ import annotations

import os
from typing import Any

from celery.local import Proxy

from paperintel.errors import DomainError
from paperintel.schemas.enums import DataQualityState, TaskState

_EAGER_ENV = "PAPERINTEL_TASKS_EAGER"


def tasks_eager() -> bool:
    """Explicit synchronous dispatch mode (tests / CLI rerun)."""
    return os.environ.get(_EAGER_ENV, "") == "1"


def create_celery_app():
    """Build the Celery application from settings (env-only config)."""
    from celery import Celery  # noqa: PLC0415

    from paperintel.config.settings import load_config  # noqa: PLC0415

    settings = load_config(None)
    app = Celery(
        "paperintel",
        broker=settings.celery.broker_url,
        backend=settings.celery.result_backend,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_acks_late=settings.celery.task_acks_late,
        worker_prefetch_multiplier=settings.celery.worker_prefetch_multiplier,
        # Bounded retries live on the task ROW (engine), not in Celery.
        task_default_max_retries=None,
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "priority_steps": list(range(10)),
            "queue_order_strategy": "priority",
        },
        task_default_priority=5,
    )
    app.task(name=TASK_NAME)(run_task_once_standalone)
    app.task(name="paperintel.dispatch.pending")(dispatch_pending)
    from paperintel.operations.worker_heartbeat import install_signals

    install_signals()
    app.conf.beat_schedule = {
        "dispatch-durable-queue": {
            "task": "paperintel.dispatch.pending",
            "schedule": 10.0,
            "options": {"priority": 0},
        }
    }
    return app


#: Module-level app for `celery -A paperintel.workflow.celery_app worker`.
celery_app = None  # lazily built: import must not require a live broker


def get_celery_app():
    global celery_app  # noqa: PLW0603
    if celery_app is None:
        celery_app = create_celery_app()
    return celery_app


TASK_NAME = "paperintel.task.run"


def dispatch_candidates(session, *, limit=100):
    """First outstanding stage per job, with T3 ahead of bulk work.

    Repeated broker messages are safe: the transactional claim is authoritative.
    Keeping queued work in PostgreSQL closes the commit/publish crash window.
    """
    from sqlalchemy import select

    from paperintel.database.models import JobRow, TaskRow
    from paperintel.schemas.common import utcnow
    from paperintel.schemas.enums import PIPELINE_STAGE_ORDER
    from paperintel.workflow.engine import STAGE_FOR_TASK

    terminal = {
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED_WITH_WARNINGS,
        TaskState.SKIPPED,
        TaskState.CANCELLED,
        TaskState.FAILED,
    }
    rows = session.scalars(
        select(TaskRow)
        .join(JobRow)
        .where(
            JobRow.state.notin_([TaskState.CANCELLED, TaskState.FAILED]),
            TaskRow.state.notin_(terminal),
        )
        .order_by(TaskRow.created_at, TaskRow.task_id)
    ).all()
    # Replayed earlier stages must precede their still-queued dependants.
    rows.sort(key=lambda t: (
        PIPELINE_STAGE_ORDER.index(STAGE_FOR_TASK[t.task_type])
        if t.task_type in STAGE_FOR_TASK else len(PIPELINE_STAGE_ORDER),
        t.created_at, t.task_id,
    ))
    first = {}
    for task in rows:
        first.setdefault(task.job_id, task)
    due = [
        task
        for task in first.values()
        if task.state is TaskState.QUEUED
        and (task.next_retry_at is None or task.next_retry_at <= utcnow())
    ]
    due.sort(
        key=lambda task: (
            task.job.effective_tier.value != "T3_DEEP",
            -task.priority,
            task.created_at,
        )
    )
    return [task.task_id for task in due[:limit]]


def dispatch_pending():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from paperintel.config.settings import get_settings

    database_url = get_settings().database.url
    db_engine = create_engine(database_url)
    try:
        with Session(db_engine) as session:
            candidates = dispatch_candidates(session)
        for task_id in candidates:
            submit_task(task_id, database_url=database_url)
        return {"submitted": len(candidates)}
    finally:
        db_engine.dispose()


def submit_task(task_id: str, *, database_url: str) -> dict[str, Any]:
    """Submit one QUEUED task for execution.

    Eager mode runs it synchronously (result returned). Otherwise the task
    is sent to the broker; broker failures raise QUEUE_001 (never a silent
    local fallback pretending to be a worker).
    """
    if tasks_eager():
        return run_task_once_standalone(task_id, database_url)
    app = get_celery_app()
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from paperintel.database.models import TaskRow
    from paperintel.schemas.common import utcnow

    db_engine = create_engine(database_url)
    try:
        with Session(db_engine) as session:
            task = session.get(TaskRow, task_id)
            if task is None:
                raise DomainError("QUEUE_001", message="Cannot dispatch an unknown task.")
            # Redis priority 0 is highest; reserve it for T3 / interactive audits.
            priority = (
                0
                if task.job.effective_tier.value == "T3_DEEP"
                else max(1, min(9, 5 - task.priority))
            )
            countdown = (
                max(0, (task.next_retry_at - utcnow()).total_seconds()) if task.next_retry_at else 0
            )
    finally:
        db_engine.dispose()
    try:
        result = app.send_task(
            TASK_NAME, args=[task_id, database_url], priority=priority, countdown=countdown
        )
        return {"submitted": True, "task_id": task_id, "celery_id": result.id}
    except Exception as exc:  # noqa: BLE001 - broker errors are operational
        raise DomainError(
            "QUEUE_001",
            message="Job queue unavailable while dispatching task.",
            details={"task_id": task_id, "reason": type(exc).__name__},
        ) from exc


def run_task_once_standalone(task_id: str, database_url: str) -> dict[str, Any]:
    """Open own engine/session and execute one task (worker entry path)."""
    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    engine = create_engine(database_url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with factory() as session:
            outcome = run_task_once(session, task_id)
            session.commit()
            from paperintel.database.models import TaskRow

            task = session.get(TaskRow, task_id)
            retry = (
                task is not None
                and task.state is TaskState.QUEUED
                and task.next_retry_at is not None
            )
        if retry and not tasks_eager():
            submit_task(task_id, database_url=database_url)
        return outcome
    finally:
        engine.dispose()


def run_task_once(session, task_id: str) -> dict[str, Any]:
    """Execute one task inside the GIVEN session: claim → handler →
    complete/fail. Duplicate execution is impossible: claim_task only
    succeeds for QUEUED tasks (exactly one claimant wins).

    This is the single execution path shared by workers, eager dispatch,
    the CLI, and tests.
    """
    from paperintel.workflow import engine, handlers  # noqa: PLC0415

    task = engine.claim_task(session, task_id)
    if task is None:
        return {
            "task_id": task_id,
            "claimed": False,
            "outcome": "not_claimed",
            "state": _task_state(session, task_id),
            "note": "task not claimable (already running or terminal)",
        }
    try:
        # Keep the claim/attempt outside the savepoint. A handler may flush
        # several outputs before failing, including with an IntegrityError.
        # Roll those writes back while keeping the outer transaction usable
        # for durable retry/error accounting.
        with session.begin_nested():
            output = handlers.run_handler(session, task)
    except DomainError as exc:
        engine.fail_task(session, task_id, code=exc.code, details=exc.details)
        return {
            "task_id": task_id,
            "claimed": True,
            "outcome": "failed",
            "code": exc.code,
        }
    except Exception as exc:  # noqa: BLE001 - recorded, retried, never hidden
        from paperintel.operations.logging import get_logger  # noqa: PLC0415

        get_logger("paperintel.workflow").error(
            "task %s raised %s: %s",
            task_id,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        engine.fail_task(
            session,
            task_id,
            code="INTERNAL_001",
            details={"type": type(exc).__name__},
        )
        return {
            "task_id": task_id,
            "claimed": True,
            "outcome": "failed",
            "code": "INTERNAL_001",
            "detail": f"{type(exc).__name__}",
        }
    quality = handlers.quality_from_output(output)
    engine.complete_task(
        session,
        task_id,
        output_manifest=output,
        quality_state=quality,
        warnings=list(output.get("warnings") or []),
    )
    return {
        "task_id": task_id,
        "claimed": True,
        "outcome": "succeeded",
        "state": TaskState.SUCCEEDED.value
        if quality is DataQualityState.GOOD
        else TaskState.SUCCEEDED_WITH_WARNINGS.value,
        "output": output,
    }


def _task_state(session, task_id: str) -> str | None:
    from paperintel.database.models import TaskRow  # noqa: PLC0415

    task = session.get(TaskRow, task_id)
    return task.state.value if task is not None else None


# Celery CLI discovers `app`; construction remains lazy and needs no broker.
app = Proxy(get_celery_app)

"""Actual Redis/Celery delivery and persisted delayed retries, isolated queue/DB."""

import asyncio
import time
import uuid

import pytest
from celery.contrib.testing.worker import start_worker
from sqlalchemy.orm import Session
from tests.fixtures.generators import build_f01_native

from paperintel.database.models import TaskRow
from paperintel.errors import DomainError
from paperintel.ingest.service import import_pdf
from paperintel.schemas.enums import TaskState
from paperintel.storage.object_store import LocalObjectStore
from paperintel.workflow import celery_app, handlers
from paperintel.workflow import engine as workflow

pytestmark = [pytest.mark.needs_db, pytest.mark.needs_redis]


@pytest.fixture()
def heartbeat_cleanup():
    # Celery's embedded test worker exits through test_worker_stopped rather
    # than the normal process shutdown path. Release the thread it started.
    from paperintel.operations import worker_heartbeat

    yield
    worker_heartbeat._stop()


def test_real_worker_retries_after_persisted_deadline(engine, test_db_url, tmp_path, monkeypatch, heartbeat_cleanup):
    pdf = tmp_path / "worker.pdf"
    pdf.write_bytes(build_f01_native())
    with Session(engine) as session:
        imported = asyncio.run(
            import_pdf(
                pdf,
                session=session,
                store=LocalObjectStore(tmp_path / "objects"),
                data_dir=tmp_path,
            )
        )
        job = workflow.create_job(
            session, paper_id=imported.paper_id, paper_version_id=imported.paper_version_id
        )
        task, _ = workflow.enqueue_task(
            session,
            job,
            task_type="review.worker",
            module_id="review",
            input_manifest={},
            idempotency_key="review-worker",
        )
        task_id = task.task_id
        session.commit()

    calls = []

    def handler(session, task):
        from paperintel.config.settings import get_settings
        from paperintel.operations.heartbeat import read_heartbeats

        beats = read_heartbeats(get_settings().core.data_dir)
        assert len(beats) == 1
        assert beats[0].state == "FRESH"
        assert beats[0].detail["task_ids"]
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise DomainError("LLM_001", message="Transient test failure")
        return {}

    app = celery_app.create_celery_app()
    queue = "paperintel-review-" + uuid.uuid4().hex
    app.conf.task_default_queue = queue
    monkeypatch.setattr(celery_app, "get_celery_app", lambda: app)
    monkeypatch.setattr(handlers, "run_handler", handler)
    with start_worker(
        app, pool="solo", queues=[queue], perform_ping_check=False, shutdown_timeout=15
    ):
        submitted = celery_app.submit_task(task_id, database_url=test_db_url)
        first = app.AsyncResult(submitted["celery_id"]).get(timeout=10, disable_sync_subtasks=False)
        assert first["outcome"] == "failed"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with Session(engine) as session:
                row = session.get(TaskRow, task_id)
                if row.state is TaskState.SUCCEEDED:
                    assert row.attempt == 2 and row.next_retry_at is None
                    break
            time.sleep(0.05)
        else:
            pytest.fail("Worker did not deliver the durable retry")
    assert len(calls) == 2 and calls[1] - calls[0] >= 0.9
    app.close()

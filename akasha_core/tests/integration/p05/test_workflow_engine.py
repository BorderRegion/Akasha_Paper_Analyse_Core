"""P05 workflow engine integration tests (disposable DB + tmp store).

Gate requirements (doc 05 P05): duplicate task dispatch does not duplicate
canonical outputs; worker interruption recovery; replay one stage only;
invalid state transition rejected; retry exhaustion; cancelled job does
not continue dispatching new work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from tests.fixtures.generators import build_f01_native

from paperintel.database.models import EvidenceRow, JobRow, SectionRow, TaskRow
from paperintel.errors import DomainError
from paperintel.ingest.service import import_pdf
from paperintel.operations import tracing
from paperintel.schemas.enums import PipelineStage, TaskState
from paperintel.storage.object_store import LocalObjectStore
from paperintel.workflow import engine
from paperintel.workflow.celery_app import run_task_once, submit_task


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def imported(session, store, data_dir, tmp_path, monkeypatch):
    """F01 imported WITHOUT evidence persistence, so the workflow engine
    owns STRUCTURED + EVIDENCE_INDEXED (and, since P07, TRIAGED + ANALYZED:
    the canary evidence unit is seeded so the mock LLM provider's answers
    are fully supported end-to-end)."""
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(
        import_pdf(
            path,
            session=session,
            store=store,
            data_dir=data_dir,
            persist_evidence=False,
        )
    )
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE",
        str(Path(__file__).parents[3] / "config" / "providers.mock.yaml"),
    )
    session.commit()
    return result


def make_job(session, imported) -> JobRow:
    from tests.fixtures.canary import seed_canary_evidence

    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.EXTRACTED,
    )
    plan = engine.plan_job(
        session,
        job,
        data_dir=str(Path(imported.report_path).parents[2]),
        content_sha256=Path(imported.report_path).stem,
        report_path=imported.report_path,
    )
    # The canary evidence is seeded AFTER planning so EVIDENCE_INDEXED is
    # still unsatisfied for the plan (the mock LLM's answers cite it when
    # the ANALYZED task runs).
    seed_canary_evidence(session, imported.paper_version_id)
    session.commit()
    job._plan = plan  # stash for assertions (test-only attribute)
    return job


def run_all_queued(session, job_id: str) -> list[dict]:
    outcomes = []
    while True:
        queued = session.scalars(
            select(TaskRow)
            .where(TaskRow.job_id == job_id, TaskRow.state == TaskState.QUEUED)
            .order_by(TaskRow.created_at)
        ).all()
        if not queued:
            break
        for task in queued:
            outcomes.append(run_task_once(session, task.task_id))
        session.commit()
    return outcomes


def evidence_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(EvidenceRow)) or 0


def test_failed_stage_replay_dispatches_before_dependants_and_recovers_job(session, imported):
    from paperintel.workflow.celery_app import dispatch_candidates

    job = make_job(session, imported)
    original = next(t for t in engine.job_tasks(session, job.job_id) if t.state is TaskState.QUEUED)
    original.max_attempts = 1
    engine.claim_task(session, original.task_id)
    engine.fail_task(session, original.task_id, code="INTERNAL_001")
    session.commit()
    assert job.state is TaskState.FAILED
    assert dispatch_candidates(session) == []
    replay = engine.replay_stage(session, job.job_id, engine.STAGE_FOR_TASK[original.task_type])
    session.commit()
    assert job.state is TaskState.QUEUED
    assert job.finished_at is None
    assert dispatch_candidates(session)[0] == replay.task_id
    assert run_task_once(session, replay.task_id)["outcome"] == "succeeded"
    assert original.state is TaskState.FAILED
    assert job.state is TaskState.RUNNING
    run_all_queued(session, job.job_id)
    assert job.state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS)


def test_analysis_suite_uses_one_loop_and_closes_provider(session, imported, monkeypatch):
    from paperintel.agents import base
    from paperintel.workflow import handlers

    job = make_job(session, imported)
    observed_loops = []
    closed_loops = []
    original_run = base.run_agent
    provider = handlers._resolve_llm_provider()

    async def record_run(*args, **kwargs):
        observed_loops.append(asyncio.get_running_loop())
        return await original_run(*args, **kwargs)

    async def close():
        closed_loops.append(asyncio.get_running_loop())

    monkeypatch.setattr(provider, "aclose", close, raising=False)
    monkeypatch.setattr(handlers, "_resolve_llm_provider", lambda: provider)
    monkeypatch.setattr(base, "run_agent", record_run)
    for task in engine.job_tasks(session, job.job_id):
        if task.state is TaskState.QUEUED:
            run_task_once(session, task.task_id)
        if task.task_type == "agents.run_suite":
            break
    assert len(observed_loops) >= 2
    assert len(closed_loops) == 1
    assert all(loop is closed_loops[0] for loop in observed_loops)
    assert closed_loops[0].is_closed()


@pytest.mark.parametrize("database_error", [False, True])
def test_failed_handler_rolls_back_outputs_but_preserves_attempt(
    session, imported, monkeypatch, database_error
):
    from sqlalchemy import text

    from paperintel.database.models import PaperRow
    from paperintel.workflow import handlers

    job = make_job(session, imported)
    task = next(t for t in engine.job_tasks(session, job.job_id) if t.state is TaskState.QUEUED)
    paper = session.get(PaperRow, imported.paper_id)
    original_title = paper.canonical_title

    def fail_after_write(session, task):
        paper.canonical_title = "partial write must not survive"
        session.flush()
        if database_error:
            session.execute(text("SELECT 1 / 0"))
        raise DomainError("LLM_001", message="Injected failure after flush")

    monkeypatch.setattr(handlers, "run_handler", fail_after_write)
    result = run_task_once(session, task.task_id)
    session.commit()
    session.expire_all()
    assert result["outcome"] == "failed"
    assert session.get(PaperRow, imported.paper_id).canonical_title == original_title
    persisted = session.get(TaskRow, task.task_id)
    assert persisted.attempt == 1
    assert persisted.state is TaskState.QUEUED
    assert persisted.error_code == ("INTERNAL_001" if database_error else "LLM_001")
    assert persisted.next_retry_at is not None


@pytest.mark.parametrize("finish", ["complete", "fail"])
def test_stale_running_task_cannot_overwrite_cancellation(session, imported, finish):
    from sqlalchemy import update

    job = make_job(session, imported)
    task = next(t for t in engine.job_tasks(session, job.job_id) if t.state is TaskState.QUEUED)
    engine.claim_task(session, task.task_id)
    # Simulate a database cancellation with an out-of-date ORM identity map.
    session.execute(update(TaskRow).where(TaskRow.task_id == task.task_id)
                    .values(state=TaskState.CANCELLED).execution_options(synchronize_session=False))
    assert task.state is TaskState.RUNNING
    with pytest.raises(DomainError):
        if finish == "complete":
            engine.complete_task(session, task.task_id, output_manifest={"should_not_persist": True})
        else:
            engine.fail_task(session, task.task_id, code="LLM_001")
    session.commit()
    session.refresh(task)
    assert task.state is TaskState.CANCELLED
    assert not task.output_manifest
    assert task.error_code is None


def test_failed_analysis_run_does_not_satisfy_analysis_stage(session, imported):
    from paperintel.database.models import AnalysisRunRow
    from paperintel.ids import new_run_id
    from paperintel.schemas.common import utcnow

    job = make_job(session, imported)
    session.add(AnalysisRunRow(
        run_id=new_run_id(), paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id, agent_type="agents.method",
        pipeline_version="1.0.0", config_hash="failed-run", model_id="none",
        provider_id="none", status=TaskState.FAILED,
        started_at=utcnow(),
    ))
    session.flush()
    plan = engine.plan_job(session, job, data_dir=str(Path(imported.report_path).parents[2]),
                           content_sha256=Path(imported.report_path).stem, report_path=imported.report_path)
    assert "ANALYZED" not in plan["satisfied"]
    assert "ANALYZED" in {entry["stage"] for entry in plan["enqueued"]}


# ---------------------------------------------------------------------------
# job + plan
# ---------------------------------------------------------------------------


def test_plan_reports_satisfied_skipped_and_enqueued(session, imported) -> None:
    job = make_job(session, imported)
    plan = job._plan
    # Import satisfied: IMPORTED/FINGERPRINTED/PDF_INSPECTED/EXTRACTED.
    for stage in ("IMPORTED", "FINGERPRINTED", "PDF_INSPECTED", "EXTRACTED"):
        assert stage in plan["satisfied"]
    # Implemented + unsatisfied → enqueued.
    enqueued_stages = {entry["stage"] for entry in plan["enqueued"]}
    # Every implemented-but-unsatisfied stage is planned; stage ORDER is
    # guaranteed by the planner emitting them in pipeline order and by the
    # engine's monotonic stage progression (the synthesizer additionally
    # refuses to fabricate: without verified claims it reports
    # INSUFFICIENT_EVIDENCE without a model call).
    assert enqueued_stages == {
        "STRUCTURED",
        "EVIDENCE_INDEXED",
        "TRIAGED",
        "ANALYZED",
        "METADATA_RESOLVED",
        "VERIFIED",
        "SYNTHESIZED",
        "LINKED",
        "SEARCH_INDEXED",
    }
    # Stages owned by later phases get SKIPPED tasks naming the phase
    # (CORPUS_READY is P12 corpus intelligence).
    assert set(plan["skipped"]) == {"CORPUS_READY"}
    skipped = session.scalars(select(TaskRow).where(TaskRow.state == TaskState.SKIPPED)).all()
    assert all(t.module_id.startswith("future:") for t in skipped)
    assert len(skipped) == len(plan["skipped"])


def test_full_job_run_completes_with_warnings(session, imported) -> None:
    """Running all enqueued tasks completes the job as
    SUCCEEDED_WITH_WARNINGS (future-stage skips are visible warnings, never
    unexplained gaps — doc 03 §8)."""
    job = make_job(session, imported)
    outcomes = run_all_queued(session, job.job_id)
    assert all(o["outcome"] == "succeeded" for o in outcomes), outcomes
    assert evidence_count(session) > 0

    session.refresh(job)
    assert job.state is TaskState.SUCCEEDED_WITH_WARNINGS
    progress = engine.job_progress(session, job.job_id)
    assert progress["tasks_by_state"].get("SKIPPED", 0) == 1
    assert (
        progress["tasks_by_state"].get("SUCCEEDED", 0)
        + progress["tasks_by_state"].get("SUCCEEDED_WITH_WARNINGS", 0)
        == 9
    )
    # The pipeline ran through P09 linkage and search indexing.
    assert progress["current_stage"] == PipelineStage.SEARCH_INDEXED.value


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_duplicate_dispatch_never_duplicates_canonical_outputs(session, imported) -> None:
    job = make_job(session, imported)
    task = session.scalars(select(TaskRow).where(TaskRow.task_type == "evidence.persist")).first()
    assert task is not None

    # Duplicate enqueue with the SAME key → same row, no new task.
    again, created = engine.enqueue_task(
        session,
        job,
        task_type=task.task_type,
        module_id=task.task_type,
        input_manifest=dict(task.input_manifest),
        idempotency_key=task.idempotency_key,
    )
    assert created is False
    assert again.task_id == task.task_id
    assert session.scalar(select(func.count()).select_from(TaskRow)) == 10

    # Execute, then re-execute: claim refuses terminal tasks, evidence
    # counts stay stable (canonical outputs never duplicated).
    first = run_task_once(session, task.task_id)
    assert first["outcome"] == "succeeded"
    count_once = evidence_count(session)
    session.commit()
    second = run_task_once(session, task.task_id)
    assert second["claimed"] is False
    assert evidence_count(session) == count_once


def test_replanning_is_idempotent(session, imported) -> None:
    """Re-planning a job (paperctl rerun re-plans) must reuse existing
    tasks — including SKIPPED future-stage tasks — instead of crashing on
    the idempotency key or duplicating rows."""
    job = make_job(session, imported)
    before = session.scalar(select(func.count()).select_from(TaskRow))

    second = engine.plan_job(
        session,
        job,
        data_dir=str(Path(imported.report_path).parents[2]),
        content_sha256=Path(imported.report_path).stem,
        report_path=imported.report_path,
    )
    session.commit()
    assert session.scalar(select(func.count()).select_from(TaskRow)) == before
    assert set(second["skipped"]) == set(job._plan["skipped"])
    # Every stage the second plan enqueues was already planned (no new
    # rows); EVIDENCE_INDEXED is now satisfied because the fixture seeds
    # the canary evidence after the first plan — a real progress signal.
    assert {entry["stage"] for entry in second["enqueued"]} <= {
        entry["stage"] for entry in job._plan["enqueued"]
    }
    assert "EVIDENCE_INDEXED" in second["satisfied"]


# ---------------------------------------------------------------------------
# worker interruption + resume
# ---------------------------------------------------------------------------


def test_worker_interruption_recovery(session, imported) -> None:
    job = make_job(session, imported)
    task = session.scalars(
        select(TaskRow).where(TaskRow.task_type == "structure.reconstruct_sections")
    ).first()
    run_task_once(session, task.task_id)  # structure completes
    session.commit()

    evidence_task = session.scalars(
        select(TaskRow).where(TaskRow.task_type == "evidence.persist")
    ).first()
    engine.claim_task(session, evidence_task.task_id)  # worker starts...
    session.commit()
    # ...and dies: no completion, task left RUNNING.

    requeued = engine.resume_job(session, job.job_id)
    assert [t.task_id for t in requeued] == [evidence_task.task_id]
    session.refresh(evidence_task)
    assert evidence_task.state is TaskState.QUEUED
    assert evidence_task.attempt == 1  # attempt counted, not lost

    outcome = run_task_once(session, evidence_task.task_id)
    assert outcome["outcome"] == "succeeded"
    session.commit()
    # Complete the remaining pipeline (triage + agent suite) so the job
    # reaches its terminal state.
    run_all_queued(session, job.job_id)
    session.refresh(job)
    assert job.state is TaskState.SUCCEEDED_WITH_WARNINGS
    # Recovery is visible in the trace: the interrupted task was claimed
    # twice (original + recovery run) and resumed events were recorded.
    events = tracing.read_trace(session, job.trace_id)
    evidence_claims = [
        e for e in events if e.kind == "task.claimed" and e.task_id == evidence_task.task_id
    ]
    assert len(evidence_claims) == 2
    assert any(e.kind == "task.resumed" for e in events)


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_one_stage_only(session, imported) -> None:
    job = make_job(session, imported)
    run_all_queued(session, job.job_id)
    before = {
        t.task_id: (t.state, t.attempt, t.output_manifest)
        for t in engine.job_tasks(session, job.job_id)
    }

    replayed = engine.replay_stage(session, job.job_id, PipelineStage.STRUCTURED)
    session.commit()
    assert replayed.state is TaskState.QUEUED
    assert replayed.task_type == "structure.reconstruct_sections"
    # Fresh idempotency key (replay ordinal baked in).
    assert replayed.idempotency_key != before

    after = {
        t.task_id: (t.state, t.attempt, t.output_manifest)
        for t in engine.job_tasks(session, job.job_id)
    }
    # Every pre-existing task is UNTOUCHED — exactly one new task exists.
    for task_id, snapshot in before.items():
        assert after[task_id] == snapshot
    assert len(after) == len(before) + 1

    # Running the replay succeeds; the section tree stays stable
    # (idempotent persistence reuses rows).
    sections_before = session.scalar(select(func.count()).select_from(SectionRow))
    outcome = run_task_once(session, replayed.task_id)
    assert outcome["outcome"] == "succeeded"
    sections_after = session.scalar(select(func.count()).select_from(SectionRow))
    assert sections_after == sections_before


def test_replay_unimplemented_stage_is_rejected(session, imported) -> None:
    job = make_job(session, imported)
    with pytest.raises(DomainError) as excinfo:
        engine.replay_stage(session, job.job_id, PipelineStage.ANALYZED)
    assert excinfo.value.code == "CFG_002"


# ---------------------------------------------------------------------------
# transitions + retries
# ---------------------------------------------------------------------------


def test_invalid_state_transitions_rejected(session, imported) -> None:
    job = make_job(session, imported)
    # Deterministic pick: the STRUCTURED task (the first pipeline stage the
    # planner enqueues); an unordered first() could return any stage.
    task = session.scalars(
        select(TaskRow).where(
            TaskRow.state == TaskState.QUEUED,
            TaskRow.task_type == "structure.reconstruct_sections",
        )
    ).first()
    assert task is not None
    run_task_once(session, task.task_id)
    session.commit()
    session.refresh(task)
    assert task.state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS)

    # Terminal → anything is invalid.
    with pytest.raises(DomainError) as excinfo:
        engine.complete_task(session, task.task_id, output_manifest={})
    assert excinfo.value.code == "INTERNAL_002"
    with pytest.raises(DomainError):
        engine.fail_task(session, task.task_id, code="INTERNAL_001")
    session.rollback()
    # Claim refuses terminal tasks (returns None, no exception).
    assert engine.claim_task(session, task.task_id) is None

    # Stage machine is monotonic: the job already sits at STRUCTURED (the
    # completed task advanced it), re-advancing is a no-op, going back is
    # rejected.
    session.refresh(job)
    assert job.current_stage is PipelineStage.STRUCTURED
    engine.advance_stage(session, job.job_id, PipelineStage.STRUCTURED)
    with pytest.raises(DomainError) as stage_exc:
        engine.advance_stage(session, job.job_id, PipelineStage.IMPORTED)
    assert stage_exc.value.code == "INTERNAL_002"
    session.rollback()


def test_retry_exhaustion_marks_task_failed(session, imported, monkeypatch) -> None:
    from paperintel.workflow import handlers as handlers_module

    def _always_fails(session, task):
        raise DomainError("LLM_001", message="simulated provider timeout", details={})

    monkeypatch.setitem(handlers_module.HANDLERS, "evidence.persist", _always_fails)
    job = make_job(session, imported)
    task = session.scalars(select(TaskRow).where(TaskRow.task_type == "evidence.persist")).first()
    assert task.max_attempts == 3

    first = run_task_once(session, task.task_id)
    assert first == {
        "task_id": task.task_id,
        "claimed": True,
        "outcome": "failed",
        "code": "LLM_001",
    }
    session.commit()
    session.refresh(task)
    assert task.state is TaskState.QUEUED  # RETRYING → QUEUED (bounded retry)
    assert task.attempt == 1
    assert task.error_code == "LLM_001"
    first_deadline = task.next_retry_at
    assert first_deadline is not None
    assert run_task_once(session, task.task_id)["claimed"] is False
    assert task.attempt == 1

    # Exhaust the budget.
    monkeypatch.setattr(engine, "utcnow", lambda: first_deadline)
    run_task_once(session, task.task_id)
    session.commit()
    second_deadline = task.next_retry_at
    assert (second_deadline - first_deadline).total_seconds() == 2
    monkeypatch.setattr(engine, "utcnow", lambda: second_deadline)
    run_task_once(session, task.task_id)
    session.commit()
    session.refresh(task)
    assert task.state is TaskState.FAILED
    assert task.attempt == 3
    # A fourth execution cannot even claim the task.
    fourth = run_task_once(session, task.task_id)
    assert fourth["claimed"] is False

    session.refresh(job)
    assert job.state is TaskState.FAILED


def test_cancelled_job_never_dispatches_new_work(session, imported) -> None:
    job = make_job(session, imported)
    engine.cancel_job(session, job.job_id, reason="operator request")
    session.commit()
    session.refresh(job)
    assert job.state is TaskState.CANCELLED

    # Enqueue refuses.
    with pytest.raises(DomainError) as excinfo:
        engine.enqueue_task(
            session,
            job,
            task_type="evidence.persist",
            module_id="evidence.persist",
            input_manifest={},
            idempotency_key="cancelled-test-key",
        )
    assert excinfo.value.code == "INTERNAL_002"
    session.rollback()

    # Resume refuses.
    with pytest.raises(DomainError):
        engine.resume_job(session, job.job_id)
    session.rollback()

    # Queued tasks were cancelled (pre-existing terminal SKIPPED tasks stay
    # skipped — cancellation never rewrites history), and running any of
    # them is impossible.
    states = {t.task_id: t.state for t in engine.job_tasks(session, job.job_id)}
    assert all(state in (TaskState.CANCELLED, TaskState.SKIPPED) for state in states.values())
    assert TaskState.QUEUED not in states.values()
    for task_id in states:
        assert run_task_once(session, task_id)["claimed"] is False


# ---------------------------------------------------------------------------
# trace events + eager celery dispatch
# ---------------------------------------------------------------------------


def test_trace_events_record_lifecycle(session, imported) -> None:
    job = make_job(session, imported)
    run_all_queued(session, job.job_id)
    events = tracing.read_trace(session, job.trace_id)
    kinds = [e.kind for e in events]
    for expected in (
        "job.created",
        "task.enqueued",
        "task.claimed",
        "task.succeeded",
        "task.skipped",
        "job.completed",
    ):
        assert expected in kinds
    # Chronological order.
    times = [e.occurred_at for e in events]
    assert times == sorted(times)


def test_eager_dispatch_runs_task_standalone(tmp_path, test_db_url, monkeypatch) -> None:
    """PAPERINTEL_TASKS_EAGER=1 executes the dispatched task synchronously
    through the same runner path a worker uses (own session + commit).

    Runs on a standalone engine with REAL commits: the savepoint-joined
    fixture session is invisible to other connections by design."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import paperintel.workflow.engine as workflow_engine
    from paperintel.ingest.service import import_pdf

    monkeypatch.setenv("PAPERINTEL_TASKS_EAGER", "1")
    standalone_engine = create_engine(test_db_url)
    factory = sessionmaker(bind=standalone_engine, expire_on_commit=False)
    pdf = tmp_path / "f01.pdf"
    pdf.write_bytes(build_f01_native())
    data_dir = tmp_path / "data"
    with factory() as session:
        imported = asyncio.run(
            import_pdf(
                pdf,
                session=session,
                store=LocalObjectStore(tmp_path / "objects"),
                data_dir=data_dir,
                persist_evidence=False,
            )
        )
        job = workflow_engine.create_job(
            session,
            paper_id=imported.paper_id,
            paper_version_id=imported.paper_version_id,
            current_stage=PipelineStage.EXTRACTED,
        )
        workflow_engine.plan_job(
            session,
            job,
            data_dir=str(data_dir),
            content_sha256=Path(imported.report_path).stem,
            report_path=imported.report_path,
        )
        task = session.scalars(
            select(TaskRow).where(TaskRow.task_type == "structure.reconstruct_sections")
        ).first()
        task_id = task.task_id
        session.commit()

    result = submit_task(task_id, database_url=test_db_url)
    assert result["outcome"] == "succeeded"

    with factory() as verify:
        refreshed = verify.get(TaskRow, task_id)
        assert refreshed.state in (
            TaskState.SUCCEEDED,
            TaskState.SUCCEEDED_WITH_WARNINGS,
        )
        assert refreshed.output_manifest["sections_created"] > 0
    standalone_engine.dispose()

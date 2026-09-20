"""P12 end-to-end scenarios (doc 05 FINAL gate list).

Each test drives a full scenario through the real services:
- mock-provider E2E (import → full pipeline → verified claims → synthesis);
- restart/resume E2E (worker interruption recovery);
- provider degradation E2E (hostile provider → coded failure, no silent pass);
- replay E2E (one stage replayed, canonical output stable);
- audit E2E (provenance chain complete for a verified claim);
- search E2E (scope-locked retrieval);
- GC E2E (dry-run + execute, canonical data preserved).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.fixtures.canary import seed_canary_evidence
from tests.fixtures.generators import build_f01_native

from paperintel.database.models import ClaimRow, TaskRow, VerificationRow
from paperintel.ingest.service import import_pdf
from paperintel.schemas.enums import PipelineStage, ResourceTier, SupportState, TaskState
from paperintel.services import read_models
from paperintel.storage.object_store import LocalObjectStore
from paperintel.triage.service import compute_triage
from paperintel.verification.audit import build_claim_audit_bundle
from paperintel.workflow import engine
from paperintel.workflow.celery_app import run_task_once


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(data_dir: Path) -> LocalObjectStore:
    data_dir.mkdir(parents=True, exist_ok=True)
    return LocalObjectStore(data_dir / "objects")


@pytest.fixture()
def imported(session, store, data_dir, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE",
        str(Path(__file__).parents[3] / "config" / "providers.mock.yaml"),
    )
    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    session.commit()
    return result


def _make_job(session, imported, data_dir: Path, tier: ResourceTier):
    compute_triage(session, paper_id=imported.paper_id, requested_tier=tier)
    seed_canary_evidence(session, imported.paper_version_id)
    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.EVIDENCE_INDEXED,
    )
    plan = engine.plan_job(
        session,
        job,
        data_dir=str(data_dir),
        content_sha256=Path(imported.report_path).stem,
        report_path=imported.report_path,
    )
    session.commit()
    return job, plan


def _run_all(session, job_id: str) -> list[dict]:
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
            if task.next_retry_at is not None:
                import time

                from paperintel.schemas.common import utcnow

                time.sleep(max(0, (task.next_retry_at - utcnow()).total_seconds()))
            outcomes.append(run_task_once(session, task.task_id))
        session.commit()
    return outcomes


# ---------------------------------------------------------------------------
# mock-provider E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_mock_provider_end_to_end(session, imported, data_dir) -> None:
    """The complete pipeline runs on mock providers and produces verified
    claims, a synthesis run, a graph and a search projection."""
    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T3_DEEP)
    outcomes = _run_all(session, job.job_id)
    assert outcomes, "no tasks were planned"
    assert all(outcome["outcome"] == "succeeded" for outcome in outcomes), outcomes

    session.refresh(job)
    assert job.state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS)
    assert job.current_stage is PipelineStage.SEARCH_INDEXED

    claims = session.scalars(
        select(ClaimRow).where(ClaimRow.paper_version_id == imported.paper_version_id)
    ).all()
    assert claims, "agents produced no claims"
    assert any(claim.support_state is SupportState.SUPPORTED for claim in claims)

    verifications = session.scalar(select(func.count()).select_from(VerificationRow))
    assert verifications and verifications > 0

    # Search + context are immediately usable.
    context = read_models.paper_context(session, imported.paper_id)
    assert context["claims"]["total"] >= len(claims)
    search = read_models.search_dispatch(
        session,
        kind="claims",
        query="dataset samples",
        filters={"paper_version_ids": {imported.paper_version_id}},
        limit=5,
    )
    assert search["scope_size"] >= 1


# ---------------------------------------------------------------------------
# restart / resume E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_restart_resume_end_to_end(session, imported, data_dir) -> None:
    """A worker that dies mid-task is recovered by resume: the interrupted
    task is re-queued (attempt preserved) and the job completes."""
    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T2_FULL)
    # The import already satisfied STRUCTURED/EVIDENCE_INDEXED, so the
    # first QUEUED task is a later stage (triage/analysis): simulate a
    # worker dying while it runs.
    interrupted = session.scalars(
        select(TaskRow)
        .where(TaskRow.job_id == job.job_id, TaskRow.state == TaskState.QUEUED)
        .order_by(TaskRow.created_at)
    ).first()
    assert interrupted is not None

    engine.claim_task(session, interrupted.task_id)  # worker starts…
    session.commit()  # …and dies without completing
    session.refresh(interrupted)
    assert interrupted.state is TaskState.RUNNING

    requeued = engine.resume_job(session, job.job_id)
    assert interrupted.task_id in [task.task_id for task in requeued]
    session.refresh(interrupted)
    assert interrupted.state is TaskState.QUEUED
    assert interrupted.attempt >= 1

    outcomes = _run_all(session, job.job_id)
    assert all(outcome["outcome"] == "succeeded" for outcome in outcomes)
    session.refresh(job)
    assert job.state in (TaskState.SUCCEEDED, TaskState.SUCCEEDED_WITH_WARNINGS)


# ---------------------------------------------------------------------------
# provider degradation E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_provider_degradation_end_to_end(
    session, imported, data_dir, tmp_path, monkeypatch
) -> None:
    """A hostile provider must produce a CODED failure — the pipeline never
    reports success on a hallucinating provider."""
    hostile = tmp_path / "providers_hostile.yaml"
    hostile.write_text(
        'spec_version: "1.0.0"\n'
        "providers:\n"
        "  hostile_llm:\n"
        "    kind: mock_llm\n"
        "    options:\n"
        '      mode: "NON_JSON"\n'
        "      seed: 7\n"
    )
    monkeypatch.setenv("PAPERINTEL_PROVIDERS_FILE", str(hostile))
    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()

    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T2_FULL)
    outcomes = _run_all(session, job.job_id)
    failures = [outcome for outcome in outcomes if outcome["outcome"] != "succeeded"]
    assert failures, "a hallucinating provider must fail the analysis task"
    codes = {failure.get("code") for failure in failures}
    # The firewall rejects non-JSON output (LLM_003); retries exhaust into a
    # coded FAILED task. Nothing is accepted, nothing is silently skipped.
    assert codes <= {
        "LLM_003",
        "LLM_004",
        "LLM_005",
        "EVIDENCE_001",
        "EVIDENCE_002",
        "CLAIM_001",
    }, codes
    assert "LLM_003" in codes

    session.refresh(job)
    assert job.state is TaskState.FAILED
    # The failure is recorded on the task, not swallowed.
    failed_task = session.scalars(
        select(TaskRow).where(TaskRow.job_id == job.job_id, TaskRow.state == TaskState.FAILED)
    ).first()
    assert failed_task is not None and failed_task.error_code
    reset_settings_cache()


# ---------------------------------------------------------------------------
# replay E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_replay_end_to_end(session, imported, data_dir) -> None:
    """Replaying one stage creates exactly one new task and leaves the
    canonical outputs stable."""
    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T2_FULL)
    _run_all(session, job.job_id)

    evidence_before = session.scalar(
        select(func.count())
        .select_from(ClaimRow)
        .where(ClaimRow.paper_version_id == imported.paper_version_id)
    )
    before = {
        task.task_id: (task.state, task.attempt) for task in engine.job_tasks(session, job.job_id)
    }

    replayed = engine.replay_stage(
        session,
        job.job_id,
        PipelineStage.STRUCTURED,
        input_manifest=engine.build_base_manifest(
            paper_version_id=imported.paper_version_id,
            content_sha256=Path(imported.report_path).stem,
            report_path=imported.report_path,
            data_dir=str(data_dir),
        ),
    )
    session.commit()
    assert replayed.task_id not in before

    after = {
        task.task_id: (task.state, task.attempt) for task in engine.job_tasks(session, job.job_id)
    }
    for task_id, snapshot in before.items():
        assert after[task_id] == snapshot, "replay must not rewrite history"

    outcomes = _run_all(session, job.job_id)
    for outcome in outcomes:
        if outcome["outcome"] != "succeeded":
            row = session.get(TaskRow, outcome["task_id"])
            print(
                "DBG2:",
                row.task_type,
                row.state.value,
                row.error_code,
                str(row.error_details)[:300],
                str(row.output_manifest)[:200],
            )
    assert all(outcome["outcome"] == "succeeded" for outcome in outcomes)
    evidence_after = session.scalar(
        select(func.count())
        .select_from(ClaimRow)
        .where(ClaimRow.paper_version_id == imported.paper_version_id)
    )
    assert evidence_after == evidence_before


# ---------------------------------------------------------------------------
# audit E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_audit_end_to_end(session, imported, data_dir) -> None:
    """Every verified claim has a complete provenance chain: creating run,
    model calls, evidence links and verifier verdicts."""
    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T3_DEEP)
    _run_all(session, job.job_id)

    claims = session.scalars(
        select(ClaimRow).where(ClaimRow.paper_version_id == imported.paper_version_id)
    ).all()
    verified = [
        claim
        for claim in claims
        if claim.support_state
        in (SupportState.SUPPORTED, SupportState.PARTIALLY_SUPPORTED, SupportState.DISPUTED)
    ]
    assert verified, "expected verified claims"

    for claim in verified[:5]:
        bundle = build_claim_audit_bundle(session, claim.claim_id)
        assert bundle.created_by_run is not None
        assert bundle.evidence or bundle.external_provenance, (
            f"{claim.claim_id} has no evidence or external provenance"
        )
        assert bundle.verifications, f"{claim.claim_id} has no verifications"
        assert bundle.pipeline_version
        assert bundle.complete is True, (claim.claim_id, bundle.warnings)

    overview = read_models.paper_audit(session, imported.paper_id)
    assert overview["claim_count"] == len(claims)
    # Synthesis runs AFTER verification, so its own output claims are new and
    # unverified: they are reported as incomplete, with that exact reason —
    # never hidden. Every other claim must have a complete chain.
    incomplete = set(overview["incomplete_bundles"])
    for claim_id in incomplete:
        bundle = build_claim_audit_bundle(session, claim_id)
        assert any("not been verified" in warning for warning in bundle.warnings), (
            claim_id,
            bundle.warnings,
        )
    assert len(incomplete) <= 5


# ---------------------------------------------------------------------------
# search E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_search_end_to_end(session, imported, data_dir) -> None:
    """After indexing, retrieval returns scoped hits through every channel
    and cannot leak outside the requested scope."""
    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T2_FULL)
    _run_all(session, job.job_id)

    response = read_models.search_dispatch(
        session,
        kind="evidence",
        query="dataset",
        filters={"paper_version_ids": {imported.paper_version_id}},
        limit=10,
    )
    assert response["scope_size"] >= 1
    assert response["hits"]
    assert all(
        hit["paper_version_id"] == imported.paper_version_id
        for hit in response["hits"]
        if hit["document_type"] == "EVIDENCE"
    )

    empty_scope = read_models.search_dispatch(
        session,
        kind="evidence",
        query="dataset",
        filters={"paper_ids": {"pap_01UNKNOWN000000000000000000"}},
        limit=10,
    )
    assert empty_scope["hits"] == []
    assert empty_scope["scope_size"] == 0


# ---------------------------------------------------------------------------
# GC E2E
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_gc_end_to_end(session, imported, data_dir) -> None:
    """GC dry-run reports without deleting; execute removes only
    non-canonical objects and never touches the paper's PDF or assets."""
    from paperintel.database.models import AssetRow
    from paperintel.operations.gc import run_gc

    assets_before = {asset.asset_id for asset in session.scalars(select(AssetRow))}
    assert assets_before

    pdf_assets = [
        data_dir / "objects" / asset.storage_key for asset in session.scalars(select(AssetRow))
    ]
    existing_pdfs = [path for path in pdf_assets if path.exists()]

    # Create an expired temporary file so GC has real work to report.
    import os
    import time

    stale = data_dir / "tmp" / "expired-render.png"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"x" * 512)
    old = time.time() - 72 * 3600
    os.utime(stale, (old, old))

    dry = run_gc(session, data_dir, dry_run=True)
    assert any("expired-render.png" in candidate.object_path for candidate in dry.candidates)
    assert stale.exists()
    assert dry.reclaimed_bytes >= 512

    executed = run_gc(session, data_dir, dry_run=False)
    assert not stale.exists()
    assert executed.errors == []

    assets_after = {asset.asset_id for asset in session.scalars(select(AssetRow))}
    assert assets_after == assets_before
    assert all(path.exists() for path in existing_pdfs)


@pytest.mark.needs_db
def test_e2e_json_summary_is_serialisable(session, imported, data_dir) -> None:
    """Every surface payload for a finished job serialises to JSON (the
    contract every external client depends on)."""
    job, _plan = _make_job(session, imported, data_dir, ResourceTier.T2_FULL)
    _run_all(session, job.job_id)

    payloads = [
        read_models.system_status(session),
        read_models.paper_context(session, imported.paper_id),
        read_models.paper_analysis(session, imported.paper_id),
        read_models.paper_audit(session, imported.paper_id),
        read_models.job_detail(session, job.job_id),
    ]
    for payload in payloads:
        encoded = json.dumps(payload, default=str)
        assert encoded

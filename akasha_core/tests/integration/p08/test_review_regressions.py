"""Behavioral regressions for review fixes, against a disposable PostgreSQL DB."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from paperintel.database.models import ClaimRow, ExternalProvenanceRow, TaskRow
from paperintel.knowledge.external import persist_metadata
from paperintel.providers.base import MetadataRecord
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import ResourceTier, TaskState
from paperintel.verification.audit import build_version_audit_bundle
from paperintel.verification.service import run_verification
from paperintel.workflow import engine

pytestmark = pytest.mark.needs_db


def test_external_metadata_is_idempotent_verifiable_and_auditable(session, imported):
    record = MetadataRecord(
        provider="test-metadata",
        identifier="10.1234/test",
        data={"title": "A study", "year": 2026},
        retrieved_at=utcnow(),
    )
    args = dict(paper_id=imported.paper_id, paper_version_id=imported.paper_version_id)
    ids = persist_metadata(session, record, **args)
    assert len(ids) == 2
    assert persist_metadata(session, record, **args) == ids
    assert session.scalar(select(func.count()).select_from(ExternalProvenanceRow)) == 2
    report = run_verification(
        session,
        paper_version_id=imported.paper_version_id,
        tier=ResourceTier.T2_FULL,
        claim_ids=ids,
    )
    assert report.states == {"SUPPORTED": 2}
    audit = build_version_audit_bundle(session, imported.paper_version_id)
    assert {c["claim_id"] for c in audit["external_inferences"]} == set(ids)
    claim = session.get(ClaimRow, ids[0])
    claim.statement = 'title: "fabricated title"'
    session.flush()
    report = run_verification(
        session,
        paper_version_id=imported.paper_version_id,
        tier=ResourceTier.T2_FULL,
        claim_ids=[claim.claim_id],
    )
    assert report.states == {"UNSUPPORTED": 1}


def test_eta_requires_history_then_uses_observed_durations(session, imported):
    job = engine.create_job(
        session, paper_id=imported.paper_id, paper_version_id=imported.paper_version_id
    )
    task, _ = engine.enqueue_task(
        session,
        job,
        task_type="review.eta",
        module_id="review",
        input_manifest={},
        idempotency_key="eta-pending",
    )
    assert engine.job_progress(session, job.job_id)["eta_p50"] is None
    for index, seconds in enumerate((10, 20, 30, 40, 50)):
        peer, _ = engine.enqueue_task(
            session,
            job,
            task_type="review.eta",
            module_id="review",
            input_manifest={},
            idempotency_key=f"eta-peer-{index}",
        )
        peer.state = TaskState.SUCCEEDED
        peer.finished_at = utcnow() - timedelta(minutes=1)
        peer.started_at = peer.finished_at - timedelta(seconds=seconds)
    session.flush()
    progress = engine.job_progress(session, job.job_id)
    assert progress["eta_p50"] == 30 and progress["eta_p90"] == 50
    session.get(TaskRow, task.task_id).state = TaskState.BLOCKED
    assert engine.job_progress(session, job.job_id)["eta_p50"] is None


@pytest.mark.parametrize(
    "mode,verdict,count",
    [("related", "WARN", 1), ("empty", "INCONCLUSIVE", 0), ("failed", "INCONCLUSIVE", 0)],
)
def test_novelty_retrieval_retains_sources_without_proving_novelty(
    session, imported, monkeypatch, mode, verdict, count
):
    from types import SimpleNamespace

    from paperintel.errors import DomainError
    from paperintel.schemas.enums import ClaimType
    from paperintel.verification import novelty

    async def search(title, limit):
        if mode == "failed":
            raise DomainError("PROVIDER_001", message="offline")
        if mode == "empty":
            return []
        return [
            MetadataRecord(
                "catalog",
                "work-123",
                {"title": "Spectral graph embeddings", "year": 2020},
                utcnow(),
                "https://example.org/work-123",
                "a" * 64,
            )
        ]

    provider = SimpleNamespace(provider_id="catalog", search_by_title=search)
    monkeypatch.setattr(
        novelty,
        "build_registry",
        lambda _: SimpleNamespace(by_family=lambda _: {"catalog": provider}),
    )
    claim = SimpleNamespace(
        paper_id=imported.paper_id,
        claim_type=ClaimType.FACT,
        statement="First spectral graph embeddings",
    )
    result = novelty.audit_novelty(session, claim)
    assert result.verdict == verdict
    assert len(result.details["related_work_candidates"]) == count
    if count:
        source = result.details["related_work_candidates"][0]
        assert source["source_identifier"] == "work-123" and source["content_hash"] == "a" * 64
    if mode == "failed":
        assert result.details["provider_failures"] == [
            {"provider": "catalog", "error_code": "PROVIDER_001"}
        ]


def test_dispatch_recovers_due_work_prioritizes_t3_and_serializes_stages(session, imported):
    from paperintel.workflow.celery_app import dispatch_candidates

    tasks = []
    for index, tier in enumerate((ResourceTier.T1_SCAN, ResourceTier.T3_DEEP)):
        job = engine.create_job(
            session,
            paper_id=imported.paper_id,
            paper_version_id=imported.paper_version_id,
            requested_tier=tier,
        )
        for stage in range(2):
            task, _ = engine.enqueue_task(
                session,
                job,
                task_type=f"review.{stage}",
                module_id="review",
                input_manifest={},
                idempotency_key=f"dispatch-{index}-{stage}",
            )
            tasks.append(task)
    assert dispatch_candidates(session) == [tasks[2].task_id, tasks[0].task_id]
    tasks[2].next_retry_at = utcnow() + timedelta(seconds=60)
    session.flush()
    assert dispatch_candidates(session) == [tasks[0].task_id]
    tasks[2].next_retry_at = utcnow() - timedelta(seconds=1)
    tasks[0].state = TaskState.RUNNING
    session.flush()
    assert dispatch_candidates(session) == [tasks[2].task_id]

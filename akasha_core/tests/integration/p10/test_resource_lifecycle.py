"""P10 tests: tier budget policy, low-disk protection, GC and dedup.

Doc 05 P10 gate list:
- T0 does not schedule T3-only work;
- pinned paper overrides auto tier;
- low disk prevents nonessential expansion;
- GC never removes canonical evidence/PDF referenced by active records.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.generators import build_f01_native

from paperintel.config.settings import AppConfig, DiskPolicySettings
from paperintel.database.models import AssetRow, TaskRow
from paperintel.errors import DomainError
from paperintel.ingest.service import import_pdf
from paperintel.operations.disk import (
    directory_bytes,
    disk_report,
    disk_status,
    require_capacity,
)
from paperintel.operations.gc import content_dedup_report, find_candidates, run_gc
from paperintel.schemas.enums import PipelineStage, ResourceTier, RetentionClass, TaskState
from paperintel.storage.object_store import LocalObjectStore
from paperintel.triage.budget import (
    agent_allowed,
    budget_for_tier,
    minimum_tier_for_stage,
    stage_allowed,
)
from paperintel.triage.service import compute_triage
from paperintel.workflow import engine


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def store(data_dir: Path) -> LocalObjectStore:
    """Object store rooted INSIDE data_dir, exactly like production
    (LocalObjectStore(data_dir/"objects")) so the disk lifecycle sees the
    real layout."""
    data_dir.mkdir(parents=True, exist_ok=True)
    return LocalObjectStore(data_dir / "objects")


@pytest.fixture()
def imported(session, store, data_dir, tmp_path):
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    session.commit()
    return result


def _plan(session, imported, data_dir):
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


# ---------------------------------------------------------------------------
# tier budget policy
# ---------------------------------------------------------------------------


def test_stage_ladder_is_monotonic() -> None:
    """Every stage has a minimum tier, and T3 allows everything."""
    for stage in PipelineStage:
        minimum = minimum_tier_for_stage(stage)
        assert minimum in ResourceTier
        assert stage_allowed(ResourceTier.T3_DEEP, stage)
    assert not stage_allowed(ResourceTier.T0_INDEX, PipelineStage.ANALYZED)
    assert not stage_allowed(ResourceTier.T0_INDEX, PipelineStage.VERIFIED)
    assert not stage_allowed(ResourceTier.T0_INDEX, PipelineStage.SYNTHESIZED)
    assert not stage_allowed(ResourceTier.T0_INDEX, PipelineStage.LINKED)
    # T0 still indexes for search (identity/metadata must stay findable).
    assert stage_allowed(ResourceTier.T0_INDEX, PipelineStage.SEARCH_INDEXED)
    # T1 adds the overview analysis, not verification.
    assert stage_allowed(ResourceTier.T1_SCAN, PipelineStage.ANALYZED)
    assert not stage_allowed(ResourceTier.T1_SCAN, PipelineStage.VERIFIED)


def test_agent_budget_by_tier() -> None:
    t0 = budget_for_tier(ResourceTier.T0_INDEX)
    assert t0.agent_types == ()
    assert not agent_allowed(ResourceTier.T0_INDEX, "agents.method")

    t1 = budget_for_tier(ResourceTier.T1_SCAN)
    assert agent_allowed(ResourceTier.T1_SCAN, "agents.research_question")
    assert agent_allowed(ResourceTier.T1_SCAN, "agents.method")
    assert not agent_allowed(ResourceTier.T1_SCAN, "agents.critic")
    assert t1.max_claims < budget_for_tier(ResourceTier.T2_FULL).max_claims

    # T2/T3 allow the full registered suite.
    assert agent_allowed(ResourceTier.T2_FULL, "agents.critic")
    assert agent_allowed(ResourceTier.T3_DEEP, "agents.synthesizer")


@pytest.mark.needs_db
def test_t0_does_not_schedule_t3_only_work(session, imported, data_dir) -> None:
    """Doc 05 P10: at T0 the deep stages are SKIPPED with an explicit
    tier reason — not silently missing, and never enqueued."""
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T0_INDEX)
    session.flush()
    _job, plan = _plan(session, imported, data_dir)

    assert plan["effective_tier"] == "T0_INDEX"
    enqueued = {entry["stage"] for entry in plan["enqueued"]}
    assert "ANALYZED" not in enqueued
    assert "VERIFIED" not in enqueued
    assert "SYNTHESIZED" not in enqueued
    assert "LINKED" not in enqueued
    # T0 scope IS planned (search index is T0 work).
    assert "SEARCH_INDEXED" in enqueued or "SEARCH_INDEXED" in plan["satisfied"]

    skipped_tasks = session.scalars(
        select(TaskRow).where(
            TaskRow.state == TaskState.SKIPPED, TaskRow.task_type == "stage.analyzed"
        )
    ).all()
    assert skipped_tasks
    assert skipped_tasks[0].module_id == "tier:T0_INDEX"
    assert "requires T1_SCAN" in (skipped_tasks[0].output_manifest or {}).get(
        "reason", ""
    ) or skipped_tasks[0].module_id.startswith("tier:")


@pytest.mark.needs_db
def test_t1_schedules_overview_but_not_verification(session, imported, data_dir) -> None:
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T1_SCAN)
    session.flush()
    _job, plan = _plan(session, imported, data_dir)
    enqueued = {entry["stage"] for entry in plan["enqueued"]}
    assert "ANALYZED" in enqueued
    assert "VERIFIED" not in enqueued
    assert "SYNTHESIZED" not in enqueued


@pytest.mark.needs_db
def test_t3_schedules_everything(session, imported, data_dir) -> None:
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T3_DEEP)
    session.flush()
    _job, plan = _plan(session, imported, data_dir)
    enqueued = {entry["stage"] for entry in plan["enqueued"]}
    assert {"ANALYZED", "VERIFIED", "SYNTHESIZED", "LINKED"} <= enqueued
    # T3 runs the complete ladder; only CORPUS_READY (P12) remains
    # future-owned at this build scope.
    assert plan["skipped"] == ["CORPUS_READY"]


@pytest.mark.needs_db
def test_pinned_paper_overrides_auto_tier(session, imported, data_dir) -> None:
    """Doc 05 P10: a pinned paper is analyzed at full depth regardless of
    the requested tier — manual override always wins (doc 02 §11)."""
    from paperintel.knowledge.collections import add_paper, create_collection

    collection = create_collection(session, name="pinned project")
    add_paper(
        session,
        collection_id=collection.collection_id,
        paper_id=imported.paper_id,
        pinned=True,
    )
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T0_INDEX)
    session.flush()
    _job, plan = _plan(session, imported, data_dir)
    assert plan["effective_tier"] == ResourceTier.T2_FULL.value
    assert "ANALYZED" in {entry["stage"] for entry in plan["enqueued"]}


# ---------------------------------------------------------------------------
# low-disk protection
# ---------------------------------------------------------------------------


def test_disk_status_classification(tmp_path) -> None:
    # A tiny critical threshold derived from the real filesystem state.
    status = disk_status(tmp_path)
    assert status.level in ("OK", "WARNING", "CRITICAL")
    assert 0.0 <= status.free_percent <= 100.0
    assert status.total_bytes > 0

    strict = AppConfig(
        disk=DiskPolicySettings(warning_free_percent=99.99, critical_free_percent=99.98)
    )
    critical = disk_status(tmp_path, settings=strict)
    assert critical.level == "CRITICAL"
    assert critical.reasons


def test_low_disk_blocks_nonessential_expansion(tmp_path) -> None:
    """Doc 05 P10: low disk prevents nonessential expansion (RESOURCE_001);
    essential operations keep working."""
    strict = AppConfig(
        disk=DiskPolicySettings(warning_free_percent=99.99, critical_free_percent=99.98)
    )
    with pytest.raises(DomainError) as excinfo:
        require_capacity(
            tier=ResourceTier.T3_DEEP,
            operation="deep expansion",
            path=tmp_path,
            settings=strict,
        )
    assert excinfo.value.code == "RESOURCE_001"
    assert excinfo.value.details["level"] == "CRITICAL"

    # Bulk temporary rendering is likewise blocked.
    with pytest.raises(DomainError) as excinfo:
        require_capacity(
            needed_bytes=10 * 1024 * 1024,
            operation="page rendering",
            path=tmp_path,
            settings=strict,
        )
    assert excinfo.value.code == "RESOURCE_001"

    # A status-only operation on a critical disk does NOT raise (minimal
    # metadata/status operations must keep working).
    status = require_capacity(operation="status", path=tmp_path, settings=strict)
    assert status.level == "CRITICAL"


@pytest.mark.needs_db
def test_critical_disk_blocks_t3_planning(session, imported, data_dir, monkeypatch) -> None:
    """Planning a T3 paper on a critical disk raises RESOURCE_001 rather
    than starting deep work that cannot finish."""
    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T3_DEEP)
    session.flush()

    from paperintel.config.settings import get_settings

    settings = get_settings()
    strict = settings.model_copy(
        update={"disk": DiskPolicySettings(warning_free_percent=99.99, critical_free_percent=99.98)}
    )
    monkeypatch.setattr("paperintel.workflow.engine.get_settings", lambda: strict, raising=False)
    # engine reads disk status through paperintel.operations.disk.disk_status
    # which uses get_settings(); patch that module's symbol instead.
    monkeypatch.setattr("paperintel.operations.disk.get_settings", lambda: strict, raising=False)

    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.EVIDENCE_INDEXED,
    )
    with pytest.raises(DomainError) as excinfo:
        engine.plan_job(
            session,
            job,
            data_dir=str(data_dir),
            content_sha256=Path(imported.report_path).stem,
            report_path=imported.report_path,
        )
    assert excinfo.value.code == "RESOURCE_001"


# ---------------------------------------------------------------------------
# GC + dedup
# ---------------------------------------------------------------------------


def _touch(path: Path, content: bytes, *, age_hours: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if age_hours:
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))
    return path


@pytest.mark.needs_db
def test_gc_dry_run_lists_without_deleting(session, data_dir) -> None:
    temp_file = _touch(data_dir / "tmp" / "page-1.png", b"x" * 512, age_hours=48)
    debug_file = _touch(data_dir / "debug" / "raw-response.json", b"y" * 256, age_hours=24 * 30)
    fresh_temp = _touch(data_dir / "tmp" / "fresh.png", b"z" * 128, age_hours=0)

    report = run_gc(session, data_dir, dry_run=True)
    paths = {candidate.object_path for candidate in report.candidates}
    assert str(temp_file) in paths
    assert str(debug_file) in paths
    assert str(fresh_temp) not in paths  # not expired yet
    assert report.reclaimed_bytes > 0
    # Dry run deletes NOTHING.
    assert temp_file.exists() and debug_file.exists() and fresh_temp.exists()
    assert report.removed == []
    # Every candidate reports the doc 07 §7 fields.
    for candidate in report.candidates:
        assert candidate.size_bytes > 0
        assert candidate.retention_class in {entry.value for entry in RetentionClass}
        assert candidate.reason
        assert candidate.last_reference


@pytest.mark.needs_db
def test_gc_execution_removes_only_candidates(session, data_dir) -> None:
    temp_file = _touch(data_dir / "tmp" / "old.png", b"x" * 1024, age_hours=72)
    keep_file = _touch(data_dir / "objects" / "canonical.pdf", b"k" * 2048)

    report = run_gc(session, data_dir, dry_run=False)
    assert not temp_file.exists()
    assert keep_file.exists()  # canonical object untouched
    assert report.reclaimed_bytes >= 1024
    assert report.errors == []


@pytest.mark.needs_db
def test_gc_never_removes_canonical_evidence_or_pdf(session, imported, data_dir) -> None:
    """Doc 05 P10: GC never removes canonical evidence/PDF referenced by
    active records."""
    assets = session.scalars(select(AssetRow)).all()
    assert assets, "import should have produced assets"
    for asset in assets:
        asset.retention_class = RetentionClass.KEEP
    session.flush()

    candidates, protected = find_candidates(session, data_dir)
    protected_ids = {entry.last_reference for entry in protected}
    assert any(asset.asset_id in protected_ids for asset in assets)
    candidate_ids = {entry.last_reference for entry in candidates}
    assert not (protected_ids & candidate_ids)

    report = run_gc(session, data_dir, dry_run=False)
    surviving = {asset.asset_id for asset in session.scalars(select(AssetRow))}
    assert {asset.asset_id for asset in assets} <= surviving
    assert report.errors == []


@pytest.mark.needs_db
def test_gc_reports_non_canonical_assets_as_candidates(session, imported, data_dir) -> None:
    assets = session.scalars(select(AssetRow)).all()
    first = assets[0]
    first.retention_class = RetentionClass.TEMP
    session.flush()

    candidates, protected = find_candidates(session, data_dir)
    reasons = {candidate.last_reference: candidate.reason for candidate in candidates}
    assert first.asset_id in reasons
    assert "TEMP" in reasons[first.asset_id]


# ---------------------------------------------------------------------------
# disk breakdown + dedup reporting
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_disk_report_breaks_down_by_retention_class(session, data_dir) -> None:
    _touch(data_dir / "objects" / "a.pdf", b"a" * 100)
    _touch(data_dir / "cache" / "report.json", b"b" * 200)
    _touch(data_dir / "tmp" / "c.png", b"c" * 300)
    _touch(data_dir / "debug" / "d.json", b"d" * 400)

    report = disk_report(data_dir)
    by_class = {entry["retention_class"]: entry["bytes"] for entry in report["by_retention_class"]}
    assert by_class["KEEP"] == 100
    assert by_class["CACHE"] == 200
    assert by_class["TEMP"] == 300
    assert by_class["DEBUG_TTL"] == 400
    assert report["store_bytes"] == 1000
    assert report["disk"]["level"] in ("OK", "WARNING", "CRITICAL")
    assert report["policy"]["critical_free_percent"] >= 0


@pytest.mark.needs_db
def test_content_dedup_report_detects_stray_duplicates(session, imported, data_dir) -> None:
    assets = session.scalars(select(AssetRow)).all()
    assert assets
    canonical_dir = data_dir / "objects"
    canonical_dir.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        target = canonical_dir / asset.storage_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"canonical")

    clean = content_dedup_report(session, data_dir)
    assert clean["duplicate_bytes"] == 0
    assert clean["dedup_effective"] is True

    _touch(canonical_dir / "stray-copy.bin", b"duplicate")
    dirty = content_dedup_report(session, data_dir)
    assert dirty["duplicate_bytes"] > 0
    assert dirty["dedup_effective"] is False
    assert dirty["duplicate_objects"][0]["path"].endswith("stray-copy.bin")


def test_directory_bytes_counts_files(tmp_path) -> None:
    _touch(tmp_path / "sub" / "one.bin", b"1" * 50)
    _touch(tmp_path / "two.bin", b"2" * 25)
    assert directory_bytes(tmp_path) == 75
    assert directory_bytes(tmp_path / "missing") == 0

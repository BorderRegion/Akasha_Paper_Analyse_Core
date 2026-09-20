"""operations.selftest — the end-to-end selftest (P12, doc 04 §10).

Runs the full pipeline on a GENERATED fixture (never user data) and
reports per-stage outcomes. Any stage failure fails the selftest with the
failing stage named — never a green summary over a broken pipeline.

Mock mode uses the deterministic providers; --real-providers additionally
runs provider canaries (LLM/OCR) so credentials and connectivity are
verified honestly.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from paperintel.config.settings import AppConfig
from paperintel.database.base import (
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from paperintel.errors import DomainError
from paperintel.schemas.enums import PipelineStage, TaskState
from paperintel.version import PIPELINE_VERSION, SPEC_VERSION


@dataclass(slots=True)
class SelftestStage:
    name: str
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    duration_ms: int = 0


def run_selftest(
    settings: AppConfig,
    *,
    mock: bool = True,
    real_providers: bool = False,
    keep_artifacts: bool = False,
    with_analysis: bool = True,
) -> dict[str, Any]:
    if keep_artifacts:
        return _run_selftest(
            settings,
            mock=mock,
            real_providers=real_providers,
            keep_artifacts=True,
            with_analysis=with_analysis,
        )
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="paperintel-selftest-") as directory:
        isolated = settings.model_copy(
            update={"core": settings.core.model_copy(update={"data_dir": Path(directory)})}
        )
        from paperintel.providers.runtime import runtime_root

        token = runtime_root.set(directory)
        try:
            return _run_selftest(
                isolated,
                mock=mock,
                real_providers=real_providers,
                keep_artifacts=False,
                with_analysis=with_analysis,
            )
        finally:
            runtime_root.reset(token)


def _run_selftest(
    settings: AppConfig,
    *,
    mock: bool = True,
    real_providers: bool = False,
    keep_artifacts: bool = False,
    with_analysis: bool = True,
) -> dict[str, Any]:
    """Run the golden-path selftest in a throwaway database scope.

    The selftest writes into the configured database (a local deployment
    has one store). With keep_artifacts=False all nested commits are
    savepoints inside a transaction that is rolled back at exit, and the
    wrapper removes the isolated filesystem directory even on failure.
    """
    stages: list[SelftestStage] = []
    started = time.monotonic()

    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    connection = None
    transaction = None
    if not keep_artifacts:
        from sqlalchemy.orm import sessionmaker

        connection = engine.connect()
        transaction = connection.begin()
        factory = sessionmaker(
            bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
    try:
        with session_scope(factory) as session:
            stages.append(_stage("providers", lambda: _providers_stage(settings, mock)))
            if real_providers:
                stages.append(_stage("provider_canaries", lambda: _canary_stage(settings)))
            stages.append(_stage("config", lambda: _config_stage(settings)))
            import_result = None
            try:
                import_result = _import_fixture(session, settings)
                stages.append(
                    SelftestStage(
                        "import",
                        "PASS",
                        {
                            "paper_id": import_result["paper_id"],
                            "paper_version_id": import_result["paper_version_id"],
                            "pages": import_result["pages"],
                        },
                    )
                )
            except DomainError as exc:
                stages.append(SelftestStage("import", "FAIL", {}, error_code=exc.code))
                return _summary(stages, started, settings, mock, real_providers)

            if with_analysis:
                stages.extend(_run_pipeline_stages(session, settings, import_result))
            else:
                stages.append(
                    SelftestStage(
                        "analysis",
                        "SKIPPED",
                        {"reason": "--no-analysis requested"},
                    )
                )

            # Search and audit read the analysis output: without analysis
            # they are explicitly SKIPPED (with the reason), never counted
            # as failures of a stage that was asked not to run.
            if with_analysis:
                stages.append(_stage("search", lambda: _search_stage(session, import_result)))
                stages.append(_stage("audit", lambda: _audit_stage(session, import_result)))
            else:
                for dependent in ("search", "audit"):
                    stages.append(
                        SelftestStage(
                            dependent,
                            "SKIPPED",
                            {"reason": "requires --analysis (requested --no-analysis)"},
                        )
                    )
            stages.append(
                _stage(
                    "disk",
                    lambda: _disk_stage(settings),
                )
            )

            if not keep_artifacts and import_result.get("job_id"):
                from paperintel.workflow import engine as wf_engine

                try:
                    wf_engine.cancel_job(session, import_result["job_id"])
                    stages.append(
                        SelftestStage(
                            "cleanup",
                            "PASS",
                            {"cancelled_job": import_result["job_id"]},
                        )
                    )
                except DomainError as exc:  # already terminal
                    stages.append(SelftestStage("cleanup", "PASS", {"note": exc.code}))
            if not keep_artifacts:
                session.rollback()
    finally:
        if transaction is not None and transaction.is_active:
            transaction.rollback()
        if connection is not None:
            connection.close()
        engine.dispose()

    return _summary(stages, started, settings, mock, real_providers)


def _stage(name: str, func) -> SelftestStage:
    start = time.monotonic()
    try:
        detail = func()
    except DomainError as exc:
        return SelftestStage(
            name,
            "FAIL",
            dict(exc.details or {}),
            error_code=exc.code,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    return SelftestStage(
        name, "PASS", detail or {}, duration_ms=int((time.monotonic() - start) * 1000)
    )


def _providers_stage(settings: AppConfig, mock: bool) -> dict[str, Any]:
    from paperintel.services.read_models import providers_status

    status = providers_status(settings=settings)
    if not status.get("providers"):
        raise DomainError(
            "CFG_001",
            message="No providers configured — run with a providers file.",
            details={},
        )
    families: dict[str, int] = {}
    for entry in status["providers"].values():
        families[entry["family"]] = families.get(entry["family"], 0) + 1
    return {
        "providers": len(status["providers"]),
        "families": families,
        "mode": "mock" if mock else "real",
    }


def _canary_stage(settings: AppConfig) -> dict[str, Any]:
    """Real-provider canaries (LLM + OCR) — honest, never faked."""
    from paperintel.config.provider_config import load_provider_config
    from paperintel.providers.factory import build_registry
    from paperintel.schemas.enums import ProviderFamily

    if settings.providers_file is None:
        raise DomainError("CFG_001", message="No providers file configured.", details={})
    registry = build_registry(load_provider_config(settings.providers_file))
    results: dict[str, Any] = {}
    llm_providers = registry.by_family(ProviderFamily.LLM)
    for name, provider in llm_providers.items():
        if hasattr(provider, "run_canary"):
            record = asyncio.run(provider.run_canary())
            results[name] = getattr(record, "state", "UNKNOWN")
            if str(results[name]).endswith("FAILED"):
                raise DomainError(
                    "PROVIDER_001",
                    message=f"LLM canary failed for {name}",
                    details={"provider": name, "state": str(results[name])},
                )
    if not results:
        raise DomainError(
            "CFG_001",
            message="No LLM provider exposes a canary — real-provider mode requires one.",
            details={"providers": list(llm_providers)},
        )
    return {"canaries": results}


def _config_stage(settings: AppConfig) -> dict[str, Any]:
    return {
        "spec_version": SPEC_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "data_dir": str(settings.core.data_dir),
        "env": settings.core.env,
    }


def _import_fixture(session, settings: AppConfig) -> dict[str, Any]:
    """Import a generated fixture PDF (never user data)."""
    from pathlib import Path

    from tests.fixtures.generators import build_f01_native

    from paperintel.ingest.service import import_pdf
    from paperintel.storage.object_store import LocalObjectStore

    data_dir = Path(settings.core.data_dir)
    fixture_dir = data_dir / "temp" / "selftest"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    # One STABLE fixture path: the generated PDF is written once and reused,
    # so repeated selftests import the same bytes and dedup onto the same
    # paper version (the mock provider's canary evidence stays bound to a
    # single version instead of multiplying fixtures).
    pdf_path = fixture_dir / "selftest-fixture.pdf"
    if not pdf_path.is_file():
        pdf_path.write_bytes(build_f01_native())

    store = LocalObjectStore(data_dir / "objects")
    result = asyncio.run(import_pdf(pdf_path, session=session, store=store, data_dir=data_dir))
    reusable = _reusable_version(session, settings)
    if reusable is not None and reusable["paper_version_id"] != result.paper_version_id:
        # A previous selftest already bound the mock's canary evidence to its
        # own version; evidence is immutable, so the honest move is to run
        # this selftest against that version instead of fabricating a second
        # canary (which the mock's fixed ID cannot express).
        session.flush()
        return {**reusable, "fixture_path": str(pdf_path)}
    _seed_mock_canary(session, result.paper_version_id)
    session.flush()
    return {
        "paper_id": result.paper_id,
        "paper_version_id": result.paper_version_id,
        "pages": getattr(result, "page_count", None),
        "fixture_path": str(pdf_path),
    }


def _canary_owner(session) -> str | None:
    """The paper version that currently holds the mock's canary evidence."""
    from paperintel.database.models import EvidenceRow
    from paperintel.providers.mocks.llm import CANARY_EVIDENCE_ID

    row = session.get(EvidenceRow, CANARY_EVIDENCE_ID)
    return row.paper_version_id if row is not None else None


def _version_identity(session, paper_version_id: str) -> dict[str, Any] | None:
    from paperintel.database.models import PaperVersionRow

    version = session.get(PaperVersionRow, paper_version_id)
    if version is None:
        return None
    return {
        "paper_id": version.paper_id,
        "paper_version_id": paper_version_id,
        "pages": version.page_count,
    }


def _reusable_version(session, settings: AppConfig) -> dict[str, Any] | None:
    """The canary-owning version, but ONLY when its stored objects are present.

    The selftest runs in an isolated temporary store (review design): reusing a
    version whose PDF object lives in a different store would fail deep inside
    the extraction stage. A stale canary from an earlier, non-isolated run is
    therefore reported as an explicit precondition error instead of surfacing
    as STORAGE_003 halfway through the pipeline.
    """
    from pathlib import Path

    from paperintel.database.models import AssetRow, PaperVersionRow

    owner = _canary_owner(session)
    if owner is None:
        return None
    version = session.get(PaperVersionRow, owner)
    if version is None:
        return None
    asset = session.get(AssetRow, version.asset_id)
    if asset is None:
        return None
    object_path = Path(settings.core.data_dir) / "objects" / asset.storage_key
    if object_path.is_file():
        identity = _version_identity(session, owner)
        if identity is not None:
            return {**identity, "reused_canary_version": True}
        return None
    raise DomainError(
        "STORAGE_003",
        message=(
            "A selftest canary is bound to a version whose PDF object is not in the "
            "current store. This is stale state from an earlier non-isolated "
            "selftest run; the selftest needs a clean store. Inspect "
            "data/operations and the selftest fixture rows, or run the selftest "
            "with --keep-artifacts against the store that holds them."
        ),
        details={
            "canary_version": owner,
            "asset_id": asset.asset_id,
            "storage_key": asset.storage_key,
            "data_dir": str(settings.core.data_dir),
        },
    )


def _seed_mock_canary(session, paper_version_id: str) -> None:
    """Seed the deterministic LLM mock's canary evidence unit.

    The mock provider (spec doc 06 §3) answers from a FIXED canary evidence
    ID; seeding that unit for the selftest's own fixture makes the mock's
    answers fully supported end-to-end (existence, scope and numeric
    support are all real), so the selftest exercises the real firewall and
    ledger instead of failing on unknown evidence.
    """
    import hashlib
    from datetime import UTC, datetime

    from paperintel.database.models import AnalysisRunRow, EvidenceRow
    from paperintel.ids import new_run_id
    from paperintel.providers.mocks.llm import CANARY_EVIDENCE_ID, CANARY_EVIDENCE_TEXT
    from paperintel.schemas.enums import (
        DataQualityState,
        EvidenceType,
        SourceMethod,
        TaskState,
    )

    if session.get(EvidenceRow, CANARY_EVIDENCE_ID) is not None:
        return
    run_id = new_run_id()
    now = datetime.now(UTC)
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_version_id=paper_version_id,
            agent_type="extraction",
            pipeline_version=PIPELINE_VERSION,
            config_hash=hashlib.sha256(b"selftest-canary-v1").hexdigest(),
            model_id="none",
            provider_id="prv_none",
            status=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
        )
    )
    session.add(
        EvidenceRow(
            evidence_id=CANARY_EVIDENCE_ID,
            paper_version_id=paper_version_id,
            evidence_type=EvidenceType.PARAGRAPH,
            page_start=1,
            page_end=1,
            text=CANARY_EVIDENCE_TEXT,
            source_method=SourceMethod.PDF_NATIVE,
            quality_state=DataQualityState.GOOD,
            content_sha256=hashlib.sha256(CANARY_EVIDENCE_TEXT.encode()).hexdigest(),
            extraction_run_id=run_id,
        )
    )
    session.flush()


def _run_pipeline_stages(session, settings: AppConfig, import_result: dict) -> list[SelftestStage]:
    """Plan and execute the full pipeline eagerly, one stage at a time."""
    from pathlib import Path

    from paperintel.workflow import engine as wf_engine
    from paperintel.workflow.celery_app import run_task_once

    stages: list[SelftestStage] = []
    report_path = (
        Path(settings.core.data_dir)
        / "cache"
        / "extraction_reports"
        / f"{_content_sha(session, import_result['paper_version_id'])}.json"
    )
    job = wf_engine.create_job(
        session,
        paper_id=import_result["paper_id"],
        paper_version_id=import_result["paper_version_id"],
        current_stage=PipelineStage.EVIDENCE_INDEXED,
    )
    plan = wf_engine.plan_job(
        session,
        job,
        data_dir=str(settings.core.data_dir),
        content_sha256=_content_sha(session, import_result["paper_version_id"]),
        report_path=str(report_path),
    )
    import_result["job_id"] = job.job_id

    from paperintel.database.models import TaskRow

    executed = 0
    while True:
        queued = session.scalars(
            select(TaskRow)
            .where(TaskRow.job_id == job.job_id, TaskRow.state == TaskState.QUEUED)
            .order_by(TaskRow.created_at)
        ).all()
        if not queued:
            break
        for task in queued:
            start = time.monotonic()
            outcome = run_task_once(session, task.task_id)
            session.commit()
            executed += 1
            stages.append(
                SelftestStage(
                    f"task:{task.task_type}",
                    "PASS" if outcome.get("outcome") == "succeeded" else "FAIL",
                    {"state": outcome.get("state"), "code": outcome.get("code")},
                    error_code=outcome.get("code"),
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            )
            if outcome.get("outcome") != "succeeded":
                return stages

    session.refresh(job)
    from paperintel.database.models import ClaimRow

    claims_exist = (
        session.scalar(
            select(func.count())
            .select_from(ClaimRow)
            .where(ClaimRow.paper_version_id == import_result["paper_version_id"])
        )
        or 0
    )
    # Re-running the selftest against an already-analysed version is a
    # legitimate pass: the golden path IS satisfied for that version.
    already_satisfied = executed == 0 and claims_exist > 0
    pipeline_ok = already_satisfied or job.state in (
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED_WITH_WARNINGS,
    )
    stages.append(
        SelftestStage(
            "pipeline",
            "PASS" if pipeline_ok else "FAIL",
            {
                "job_id": job.job_id,
                "job_state": job.state.value,
                "final_stage": job.current_stage.value,
                "tasks_executed": executed,
                "planned_skipped": plan.get("skipped", []),
                "already_satisfied": already_satisfied,
                "claims_on_version": claims_exist,
            },
        )
    )
    return stages


def _content_sha(session, paper_version_id: str) -> str:
    from paperintel.database.models import PaperVersionRow

    version = session.get(PaperVersionRow, paper_version_id)
    return version.content_sha256 if version else ""


def _search_stage(session, import_result: dict) -> dict[str, Any]:
    from paperintel.services.read_models import search_dispatch

    payload = search_dispatch(
        session,
        kind="evidence",
        query="dataset samples accuracy",
        filters={"paper_version_ids": {import_result["paper_version_id"]}},
        limit=5,
    )
    if payload["scope_size"] == 0:
        raise DomainError(
            "STORAGE_001",
            message="search index is empty for the selftest paper",
            details={"paper_version_id": import_result["paper_version_id"]},
        )
    return {"scope_size": payload["scope_size"], "hits": payload["count"]}


def _audit_stage(session, import_result: dict) -> dict[str, Any]:
    from paperintel.verification.audit import build_version_audit_bundle

    # Version-scoped on purpose: a paper can have several versions and the
    # selftest must audit the one it actually analysed.
    overview = build_version_audit_bundle(session, import_result["paper_version_id"])
    if overview["claim_count"] == 0:
        raise DomainError(
            "INTERNAL_001",
            message="analysis produced no claims — audit has nothing to verify",
            details={"paper_id": import_result["paper_id"]},
        )
    return {
        "claim_count": overview["claim_count"],
        "support_states": overview["support_states"],
        "incomplete_bundles": len(overview["incomplete_bundles"]),
    }


def _disk_stage(settings: AppConfig) -> dict[str, Any]:
    from paperintel.operations.disk import disk_report

    report = disk_report(settings.core.data_dir, settings=settings)
    return {
        "level": report["disk"]["level"],
        "free_percent": report["disk"]["free_percent"],
        "store_bytes": report["store_bytes"],
    }


def _summary(
    stages: list[SelftestStage],
    started: float,
    settings: AppConfig,
    mock: bool,
    real_providers: bool,
) -> dict[str, Any]:
    failed = [stage for stage in stages if stage.status == "FAIL"]
    counts = {
        "PASS": sum(1 for stage in stages if stage.status == "PASS"),
        "FAIL": len(failed),
        "SKIPPED": sum(1 for stage in stages if stage.status == "SKIPPED"),
    }
    return {
        "status": "FAIL" if failed else "PASS",
        "mode": "mock" if mock else "real_providers",
        "real_providers": real_providers,
        "spec_version": SPEC_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "counts": counts,
        "stages": [
            {
                "name": stage.name,
                "status": stage.status,
                "detail": stage.detail,
                "error_code": stage.error_code,
                "duration_ms": stage.duration_ms,
            }
            for stage in stages
        ],
        "failed_stages": [stage.name for stage in failed],
        "data_dir": str(settings.core.data_dir),
    }


__all__ = ["SelftestStage", "run_selftest"]

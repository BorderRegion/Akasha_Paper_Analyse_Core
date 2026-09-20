"""services.read_models — the shared read models for every surface (P12).

REST, MCP and the CLI all call THESE functions; no surface owns business
logic (doc 04 §7 "MCP tools call the same service/domain layer as REST").
They compose the phase services (evidence, workflow, verification, search,
corpus) into the response shapes the API contract specifies.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paperintel.config.settings import AppConfig, get_settings
from paperintel.database.models import (
    AnalysisRunRow,
    ClaimRow,
    CollectionRow,
    EvidenceRow,
    JobRow,
    ModelCallRow,
    PaperRow,
    PaperTagRow,
    PromptVersionRow,
    TagRow,
    TaskRow,
    TriageResultRow,
)
from paperintel.errors import DomainError
from paperintel.operations.disk import disk_report
from paperintel.schemas.enums import (
    PipelineStage,
    ResourceTier,
    SupportState,
    TaskState,
)
from paperintel.version import PIPELINE_VERSION, SPEC_VERSION


def _paper_or_fail(session: Session, paper_id: str) -> PaperRow:
    paper = session.get(PaperRow, paper_id)
    if paper is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown paper ID: {paper_id}",
            details={"paper_id": paper_id},
        )
    return paper


def _latest_version(session: Session, paper_id: str):
    from paperintel.database.models import PaperVersionRow

    return session.scalars(
        select(PaperVersionRow)
        .where(PaperVersionRow.paper_id == paper_id)
        .order_by(PaperVersionRow.created_at.desc())
    ).first()


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def system_status(session: Session, *, settings: AppConfig | None = None) -> dict[str, Any]:
    """`GET /v1/system/status` (doc 04 §2.1)."""
    settings = settings or get_settings()
    from paperintel.operations.heartbeat import read_heartbeats
    from paperintel.workflow.celery_app import tasks_eager

    queue_counts = {
        "pending": session.scalar(
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.state.in_([TaskState.PENDING, TaskState.QUEUED, TaskState.WAITING]))
        )
        or 0,
        "running": session.scalar(
            select(func.count())
            .select_from(TaskRow)
            .where(TaskRow.state.in_([TaskState.RUNNING, TaskState.RETRYING]))
        )
        or 0,
        "failed": session.scalar(
            select(func.count()).select_from(TaskRow).where(TaskRow.state == TaskState.FAILED)
        )
        or 0,
    }

    disk = disk_report(settings.core.data_dir, settings=settings)
    database_estimate = 0
    try:
        database_estimate = int(
            session.scalar(select(func.pg_database_size(func.current_database()))) or 0
        )
    except Exception:  # noqa: BLE001 - non-PostgreSQL/test doubles: report 0
        database_estimate = 0

    breakdown = {
        entry["retention_class"].lower(): entry["bytes"] for entry in disk["by_retention_class"]
    }
    breakdown["database_estimate"] = database_estimate

    providers = providers_status(settings=settings)
    state = "HEALTHY"
    if disk["disk"]["level"] == "WARNING":
        state = "DEGRADED"
    elif disk["disk"]["level"] == "CRITICAL":
        state = "DEGRADED"
    if queue_counts["failed"]:
        state = "DEGRADED" if state == "HEALTHY" else state

    return {
        "spec_version": SPEC_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "overall_state": state,
        "queue": queue_counts,
        "providers": {
            name: {
                "family": entry["family"],
                "circuit_breaker_state": entry["circuit_breaker_state"],
            }
            for name, entry in providers.get("providers", {}).items()
        },
        "workers": {
            "worker_count": sum(
                heartbeat.state == "FRESH"
                for heartbeat in read_heartbeats(settings.core.data_dir)
            ),
            "mode": "eager" if tasks_eager() else "celery",
        },
        "disk": {
            "total_bytes": disk["disk"]["total_bytes"],
            "used_bytes": disk["disk"]["used_bytes"],
            "free_bytes": disk["disk"]["free_bytes"],
            "free_percent": disk["disk"]["free_percent"],
            "level": disk["disk"]["level"],
            "breakdown": breakdown,
            "store_bytes": disk["store_bytes"],
        },
        "pipeline": pipeline_totals(session),
    }


def pipeline_totals(session: Session) -> dict[str, Any]:
    """Corpus-wide pipeline counters (papers, versions, claims, evidence)."""
    return {
        "papers": session.scalar(select(func.count()).select_from(PaperRow)) or 0,
        "claims": session.scalar(select(func.count()).select_from(ClaimRow)) or 0,
        "claims_supported": session.scalar(
            select(func.count())
            .select_from(ClaimRow)
            .where(ClaimRow.support_state == SupportState.SUPPORTED)
        )
        or 0,
        "evidence": session.scalar(select(func.count()).select_from(EvidenceRow)) or 0,
        "jobs": session.scalar(select(func.count()).select_from(JobRow)) or 0,
        "analysis_runs": session.scalar(select(func.count()).select_from(AnalysisRunRow)) or 0,
        "model_calls": session.scalar(select(func.count()).select_from(ModelCallRow)) or 0,
    }


def system_version() -> dict[str, Any]:
    """`GET /v1/system/version`."""
    return {
        "spec_version": SPEC_VERSION,
        "pipeline_version": PIPELINE_VERSION,
    }


def modules_status() -> dict[str, Any]:
    """`GET /v1/system/modules` — bundled module manifests."""
    from paperintel.modules import load_bundled_manifests

    manifests = load_bundled_manifests()
    return {
        "count": len(manifests),
        "modules": [
            {
                "module_id": manifest.module_id,
                "version": manifest.version,
                "healthcheck": manifest.healthcheck.model_dump()
                if hasattr(manifest.healthcheck, "model_dump")
                else str(manifest.healthcheck),
            }
            for manifest in sorted(manifests.values(), key=lambda item: item.module_id)
        ],
    }


def providers_status(*, settings: AppConfig | None = None) -> dict[str, Any]:
    """`GET /v1/system/providers` — configured providers and breaker state.

    Never returns credential values (security redaction).
    """
    settings = settings or get_settings()
    if settings.providers_file is None:
        return {"providers": {}, "note": "no providers file configured"}
    from paperintel.config.provider_config import load_provider_config
    from paperintel.providers.factory import build_registry

    config = load_provider_config(settings.providers_file)
    registry = build_registry(config)
    from paperintel.operations.provider_observations import snapshots

    observations = snapshots(settings.core.data_dir)
    entries: dict[str, Any] = {}
    for name in registry.names():
        provider = registry.get(name)
        entries[name] = {
            "provider_id": provider.provider_id,
            "family": provider.family.value,
            "availability_state": "UNKNOWN",
            "canary_state": "UNKNOWN",
            "circuit_breaker_state": "UNKNOWN",
            "schema_pass_rate": None,
            "citation_pass_rate": None,
            "unsupported_claim_rate": None,
            **observations.get(provider.provider_id, {}),
        }
    return {"providers": entries}


def storage_status(*, settings: AppConfig | None = None) -> dict[str, Any]:
    """`GET /v1/system/storage`."""
    settings = settings or get_settings()
    return disk_report(settings.core.data_dir, settings=settings)


def workers_status(session: Session, *, settings: AppConfig | None = None) -> dict[str, Any]:
    """`GET /v1/system/workers` — queue state per task type."""
    settings = settings or get_settings()
    from paperintel.workflow.celery_app import tasks_eager

    rows = session.execute(
        select(TaskRow.task_type, TaskRow.state, func.count())
        .group_by(TaskRow.task_type, TaskRow.state)
        .order_by(TaskRow.task_type)
    ).all()
    by_type: dict[str, dict[str, int]] = {}
    for task_type, state, count in rows:
        by_type.setdefault(task_type, {})[state.value] = int(count)
    return {
        "mode": "eager" if tasks_eager() else "celery",
        "by_task_type": by_type,
    }


def metrics_snapshot(session: Session, *, settings: AppConfig | None = None) -> str:
    """Prometheus-style text metrics for `GET /metrics`."""
    status = system_status(session, settings=settings)
    lines = [
        "# HELP paperintel_queue_tasks Tasks by queue state",
        "# TYPE paperintel_queue_tasks gauge",
    ]
    for state, count in status["queue"].items():
        lines.append(f'paperintel_queue_tasks{{state="{state}"}} {count}')
    lines += [
        "# HELP paperintel_pipeline_records Canonical record counts",
        "# TYPE paperintel_pipeline_records gauge",
    ]
    for name, count in status["pipeline"].items():
        lines.append(f'paperintel_pipeline_records{{kind="{name}"}} {count}')
    lines += [
        "# HELP paperintel_disk_free_bytes Free bytes on the store volume",
        "# TYPE paperintel_disk_free_bytes gauge",
        f"paperintel_disk_free_bytes {status['disk']['free_bytes']}",
        "# HELP paperintel_spec_info Spec and pipeline versions",
        "# TYPE paperintel_spec_info gauge",
        f'paperintel_spec_info{{spec_version="{SPEC_VERSION}",'
        f'pipeline_version="{PIPELINE_VERSION}"}} 1',
    ]
    from paperintel.operations.metrics import export_metrics

    return "\n".join(lines) + "\n" + export_metrics(session, status, settings=settings)


# ---------------------------------------------------------------------------
# papers
# ---------------------------------------------------------------------------


def paper_summary(session: Session, paper_id: str) -> dict[str, Any]:
    """`GET /v1/papers/{paper_id}` core record."""
    paper = _paper_or_fail(session, paper_id)
    version = _latest_version(session, paper_id)
    triage = session.scalars(
        select(TriageResultRow)
        .where(TriageResultRow.paper_id == paper_id)
        .order_by(TriageResultRow.created_at.desc())
    ).first()
    return {
        "paper_id": paper.paper_id,
        "canonical_title": paper.canonical_title,
        "doi": paper.doi,
        "paper_type": paper.paper_type,
        "primary_language": paper.primary_language,
        "created_at": paper.created_at.isoformat(),
        "latest_version": (
            {
                "paper_version_id": version.paper_version_id,
                "version_label": version.version_label,
                "content_sha256": version.content_sha256,
                "page_count": version.page_count,
                "publication_date": version.publication_date.isoformat()
                if version.publication_date
                else None,
            }
            if version is not None
            else None
        ),
        "triage": (
            {
                "recommended_tier": triage.recommended_tier.value,
                "effective_tier": triage.effective_tier.value,
                "manual_override": triage.manual_override,
                "reason_codes": triage.reason_codes,
            }
            if triage is not None
            else None
        ),
    }


def paper_context(session: Session, paper_id: str) -> dict[str, Any]:
    """`GET /v1/papers/{paper_id}/context` — the preferred external-AI entry
    (doc 04 §3): identity, structure, evidence counts, claims by state,
    quality and provenance, redacted of secrets."""
    _paper_or_fail(session, paper_id)  # unknown paper → CFG_002
    version = _latest_version(session, paper_id)
    if version is None:
        raise DomainError(
            "CFG_002",
            message="Paper has no imported versions.",
            details={"paper_id": paper_id},
        )

    from paperintel.evidence.retrieval import section_tree, version_summary

    version_id = version.paper_version_id
    claims = list(
        session.scalars(
            select(ClaimRow)
            .where(ClaimRow.paper_version_id == version_id)
            .order_by(ClaimRow.created_at, ClaimRow.claim_id)
        )
    )
    by_state: dict[str, int] = {}
    for claim in claims:
        by_state[claim.support_state.value] = by_state.get(claim.support_state.value, 0) + 1

    evidence_rows = list(
        session.scalars(select(EvidenceRow).where(EvidenceRow.paper_version_id == version_id))
    )
    quality_states: dict[str, int] = {}
    for row in evidence_rows:
        quality_states[row.quality_state.value] = quality_states.get(row.quality_state.value, 0) + 1

    tags = [
        {"namespace": tag.namespace, "name": tag.canonical_name, "is_candidate": link.is_candidate}
        for link, tag in session.execute(
            select(PaperTagRow, TagRow)
            .join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
            .where(PaperTagRow.paper_id == paper_id)
        ).all()
    ]

    return {
        "paper": paper_summary(session, paper_id),
        "structure": section_tree(session, version_id),
        "summary": version_summary(session, version),
        "claims": {
            "total": len(claims),
            "by_support_state": by_state,
            "categories": sorted({claim.category for claim in claims}),
        },
        "evidence": {
            "total": len(evidence_rows),
            "by_quality_state": quality_states,
            "by_source_method": _count_by(evidence_rows, "source_method"),
        },
        "tags": tags,
        "versions": {
            "pipeline_version": PIPELINE_VERSION,
            "spec_version": SPEC_VERSION,
        },
    }


def _count_by(rows: list[Any], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = getattr(row, attribute)
        key = getattr(value, "value", str(value))
        counts[key] = counts.get(key, 0) + 1
    return counts


def paper_analysis(session: Session, paper_id: str) -> dict[str, Any]:
    """`GET /v1/papers/{paper_id}/analysis` — claims grouped by category with
    their support state and provenance."""
    version = _latest_version(session, paper_id)
    if version is None:
        raise DomainError(
            "CFG_002", message="Paper has no imported versions.", details={"paper_id": paper_id}
        )
    claims = list(
        session.scalars(
            select(ClaimRow)
            .where(ClaimRow.paper_version_id == version.paper_version_id)
            .order_by(ClaimRow.category, ClaimRow.created_at)
        )
    )
    grouped: dict[str, list[dict]] = {}
    for claim in claims:
        grouped.setdefault(claim.category, []).append(
            {
                "claim_id": claim.claim_id,
                "statement": claim.statement,
                "claim_type": claim.claim_type.value,
                "support_state": claim.support_state.value,
                "created_by_run_id": claim.created_by_run_id,
            }
        )
    return {
        "paper_id": paper_id,
        "paper_version_id": version.paper_version_id,
        "claim_count": len(claims),
        "by_category": grouped,
        "runs": _recent_runs(session, version.paper_version_id),
    }


def paper_claims(session: Session, paper_id: str) -> dict[str, Any]:
    analysis = paper_analysis(session, paper_id)
    claims = [
        claim_detail(session, item["claim_id"])
        for items in analysis["by_category"].values()
        for item in items
    ]
    return {
        "paper_id": paper_id,
        "paper_version_id": analysis["paper_version_id"],
        "claims": claims,
        "count": len(claims),
    }


def paper_profile(session: Session, paper_id: str, kind: str) -> dict[str, Any]:
    from paperintel.database.models import TechniquePaperRow, TechniqueRow

    payload = paper_claims(session, paper_id)
    agent = {
        "methods": "agents.method",
        "experiments": "agents.experiment",
        "techniques": "agents.technique",
        "people": "agents.people",
    }[kind]
    items = [
        claim
        for claim in payload["claims"]
        if (claim["created_by_run"] or {}).get("agent_type") == agent
    ]
    result = {
        "paper_id": paper_id,
        "paper_version_id": payload["paper_version_id"],
        kind: items,
        "count": len(items),
    }
    if kind == "techniques":
        rows = session.scalars(
            select(TechniqueRow)
            .join(TechniquePaperRow)
            .where(TechniquePaperRow.paper_id == paper_id)
        ).all()
        result["techniques"] = [
            {
                column.name: getattr(row, column.name)
                for column in TechniqueRow.__table__.columns
                if column.name != "created_at"
            }
            for row in rows
        ]
        result["claims"] = items
        result["count"] = len(rows)
    return result


def paper_pipeline(session: Session, paper_id: str) -> dict[str, Any]:
    from paperintel.workflow.engine import STAGE_FOR_TASK, job_progress

    _paper_or_fail(session, paper_id)
    job = session.scalars(
        select(JobRow)
        .where(JobRow.paper_id == paper_id)
        .order_by(JobRow.created_at.desc(), JobRow.job_id.desc())
    ).first()
    if job is None:
        return {
            "paper_id": paper_id,
            "job_id": None,
            "trace_id": None,
            "requested_tier": None,
            "effective_tier": None,
            "state": "NOT_STARTED",
            "stages": [],
            "eta_p50_seconds": None,
            "eta_p90_seconds": None,
        }
    tasks = session.scalars(
        select(TaskRow)
        .where(TaskRow.job_id == job.job_id)
        .order_by(TaskRow.created_at, TaskRow.task_id)
    ).all()
    stages: dict[str, list] = {}
    for task in tasks:
        stage = STAGE_FOR_TASK.get(task.task_type)
        name = stage.value.lower() if stage else task.task_type.removeprefix("stage.")
        stages.setdefault(name, []).append(task)
    priorities = [
        "FAILED",
        "RUNNING",
        "RETRYING",
        "BLOCKED",
        "WAITING",
        "QUEUED",
        "PENDING",
        "CANCELLED",
        "SUCCEEDED_WITH_WARNINGS",
        "SUCCEEDED",
        "SKIPPED",
    ]
    progress = job_progress(session, job.job_id)
    return {
        "paper_id": paper_id,
        "job_id": job.job_id,
        "trace_id": job.trace_id,
        "requested_tier": job.requested_tier.value,
        "effective_tier": job.effective_tier.value,
        "state": job.state.value,
        "progress": progress,
        "eta_p50_seconds": progress.get("eta_p50"),
        "eta_p90_seconds": progress.get("eta_p90"),
        "stages": [
            {
                "name": name,
                "state": min((t.state.value for t in group), key=priorities.index),
                "quality": "DEGRADED"
                if any(t.quality_state.value == "DEGRADED" for t in group)
                else ("GOOD" if all(t.quality_state.value == "GOOD" for t in group) else "UNKNOWN"),
                "subtasks": [
                    {"task_id": t.task_id, "state": t.state.value, "quality": t.quality_state.value}
                    for t in group
                ],
            }
            for name, group in stages.items()
        ],
    }


def _recent_runs(session: Session, version_id: str) -> list[dict]:
    rows = session.scalars(
        select(AnalysisRunRow)
        .where(AnalysisRunRow.paper_version_id == version_id)
        .order_by(AnalysisRunRow.started_at.desc())
    ).all()
    return [
        {
            "run_id": row.run_id,
            "agent_type": row.agent_type,
            "status": row.status.value,
            "model_id": row.model_id,
            "provider_id": row.provider_id,
            "started_at": row.started_at.isoformat(),
        }
        for row in rows
    ]


def paper_audit(session: Session, paper_id: str) -> dict[str, Any]:
    """`GET /v1/papers/{paper_id}/audit` — version-level audit overview."""
    from paperintel.verification.audit import build_version_audit_bundle

    version = _latest_version(session, paper_id)
    if version is None:
        raise DomainError(
            "CFG_002", message="Paper has no imported versions.", details={"paper_id": paper_id}
        )
    overview = build_version_audit_bundle(session, version.paper_version_id)
    overview["paper_id"] = paper_id
    overview["pipeline_version"] = PIPELINE_VERSION
    return overview


def claim_detail(session: Session, claim_id: str) -> dict[str, Any]:
    """`GET /v1/claims/{claim_id}` — the claim with its full audit bundle."""
    from paperintel.verification.audit import build_claim_audit_bundle

    bundle = build_claim_audit_bundle(session, claim_id)
    return {
        "claim_id": bundle.claim_id,
        "paper_id": bundle.paper_id,
        "paper_version_id": bundle.paper_version_id,
        "claim_type": bundle.claim_type,
        "category": bundle.category,
        "statement": bundle.statement,
        "support_state": bundle.support_state,
        "evidence": bundle.evidence,
        "verifications": bundle.verifications,
        "created_by_run": bundle.created_by_run,
        "model_calls": bundle.model_calls,
        "external_provenance": bundle.external_provenance,
        "warnings": bundle.warnings,
        "provenance_complete": bundle.complete,
    }


def claim_evidence(session: Session, claim_id: str) -> dict[str, Any]:
    bundle = claim_detail(session, claim_id)
    return {"claim_id": claim_id, "evidence": bundle["evidence"]}


def claim_verifications(session: Session, claim_id: str) -> dict[str, Any]:
    bundle = claim_detail(session, claim_id)
    return {
        "claim_id": claim_id,
        "support_state": bundle["support_state"],
        "verifications": bundle["verifications"],
    }


def evidence_detail(session: Session, evidence_id: str) -> dict[str, Any]:
    """`GET /v1/evidence/{evidence_id}`."""
    from paperintel.evidence.retrieval import evidence_to_contract

    row = session.get(EvidenceRow, evidence_id)
    if row is None:
        raise DomainError(
            "EVIDENCE_001",
            message=f"Unknown evidence ID: {evidence_id}",
            details={"evidence_id": evidence_id},
        )
    contract = evidence_to_contract(row)
    payload = contract.model_dump(mode="json")
    payload["asset"] = {"asset_id": row.asset_id} if getattr(row, "asset_id", None) else None
    return payload


def typed_evidence(session: Session, paper_version_id: str, evidence_type: str, limit: int = 20):
    from paperintel.evidence.retrieval import list_evidence

    rows = [
        row
        for row in list_evidence(session, paper_version_id, include_superseded=False)
        if row.evidence_type.value == evidence_type
    ][:limit]
    return {
        "paper_version_id": paper_version_id,
        "evidence_type": evidence_type,
        "count": len(rows),
        "evidence": [evidence_detail(session, row.evidence_id) for row in rows],
    }


def evidence_for_page(session: Session, paper_version_id: str, page: int) -> dict[str, Any]:
    """`get_page_evidence` (MCP): evidence located on one page."""
    rows = session.scalars(
        select(EvidenceRow)
        .where(
            EvidenceRow.paper_version_id == paper_version_id,
            EvidenceRow.page_start <= page,
            EvidenceRow.page_end >= page,
        )
        .order_by(EvidenceRow.evidence_id)
    ).all()
    return {
        "paper_version_id": paper_version_id,
        "page": page,
        "count": len(rows),
        "evidence": [
            {
                "evidence_id": row.evidence_id,
                "evidence_type": row.evidence_type.value,
                "text": (row.text or "")[:2000],
                "quality_state": row.quality_state.value,
            }
            for row in rows
        ],
    }


# ---------------------------------------------------------------------------
# jobs / tasks / traces
# ---------------------------------------------------------------------------


def job_list(session: Session, *, state: str | None = None, limit: int = 50) -> dict[str, Any]:
    """`GET /v1/jobs`."""
    from paperintel.workflow import engine

    if not 1 <= limit <= 200:
        raise DomainError("CFG_002", message="limit must be between 1 and 200.")
    stmt = select(JobRow).order_by(JobRow.created_at.desc()).limit(limit)
    if state:
        try:
            wanted = TaskState(state.upper())
        except ValueError as exc:
            raise DomainError(
                "CFG_002",
                message=f"Unknown job state: {state!r}",
                details={"state": state},
            ) from exc
        stmt = stmt.where(JobRow.state.is_(wanted))
    jobs = list(session.scalars(stmt))
    return {
        "count": len(jobs),
        "jobs": [
            {
                "job_id": job.job_id,
                "paper_id": job.paper_id,
                "paper_version_id": job.paper_version_id,
                "state": job.state.value,
                "current_stage": job.current_stage.value,
                "trace_id": job.trace_id,
                "progress": engine.job_progress(session, job.job_id)["tasks_by_state"],
            }
            for job in jobs
        ],
    }


def job_detail(session: Session, job_id: str) -> dict[str, Any]:
    """`GET /v1/jobs/{job_id}`."""
    from paperintel.workflow import engine

    job = session.get(JobRow, job_id)
    if job is None:
        raise DomainError(
            "CFG_002", message=f"Unknown job ID: {job_id}", details={"job_id": job_id}
        )
    progress = engine.job_progress(session, job_id)
    tasks = session.scalars(
        select(TaskRow).where(TaskRow.job_id == job_id).order_by(TaskRow.created_at)
    ).all()
    return {
        "job": {
            "job_id": job.job_id,
            "paper_id": job.paper_id,
            "paper_version_id": job.paper_version_id,
            "state": job.state.value,
            "current_stage": job.current_stage.value,
            "trace_id": job.trace_id,
            "priority": job.priority,
            "created_at": job.created_at.isoformat(),
        },
        "progress": progress,
        "tasks": [
            {
                "task_id": task.task_id,
                "task_type": task.task_type,
                "module_id": task.module_id,
                "state": task.state.value,
                "attempt": task.attempt,
                "error_code": task.error_code,
                "idempotency_key": task.idempotency_key,
                "output_manifest": task.output_manifest,
            }
            for task in tasks
        ],
    }


def task_detail(session: Session, task_id: str) -> dict[str, Any]:
    """`GET /v1/tasks/{task_id}`."""
    task = session.get(TaskRow, task_id)
    if task is None:
        raise DomainError(
            "CFG_002", message=f"Unknown task ID: {task_id}", details={"task_id": task_id}
        )
    return {
        "task_id": task.task_id,
        "job_id": task.job_id,
        "task_type": task.task_type,
        "module_id": task.module_id,
        "state": task.state.value,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "error_code": task.error_code,
        "error_details": task.error_details,
        "input_manifest": task.input_manifest,
        "output_manifest": task.output_manifest,
        "trace_id": task.trace_id,
    }


def trace_detail(session: Session, trace_id: str) -> dict[str, Any]:
    """`GET /v1/traces/{trace_id}`."""
    from paperintel.operations import tracing

    events = tracing.read_trace(session, trace_id)
    return {
        "trace_id": trace_id,
        "count": len(events),
        "events": [
            {
                "kind": event.kind,
                "message": event.message,
                "task_id": event.task_id,
                "job_id": event.job_id,
                "occurred_at": event.occurred_at.isoformat(),
                "data": event.data,
            }
            for event in events
        ],
    }


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def search_dispatch(
    session: Session,
    *,
    kind: str,
    query: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 20,
    embedder=None,
) -> dict[str, Any]:
    """`POST /v1/search/*` — one entry point for every search surface.

    ``kind`` selects the document type filter (papers/claims/evidence/
    entities/techniques/methods); scope filters are the caller's exact
    structured filters.
    """
    from paperintel.search.query import (
        DOCUMENT_CLAIM,
        DOCUMENT_EVIDENCE,
        DOCUMENT_PAPER,
        SearchFilters,
        search,
    )

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise DomainError("CFG_002", message="limit must be an integer between 1 and 200.")
    filters = dict(filters or {})
    document_types = {
        "papers": {DOCUMENT_PAPER},
        "claims": {DOCUMENT_CLAIM},
        "evidence": {DOCUMENT_EVIDENCE},
    }.get(kind)
    if document_types is None and kind not in ("entities", "techniques", "methods"):
        raise DomainError(
            "CFG_002", message=f"Unknown search kind: {kind!r}", details={"kind": kind}
        )
    if kind in ("entities", "techniques", "methods"):
        # Entity-shaped searches go through the graph filter (entity_types),
        # which the retrieval layer intersects with every other scope filter.
        entity_type = {
            "entities": None,
            "techniques": ["TECHNIQUE"],
            "methods": ["METHOD"],
        }[kind]
        filters["document_types"] = {DOCUMENT_EVIDENCE, DOCUMENT_CLAIM}
        if entity_type:
            filters["entity_types"] = set(entity_type)

    collection_ids = filters.get("collection_ids") or set()
    if collection_ids:
        # A referenced collection that does not exist is an ERROR, not an
        # empty result: silently returning nothing would hide a typo.
        from paperintel.knowledge.collections import get_collection

        for collection_id in sorted(collection_ids):
            get_collection(session, collection_id)

    try:
        search_filters = SearchFilters(**filters)
    except TypeError as exc:
        raise DomainError(
            "CFG_002",
            message=f"Unknown search filter: {exc}",
            details={"filters": sorted(filters)},
        ) from exc
    if document_types is not None:
        search_filters.document_types = document_types
    response = search(
        session,
        query=query,
        filters=search_filters,
        limit=limit,
        embedder=embedder,
    )
    return {
        "kind": kind,
        "query": query or "",
        "scope_size": response.scope_size,
        "channels_used": response.channels_used,
        "warnings": response.warnings,
        "count": len(response.hits),
        "hits": [
            {
                "document_id": hit.document_id,
                "document_type": hit.document_type,
                "paper_id": hit.paper_id,
                "paper_version_id": hit.paper_version_id,
                "score": hit.score,
                "channels": hit.channels,
                "content_excerpt": hit.content_excerpt,
            }
            for hit in response.hits
        ],
    }


def compare_papers(session: Session, paper_ids: list[str]) -> dict[str, Any]:
    """`compare_papers` (MCP): side-by-side claim/tag/entity comparison."""
    if len(paper_ids) < 2:
        raise DomainError(
            "CFG_002",
            message="compare_papers requires at least two paper IDs.",
            details={"paper_ids": paper_ids},
        )
    entries = []
    for paper_id in paper_ids:
        summary = paper_summary(session, paper_id)
        version = _latest_version(session, paper_id)
        claims = (
            list(
                session.scalars(
                    select(ClaimRow).where(ClaimRow.paper_version_id == version.paper_version_id)
                )
            )
            if version is not None
            else []
        )
        entries.append(
            {
                "paper": summary,
                "claim_count": len(claims),
                "claim_categories": sorted({claim.category for claim in claims}),
                "supported_claims": sum(
                    1 for claim in claims if claim.support_state is SupportState.SUPPORTED
                ),
            }
        )
    shared_categories = set(entries[0]["claim_categories"])
    for entry in entries[1:]:
        shared_categories &= set(entry["claim_categories"])
    return {
        "papers": entries,
        "shared_claim_categories": sorted(shared_categories),
    }


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------


def collection_list(session: Session) -> dict[str, Any]:
    from paperintel.knowledge.collections import list_collections

    return {
        "count": len(list_collections(session)),
        "collections": [
            {
                "collection_id": stats.collection_id,
                "name": stats.name,
                "paper_count": stats.paper_count,
                "pinned_count": stats.pinned_count,
                "override_count": stats.override_count,
            }
            for stats in list_collections(session)
        ],
    }


def collection_detail(session: Session, collection_id: str) -> dict[str, Any]:
    from paperintel.knowledge.collections import (
        collection_paper_ids,
        collection_stats,
        get_collection,
    )

    collection = get_collection(session, collection_id)
    stats = collection_stats(session, collection_id)
    return {
        "collection_id": collection.collection_id,
        "name": collection.name,
        "purpose": collection.purpose,
        "research_questions": collection.research_questions or [],
        "preferred_tags": collection.preferred_tags or [],
        "relevance_notes": collection.relevance_notes,
        "paper_ids": collection_paper_ids(session, collection_id),
        "stats": {
            "paper_count": stats.paper_count,
            "pinned_count": stats.pinned_count,
            "override_count": stats.override_count,
        },
    }


def collection_intelligence(session: Session, collection_id: str) -> dict[str, Any]:
    """`GET /v1/collections/{collection_id}/intelligence` — corpus views."""
    from paperintel.corpus.intelligence import build_all_views
    from paperintel.knowledge.collections import get_collection

    get_collection(session, collection_id)  # unknown id → CFG_002
    return {
        "collection_id": collection_id,
        "views": build_all_views(session, collection_id=collection_id),
    }


__all__ = [
    "claim_detail",
    "claim_evidence",
    "claim_verifications",
    "collection_detail",
    "collection_intelligence",
    "collection_list",
    "compare_papers",
    "evidence_detail",
    "evidence_for_page",
    "job_detail",
    "job_list",
    "metrics_snapshot",
    "modules_status",
    "paper_analysis",
    "paper_audit",
    "paper_context",
    "paper_summary",
    "pipeline_totals",
    "providers_status",
    "search_dispatch",
    "storage_status",
    "system_status",
    "system_version",
    "task_detail",
    "trace_detail",
    "workers_status",
]


def _tier_value(tier: ResourceTier | str) -> str:
    return tier.value if isinstance(tier, ResourceTier) else str(tier)


def _stage_value(stage: PipelineStage | str) -> str:
    return stage.value if isinstance(stage, PipelineStage) else str(stage)


def prompt_inventory(session: Session) -> dict[str, Any]:
    """Registered prompt versions (observability, never secrets)."""
    rows = session.scalars(select(PromptVersionRow).order_by(PromptVersionRow.name)).all()
    return {
        "count": len(rows),
        "prompts": [
            {
                "prompt_version_id": row.prompt_version_id,
                "name": row.name,
                "version": row.version,
                "template_sha256": row.template_sha256,
            }
            for row in rows
        ],
    }


def collection_names(session: Session) -> list[str]:
    return list(session.scalars(select(CollectionRow.name).order_by(CollectionRow.name)))

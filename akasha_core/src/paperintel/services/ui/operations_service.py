"""services.ui.operations_service — operation requests, saved searches, diagnostics.

Operation kinds are an explicit table (docs/06「不接收任意函数名或shell」). Each
kind maps to an existing domain service; the operation row records the scope,
the per-target results and the jobs it created, so a client can retry only the
targets that failed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimRow,
    PaperRow,
    PaperVersionRow,
    UiOperationRequestRow,
    UiSavedSearchRow,
)
from paperintel.errors import DomainError
from paperintel.errors.ui_errors import UiError
from paperintel.ids import new_ui_operation_id, new_ui_saved_search_id
from paperintel.schemas.common import utcnow
from paperintel.schemas.ui.models import LibraryQuery

#: Supported operation kinds → human description (docs/06 §审查与外部Agent).
OPERATION_KINDS: dict[str, str] = {
    "reverify": "Re-run verification for claims",
    "reanalyze": "Queue analysis for paper versions",
    "set_tier": "Set the resource tier for papers",
    "cancel_job": "Cancel a job",
    "resume_job": "Resume a job",
    "replay_task": "Replay one task",
    "gc_preview": "Preview garbage collection",
    "gc_execute": "Execute garbage collection for a previewed scope",
    "export": "Export a bundle for paper versions",
    "corpus_refresh": "Recompute collection intelligence",
}


def submit_operation(
    session: Session,
    *,
    kind: str,
    payload: dict[str, Any],
    settings,
    idempotency_key: str | None = None,
    owner_key: str = "local",
) -> dict[str, Any]:
    if kind not in OPERATION_KINDS:
        raise DomainError(
            "CFG_002",
            message=f"Unsupported operation kind: {kind!r}",
            details={"kind": kind, "supported": sorted(OPERATION_KINDS)},
        )
    if idempotency_key:
        if len(idempotency_key) > 96:
            idempotency_key = "op:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
        existing = session.scalar(
            select(UiOperationRequestRow).where(
                UiOperationRequestRow.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            _check_repeated_operation(existing, owner_key, kind, payload)
            return _view(existing)

    scope = _scope_for(session, kind, payload)
    row = UiOperationRequestRow(
        operation_id=new_ui_operation_id(),
        owner_key=owner_key,
        kind=kind,
        payload=payload,
        state="ACCEPTED",
        scope=scope,
        results=[],
        job_ids=[],
        idempotency_key=idempotency_key,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(UiOperationRequestRow).where(
            UiOperationRequestRow.idempotency_key == idempotency_key
        )) if idempotency_key else None
        if existing is None:
            raise
        _check_repeated_operation(existing, owner_key, kind, payload)
        return _view(existing)

    handler = _HANDLERS.get(kind)
    if handler is None:  # pragma: no cover - guarded by OPERATION_KINDS
        raise DomainError("CFG_002", message=f"No handler for {kind}", details={})
    results, job_ids, state, error = (
        handler(session, payload, settings, owner_key=owner_key)
        if kind == "export" else handler(session, payload, settings)
    )
    preview_info = session.info.pop("gc_preview", None)
    if isinstance(preview_info, dict):
        # The preview's scope hash + expiry ride on the operation row so the
        # confirmation can be re-checked against the live object set.
        scope = dict(row.scope or {})
        scope.update(preview_info)
        row.scope = scope
    row.results = results
    row.job_ids = job_ids
    row.state = state
    row.error_code = error
    row.updated_at = utcnow()
    session.flush()
    return _view(row)


def _check_repeated_operation(row, owner_key, kind, payload):
    if row.owner_key != owner_key or row.kind != kind or row.payload != payload:
        raise DomainError("CFG_002", message="Idempotency key was already used for a different request.")


def _scope_for(session: Session, kind: str, payload: dict) -> dict:
    claim_ids = list(payload.get("claim_ids") or [])
    version_ids = list(payload.get("paper_version_ids") or [])
    paper_ids = list(payload.get("paper_ids") or [])
    if claim_ids and not version_ids:
        version_ids = list(
            session.scalars(
                select(ClaimRow.paper_version_id).where(ClaimRow.claim_id.in_(claim_ids))
            )
        )
    if version_ids and not paper_ids:
        paper_ids = list(
            session.scalars(
                select(PaperVersionRow.paper_id).where(
                    PaperVersionRow.paper_version_id.in_(version_ids)
                )
            )
        )
    scope: dict[str, Any] = {
        "paper_version_ids": sorted(set(version_ids)),
        "claim_ids": sorted(set(claim_ids)),
        "paper_ids": sorted(set(paper_ids)),
        "kind": kind,
    }
    if kind in ("cancel_job", "resume_job") and payload.get("job_id"):
        scope["job_id"] = payload["job_id"]
    if kind == "replay_task" and payload.get("task_id"):
        scope["task_id"] = payload["task_id"]
    if kind == "corpus_refresh" and payload.get("collection_id"):
        scope["collection_id"] = payload["collection_id"]
    return scope


def _reverify(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
    """Queue a re-verification job (docs/03 §S06).

    The operation returns ACCEPTED with the job it created: the scientific
    support state is refreshed when that job actually completes, never by this
    response. Running the verifiers inline would make the HTTP 202 a lie and
    would block the review workbench.
    """
    from paperintel.database.models import ClaimRow
    from paperintel.schemas.enums import PipelineStage, ResourceTier
    from paperintel.workflow import engine

    claim_ids = list(payload.get("claim_ids") or [])
    if not claim_ids:
        raise DomainError("CFG_002", message="reverify requires claim_ids.", details={})

    results: list[dict] = []
    job_ids: list[str] = []
    # One job per version: claims of the same version share the verification run.
    by_version: dict[str, list[str]] = {}
    for claim_id in claim_ids:
        claim = session.get(ClaimRow, claim_id)
        if claim is None:
            results.append({"target_id": claim_id, "status": "SKIPPED", "error_code": "CFG_002"})
            continue
        by_version.setdefault(claim.paper_version_id, []).append(claim_id)

    for version_id, version_claim_ids in by_version.items():
        version = session.get(PaperVersionRow, version_id)
        if version is None:  # pragma: no cover - guarded by the claim FK
            for claim_id in version_claim_ids:
                results.append(
                    {"target_id": claim_id, "status": "SKIPPED", "error_code": "CFG_002"}
                )
            continue
        job = engine.create_job(
            session,
            paper_id=version.paper_id,
            paper_version_id=version_id,
            requested_tier=ResourceTier.T3_DEEP,
            current_stage=PipelineStage.ANALYZED,
        )
        engine.replay_stage(
            session,
            job.job_id,
            PipelineStage.VERIFIED,
            input_manifest={**engine.build_base_manifest(
                paper_version_id=version_id,
                content_sha256=version.content_sha256,
                data_dir=str(settings.core.data_dir),
                report_path=str(
                    Path(settings.core.data_dir)
                    / "cache"
                    / "extraction_reports"
                    / f"{version.content_sha256}.json"
                ),
            ), "claim_ids": sorted(set(version_claim_ids)), "tier": ResourceTier.T3_DEEP.value},
        )
        job_ids.append(job.job_id)
        for claim_id in version_claim_ids:
            results.append(
                {
                    "target_id": claim_id,
                    "status": "ACCEPTED",
                    "error_code": None,
                    "job_id": job.job_id,
                }
            )

    state = "ACCEPTED" if job_ids else "FAILED"
    return results, job_ids, state, None if job_ids else "CFG_002"


def _reanalyze(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
    from paperintel.schemas.enums import PipelineStage
    from paperintel.workflow import engine

    version_ids = list(payload.get("paper_version_ids") or [])
    if not version_ids:
        raise DomainError("CFG_002", message="reanalyze requires paper_version_ids.", details={})
    results: list[dict] = []
    job_ids: list[str] = []
    for version_id in version_ids:
        version = session.get(PaperVersionRow, version_id)
        if version is None:
            results.append({"target_id": version_id, "status": "SKIPPED", "error_code": "CFG_002"})
            continue
        job = engine.create_job(
            session,
            paper_id=version.paper_id,
            paper_version_id=version_id,
            current_stage=PipelineStage.ANALYZED,
        )
        report_path = (
            Path(settings.core.data_dir)
            / "cache"
            / "extraction_reports"
            / f"{version.content_sha256}.json"
        )
        engine.plan_job(
            session,
            job,
            data_dir=str(settings.core.data_dir),
            content_sha256=version.content_sha256,
            report_path=str(report_path),
        )
        job_ids.append(job.job_id)
        results.append({"target_id": version_id, "status": "ACCEPTED", "error_code": None})
    return results, job_ids, "ACCEPTED", None


def _set_tier(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
    from paperintel.schemas.enums import ResourceTier
    from paperintel.triage.service import compute_triage

    tier_raw = payload.get("tier")
    try:
        tier = ResourceTier(str(tier_raw))
    except ValueError as exc:
        raise DomainError(
            "CFG_002", message=f"Unknown tier: {tier_raw!r}", details={"tier": tier_raw}
        ) from exc
    results: list[dict] = []
    for paper_id in payload.get("paper_ids") or []:
        if session.get(PaperRow, paper_id) is None:
            results.append({"target_id": paper_id, "status": "SKIPPED", "error_code": "CFG_002"})
            continue
        compute_triage(session, paper_id=paper_id, requested_tier=tier)
        results.append({"target_id": paper_id, "status": "COMPLETED", "error_code": None})
    return results, [], "COMPLETED", None


def _job_action(action: str):
    def _handler(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
        from paperintel.workflow import engine

        job_id = payload.get("job_id")
        if not job_id:
            raise DomainError("CFG_002", message=f"{action} requires job_id.", details={})
        if action == "cancel_job":
            job = engine.cancel_job(session, job_id)
            state = job.state.value
        else:
            engine.resume_job(session, job_id)
            state = "QUEUED"
        return (
            [{"target_id": job_id, "status": state, "error_code": None}],
            [job_id],
            "ACCEPTED",
            None,
        )

    return _handler


def _replay_task(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
    from paperintel.database.models import TaskRow
    from paperintel.workflow.celery_app import submit_task

    task_id = payload.get("task_id")
    if not task_id:
        raise DomainError("CFG_002", message="replay_task requires task_id.", details={})
    task = session.get(TaskRow, task_id)
    if task is None:
        raise DomainError(
            "CFG_002", message=f"Unknown task: {task_id}", details={"task_id": task_id}
        )
    if task.state.value in (
        "SUCCEEDED",
        "SUCCEEDED_WITH_WARNINGS",
        "FAILED",
        "CANCELLED",
        "SKIPPED",
    ):
        # Same rule as POST /v1/tasks/{task_id}/replay: a terminal task is not
        # replayed in place — the client asks for a fresh replay task instead.
        raise DomainError(
            "INTERNAL_002",
            message=(
                f"Task {task_id} is terminal ({task.state.value}); use the job replay "
                "endpoint to create a fresh replay task."
            ),
            details={"task_id": task_id, "state": task.state.value},
        )
    submit_task(task_id, database_url=settings.database.url)
    return (
        [{"target_id": task_id, "status": "ACCEPTED", "error_code": None}],
        [],
        "ACCEPTED",
        None,
    )


def _gc_preview(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
    """Preview candidates WITHOUT deleting anything (docs/03 §S10, docs/09 §GC).

    The preview records a scope hash and an expiry on the operation row; execution
    must quote that scope back, and an expired or mismatched scope is refused so a
    stale preview can never delete current objects.
    """
    import hashlib
    from datetime import timedelta

    from paperintel.operations.gc import run_gc
    from paperintel.schemas.common import utcnow

    report = run_gc(session, settings.core.data_dir, dry_run=True, settings=settings)
    candidate_paths = sorted(candidate.object_path for candidate in report.candidates)
    scope_hash = hashlib.sha256("|".join(candidate_paths).encode("utf-8")).hexdigest()[:16]
    expires_at = utcnow() + timedelta(minutes=15)
    results = [
        {
            "target_id": candidate.object_path,
            "status": f"{candidate.size_bytes} bytes ({candidate.retention_class})",
            "error_code": None,
            "reason": candidate.reason,
        }
        for candidate in report.candidates
    ]
    results.append(
        {
            "target_id": "SCOPE",
            "status": scope_hash,
            "error_code": None,
            "reason": expires_at.isoformat(),
        }
    )
    session.info["gc_preview"] = {
        "scope_hash": scope_hash,
        "expires_at": expires_at.isoformat(),
        "candidate_ids": candidate_paths,
        "total_bytes": report.candidate_bytes,
    }
    return results, [], "COMPLETED", None


def _gc_execute(session: Session, payload: dict, settings) -> tuple[list, list, str, str | None]:
    """Execute a previewed GC scope (docs/09 §GC).

    Second check at execution time: the preview must exist, must not be expired,
    and the scope hash must still match the live candidate set. Ordinary GC never
    removes PDFs, evidence or notes — the engine only offers non-KEEP objects.
    """
    import hashlib
    from datetime import datetime

    from paperintel.errors.ui_errors import UiError
    from paperintel.operations.gc import run_gc
    from paperintel.schemas.common import utcnow

    preview_id = payload.get("preview_id")
    if not preview_id:
        raise DomainError(
            "CFG_002",
            message="gc_execute requires the preview_id it was confirmed for.",
            details={},
        )
    preview = session.get(UiOperationRequestRow, preview_id)
    if preview is None or preview.kind != "gc_preview":
        raise DomainError(
            "CFG_002",
            message="gc_execute must reference an existing gc_preview operation.",
            details={"preview_id": preview_id},
        )
    scope = preview.scope or {}
    stored_hash = scope.get("scope_hash")
    expires_raw = scope.get("expires_at")
    if not stored_hash or not expires_raw:
        raise UiError(
            "SCOPE_EXPIRED",
            message="该预览没有可复核的范围，请重新预览后再执行。",
            details={"preview_id": preview_id},
        )
    if datetime.fromisoformat(str(expires_raw)) <= utcnow():
        raise UiError(
            "SCOPE_EXPIRED",
            message="该预览已过期，请重新预览后再执行。",
            details={"preview_id": preview_id, "expires_at": str(expires_raw)},
        )

    dry = run_gc(session, settings.core.data_dir, dry_run=True, settings=settings)
    current_paths = sorted(candidate.object_path for candidate in dry.candidates)
    current_hash = hashlib.sha256("|".join(current_paths).encode("utf-8")).hexdigest()[:16]
    if payload.get("scope_hash") and payload["scope_hash"] != current_hash:
        raise UiError(
            "SCOPE_EXPIRED",
            message="对象集合已变化，请重新预览后再执行。",
            details={"expected": payload["scope_hash"], "current": current_hash},
        )
    if current_hash != stored_hash:
        raise UiError(
            "SCOPE_EXPIRED",
            message="对象集合自预览以来已变化，请重新预览后再执行。",
            details={"preview_scope_hash": stored_hash, "current_scope_hash": current_hash},
        )

    report = run_gc(session, settings.core.data_dir, dry_run=False, settings=settings)
    results = [
        {"target_id": path, "status": "REMOVED", "error_code": None} for path in report.removed
    ] + [
        {"target_id": error, "status": "FAILED", "error_code": "STORAGE_001"}
        for error in report.errors
    ]
    return results, [], "COMPLETED" if not report.errors else "PARTIAL", None


def _export(session: Session, payload: dict, settings, *, owner_key="local") -> tuple[list, list, str, str | None]:
    """Export a bundle for the requested versions (JSON manifest + sizes).

    PDF binaries and raw model output are NOT included unless explicitly
    requested (docs/06 §审查与外部Agent).
    """
    version_ids = list(payload.get("paper_version_ids") or [])
    if not version_ids:
        raise DomainError("CFG_002", message="export requires paper_version_ids.", details={})
    include = set(payload.get("include") or ["brief", "claims", "evidence_refs"])
    if include - {"brief", "claims", "evidence_refs", "notes"}:
        raise DomainError("CFG_002", message="Unsupported export content selection.")
    bundle: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": utcnow().isoformat(),
        "include": sorted(include),
        "papers": [],
    }
    results: list[dict] = []
    for version_id in version_ids:
        version = session.get(PaperVersionRow, version_id)
        if version is None:
            results.append({"target_id": version_id, "status": "SKIPPED", "error_code": "CFG_002"})
            continue
        paper = session.get(PaperRow, version.paper_id)
        claims = list(
            session.scalars(select(ClaimRow).where(ClaimRow.paper_version_id == version_id))
        )
        bundle["papers"].append(
            {
                "paper_id": version.paper_id,
                "paper_version_id": version_id,
                "title": paper.canonical_title if paper else "",
                "document_sha256": version.content_sha256,
                "claims": [
                    {
                        "claim_id": claim.claim_id,
                        "statement": claim.statement,
                        "claim_type": claim.claim_type.value,
                        "support_state": claim.support_state.value,
                    }
                    for claim in claims
                ]
                if "claims" in include
                else [],
            }
        )
        results.append({"target_id": version_id, "status": "COMPLETED", "error_code": None})
        entry = bundle["papers"][-1]
        if "brief" in include:
            entry["brief"] = {"title": paper.canonical_title if paper else "", "doi": paper.doi if paper else None}
        if "evidence_refs" in include:
            from paperintel.database.models import ClaimEvidenceRow

            entry["evidence_refs"] = [
                {"claim_id": link.claim_id, "evidence_id": link.evidence_id, "role": link.role.value}
                for link in session.scalars(select(ClaimEvidenceRow).where(
                    ClaimEvidenceRow.claim_id.in_([claim.claim_id for claim in claims])))
            ]
        if "notes" in include:
            from paperintel.database.models import UiNoteRow

            entry["notes"] = [
                {"note_id": note.note_id, "body": note.body, "revision": note.revision}
                for note in session.scalars(select(UiNoteRow).where(
                    UiNoteRow.paper_version_id == version_id, UiNoteRow.owner_key == owner_key))
            ]
    if not bundle["papers"]:
        return results, [], "FAILED", "CFG_002"
    export_dir = Path(settings.core.data_dir) / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"export-{new_ui_operation_id()}.json"
    target.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    results.append(
        {"target_id": str(target), "status": f"{target.stat().st_size} bytes", "error_code": None}
    )
    return results, [], "PARTIAL" if any(item["status"] == "SKIPPED" for item in results) else "COMPLETED", None


def _corpus_refresh(
    session: Session, payload: dict, settings
) -> tuple[list, list, str, str | None]:
    from paperintel.corpus.intelligence import topic_landscape

    collection_id = payload.get("collection_id")
    if not collection_id:
        raise DomainError("CFG_002", message="corpus_refresh requires collection_id.", details={})
    view = topic_landscape(session, collection_id=collection_id)
    return (
        [
            {
                "target_id": view.run_id,
                "status": f"{len(view.findings)} findings",
                "error_code": None,
            }
        ],
        [],
        "COMPLETED",
        None,
    )


_HANDLERS = {
    "reverify": _reverify,
    "reanalyze": _reanalyze,
    "set_tier": _set_tier,
    "cancel_job": _job_action("cancel_job"),
    "resume_job": _job_action("resume_job"),
    "replay_task": _replay_task,
    "gc_preview": _gc_preview,
    "gc_execute": _gc_execute,
    "export": _export,
    "corpus_refresh": _corpus_refresh,
}


def _view(row: UiOperationRequestRow) -> dict[str, Any]:
    return {
        "operation_id": row.operation_id,
        "kind": row.kind,
        "state": row.state,
        "scope": row.scope or {},
        "job_ids": list(row.job_ids or []),
        "results": list(row.results or []),
        "error_code": row.error_code,
    }


def operation_view(
    session: Session, operation_id: str, *, owner_key: str = "local"
) -> dict[str, Any]:
    row = session.get(UiOperationRequestRow, operation_id)
    if row is None or row.owner_key != owner_key:
        raise DomainError(
            "CFG_002",
            message=f"Unknown operation: {operation_id}",
            details={"operation_id": operation_id},
        )
    return _view(row)


def save_search(
    session: Session, *, name: str, query: dict, owner_key: str = "local"
) -> UiSavedSearchRow:
    if not name.strip():
        raise DomainError("CFG_002", message="Saved search needs a name.", details={})
    # Validate the stored query against the current schema, then persist the
    # FULL filter structure (never a transient cursor).
    model = LibraryQuery.model_validate(query) if query else LibraryQuery()
    payload = model.model_dump(mode="json")
    payload["cursor"] = None
    row = UiSavedSearchRow(
        saved_search_id=new_ui_saved_search_id(),
        owner_key=owner_key,
        name=name.strip(),
        query=payload,
        schema_version="1.0.0",
    )
    session.add(row)
    session.flush()
    return row


def update_saved_search(
    session: Session,
    saved_search_id: str,
    *,
    name: str | None = None,
    query: dict | None = None,
    expected_revision: int | None = None,
    owner_key: str = "local",
) -> UiSavedSearchRow:
    """Rename/re-scope a saved search; a lost race is a conflict, not an overwrite."""
    row = session.get(UiSavedSearchRow, saved_search_id)
    if row is None or row.owner_key != owner_key:
        raise DomainError(
            "CFG_002",
            message=f"Unknown saved search: {saved_search_id}",
            details={"saved_search_id": saved_search_id},
        )
    if expected_revision is not None and expected_revision != row.revision:
        raise UiError(
            "REVISION_CONFLICT",
            message="This saved search changed elsewhere.",
            details={"expected_revision": expected_revision, "current_revision": row.revision},
        )
    if name is not None:
        if not name.strip():
            raise DomainError("CFG_002", message="Saved search needs a name.", details={})
        row.name = name.strip()
    if query is not None:
        # A saved search stores the FULL filter structure and never a transient
        # cursor (docs/06 §专题与实体).
        model = LibraryQuery.model_validate(query)
        payload = model.model_dump(mode="json")
        payload["cursor"] = None
        row.query = payload
    row.revision = row.revision + 1
    row.updated_at = utcnow()
    session.flush()
    return row


def delete_saved_search(
    session: Session, saved_search_id: str, *, owner_key: str = "local"
) -> None:
    row = session.get(UiSavedSearchRow, saved_search_id)
    if row is None or row.owner_key != owner_key:
        raise DomainError(
            "CFG_002",
            message=f"Unknown saved search: {saved_search_id}",
            details={"saved_search_id": saved_search_id},
        )
    session.delete(row)
    session.flush()


def list_saved(session: Session, *, owner_key: str = "local") -> list[dict]:
    rows = session.scalars(
        select(UiSavedSearchRow)
        .where(UiSavedSearchRow.owner_key == owner_key)
        .order_by(UiSavedSearchRow.name)
    )
    return [
        {
            "saved_search_id": row.saved_search_id,
            "name": row.name,
            "query": row.query,
            "revision": row.revision,
        }
        for row in rows
    ]


def save_diagnostics(
    session: Session, *, payload: dict, owner_key: str, settings
) -> dict[str, Any]:
    """Persist a REDACTED client manifest (≤2 MiB) and return a reference.

    The default manifest carries NO request bodies, notes, search text, tokens,
    authorization headers or cookies (docs/09 §UI Inspector). An evidence excerpt
    is only included when the caller explicitly asks for it, and the response
    reports what was included so the user can see it before sharing.
    """
    from paperintel.operations.debug import redact

    max_bytes = 2 * 1024 * 1024
    include_excerpt = bool(payload.get("include_evidence_excerpt"))
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    # Keep the most recent 200 client events, as the contract specifies.
    events = events[-200:]

    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "generated_at": utcnow().isoformat(),
        "client": redact(client),
        "events": redact(events),
        "included": {
            "request_bodies": False,
            "notes": False,
            "search_text": False,
            "credentials": False,
            "evidence_excerpt": include_excerpt,
        },
        "truncated_events": max(0, len(payload.get("events") or []) - len(events)),
    }
    if include_excerpt:
        excerpt = payload.get("evidence_excerpt")
        manifest["evidence_excerpt"] = redact(excerpt) if excerpt else None
        manifest["included"]["evidence_excerpt_preview"] = "证据摘录已包含；分享前请确认内容"

    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > max_bytes:
        raise DomainError(
            "STORAGE_001",
            message=f"Diagnostic manifest exceeds {max_bytes} bytes.",
            details={"size_bytes": len(encoded), "limit": max_bytes},
        )
    directory = Path(settings.core.data_dir) / "debug" / "ui"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d%H%M%S")
    target = directory / f"ui-diagnostics-{stamp}.json"
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "asset_id": target.name,
        "path": str(target),
        "size_bytes": target.stat().st_size,
        "max_bytes": max_bytes,
        "redacted_fields": ["authorization", "cookie", "token", "api_key"],
        "manifest": manifest,
    }


__all__ = [
    "OPERATION_KINDS",
    "delete_saved_search",
    "list_saved",
    "operation_view",
    "save_diagnostics",
    "save_search",
    "submit_operation",
    "update_saved_search",
]

"""api.ui_routes.operations — review queue, review decisions, operations, snapshot."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from paperintel.api.ui_routes.deps import (
    UiIdentity,
    data_dir,
    envelope,
    page_envelope,
    require_csrf,
    settings_of,
    ui_identity,
)
from paperintel.services.ui import personal
from paperintel.services.ui import workspace as workspace_service

router = APIRouter(prefix="/v1/ui", tags=["ui-operations"])


def _session(request: Request):
    return request.app.state.paperintel.session_factory()


@router.post("/review/query", response_model=None)
def review_query(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    """The review queue (docs/06 §审查与外部Agent).

    ``include_all`` returns every claim (with its real verification state) so
    "no risky items" is never confused with "the audit service returned
    nothing"; ``include_seen`` brings back items a human already looked at.
    """
    from paperintel.schemas.ui.models import LibraryQuery
    from paperintel.services.ui import library as library_service
    from paperintel.services.ui import review as review_service

    limit = int(payload.get("limit") or 50)
    offset = int(payload.get("offset") or 0)
    session = _session(request)
    try:
        items, total = review_service.query_review(
            session,
            collection_id=payload.get("collection_id"),
            groups=payload.get("groups") or None,
            include_seen=bool(payload.get("include_seen")),
            include_all=bool(payload.get("include_all")),
            paper_version_id=payload.get("paper_version_id"),
            limit=limit,
            offset=offset,
        )
        page = library_service.LibraryPage(
            paper_ids=[],
            next_cursor=None,
            has_more=offset + len(items) < total,
            total=total,
            total_kind="EXACT",
            scope_revision=library_service.scope_revision(session),
            filter_hash=library_service.filter_hash(LibraryQuery()),
        )
        return page_envelope(
            page,
            [item.model_dump(mode="json") for item in items],
            kind="REVIEW_ITEMS",
        )
    finally:
        session.close()


@router.post("/review/decisions", response_model=None)
def review_decision(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Record a human decision. support_state is untouched by construction."""
    session = _session(request)
    try:
        row, created = personal.record_review_decision(
            session,
            claim_id=payload.get("claim_id", ""),
            decision=payload.get("decision", ""),
            note=payload.get("note"),
            idempotency_key=payload.get("idempotency_key"),
            expected_claim_revision=payload.get("expected_claim_revision"),
            owner_key=identity.owner_key,
        )
        session.commit()
        return envelope(
            {
                "decision_id": row.decision_id,
                "claim_id": row.claim_id,
                "paper_version_id": row.paper_version_id,
                "decision": row.decision,
                "note": row.note,
                "created_at": row.created_at.isoformat(),
                "created": created,
            }
        )
    finally:
        session.close()


@router.get("/operations/snapshot", response_model=None)
def operations_snapshot(
    request: Request,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    session = _session(request)
    try:
        snapshot = workspace_service.operations_snapshot(session, settings=settings_of(request))
        return envelope(snapshot.model_dump(mode="json"))
    finally:
        session.close()


@router.post("/operations", response_model=None, status_code=202)
def create_operation(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Enqueue a supported operation.

    The kind is validated against an explicit table: no arbitrary function
    names, no shell, and every batch reports per-target results. 202 says the
    work was ACCEPTED, never that it succeeded (docs/06 §写任务).
    """
    from paperintel.services.ui.operations_service import submit_operation

    session = _session(request)
    try:
        view = submit_operation(
            session,
            kind=payload.get("kind", ""),
            payload=payload.get("payload") or {},
            idempotency_key=payload.get("idempotency_key"),
            owner_key=identity.owner_key,
            settings=settings_of(request),
        )
        session.commit()
        return envelope(view)
    finally:
        session.close()


@router.get("/operations/{operation_id}", response_model=None)
def get_operation(
    request: Request,
    operation_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    from paperintel.services.ui.operations_service import operation_view

    session = _session(request)
    try:
        return envelope(operation_view(session, operation_id, owner_key=identity.owner_key))
    finally:
        session.close()


@router.get("/operations/{operation_id}/download", response_model=None)
def download_export(
    request: Request,
    operation_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
):
    from pathlib import Path

    from fastapi.responses import FileResponse

    from paperintel.errors import DomainError
    from paperintel.services.ui.operations_service import operation_view

    with _session(request) as session:
        operation = operation_view(session, operation_id, owner_key=identity.owner_key)
        if operation["kind"] != "export" or operation["state"] not in {"COMPLETED", "PARTIAL"}:
            raise DomainError("CFG_002", message="This operation has no completed export.")
        directory = (data_dir(request) / "exports").resolve()
        for result in operation["results"]:
            path = Path(result["target_id"])
            if path.name.startswith("export-") and path.suffix == ".json":
                if path.resolve().parent == directory and path.is_file():
                    return FileResponse(path, media_type="application/json", filename=path.name)
        raise DomainError("STORAGE_003", message="The exported file is no longer available.")


@router.post("/handoffs", response_model=None, status_code=202)
def create_handoff(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Build an external-agent handoff bundle (docs/06 §审查与外部Agent, S12).

    The scope is explicit, the size limits are enforced before writing, and the
    manifest carries the version ids and source hashes so the receiving agent can
    verify what it received. PDF binaries and raw model output are excluded
    unless the user asks for them by name.
    """
    from paperintel.services.ui.handoffs import build_handoff

    session = _session(request)
    try:
        bundle = build_handoff(
            session,
            paper_version_ids=list(payload.get("paper_version_ids") or []),
            include=list(payload.get("include") or ["brief", "claims", "evidence_refs"]),
            data_dir=data_dir(request),
            limit=payload.get("limit"),
            max_bytes=payload.get("max_bytes"),
            owner_key=identity.owner_key,
        )
        return envelope(
            {
                "asset_id": bundle.asset_id,
                "size_bytes": bundle.size_bytes,
                "expires_at": None,
                "redacted_fields": [],
                "manifest": bundle.manifest,
            }
        )
    finally:
        session.close()


@router.get("/saved-searches", response_model=None)
def list_saved_searches(
    request: Request,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    from paperintel.services.ui.operations_service import list_saved

    session = _session(request)
    try:
        return envelope({"items": list_saved(session, owner_key=identity.owner_key)})
    finally:
        session.close()


@router.post("/saved-searches", response_model=None)
def create_saved_search(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    from paperintel.services.ui.operations_service import save_search

    session = _session(request)
    try:
        row = save_search(
            session,
            name=payload.get("name", ""),
            query=payload.get("query") or {},
            owner_key=identity.owner_key,
        )
        session.commit()
        return envelope(
            {
                "saved_search_id": row.saved_search_id,
                "name": row.name,
                "query": row.query,
                "revision": row.revision,
            }
        )
    finally:
        session.close()


@router.patch("/saved-searches/{saved_search_id}", response_model=None)
def patch_saved_search(
    request: Request,
    saved_search_id: str,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Rename or re-scope a saved search with an optimistic-concurrency guard."""
    from paperintel.services.ui.operations_service import update_saved_search

    session = _session(request)
    try:
        row = update_saved_search(
            session,
            saved_search_id,
            name=payload.get("name"),
            query=payload.get("query"),
            expected_revision=payload.get("expected_revision"),
            owner_key=identity.owner_key,
        )
        session.commit()
        return envelope(
            {
                "saved_search_id": row.saved_search_id,
                "name": row.name,
                "query": row.query,
                "revision": row.revision,
            }
        )
    finally:
        session.close()


@router.delete("/saved-searches/{saved_search_id}", response_model=None)
def delete_saved_search(
    request: Request,
    saved_search_id: str,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    from paperintel.services.ui.operations_service import delete_saved_search

    session = _session(request)
    try:
        delete_saved_search(session, saved_search_id, owner_key=identity.owner_key)
        session.commit()
        return envelope({"deleted": True, "target_id": saved_search_id})
    finally:
        session.close()


@router.post("/diagnostics", response_model=None)
def create_diagnostics(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Persist a redacted client manifest and return a download reference."""
    from paperintel.services.ui.operations_service import save_diagnostics

    session = _session(request)
    try:
        view = save_diagnostics(
            session, payload=payload, owner_key=identity.owner_key, settings=settings_of(request)
        )
        session.commit()
        return envelope(view)
    finally:
        session.close()

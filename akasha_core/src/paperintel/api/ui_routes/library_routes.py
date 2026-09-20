"""api.ui_routes.library — library query, workspace, versions, personal, notes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from paperintel.api.ui_routes.deps import (
    UiIdentity,
    data_dir,
    envelope,
    page_envelope,
    require_csrf,
    settings_of,
    ui_identity,
)
from paperintel.schemas.ui.models import LibraryQuery
from paperintel.services.ui import personal
from paperintel.services.ui import workspace as workspace_service

router = APIRouter(prefix="/v1/ui", tags=["ui-library"])


def _session(request: Request):
    return request.app.state.paperintel.session_factory()


@router.post("/library/query", response_model=None)
def library_query(
    request: Request,
    payload: LibraryQuery,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    """Typed library search. A CLAIM hit is returned as a claim, never as a
    paper card (docs/06 §文献库)."""
    from paperintel.services.ui import library as library_service
    from paperintel.services.ui.imports import UploadLimits

    page_size = UploadLimits.from_settings(settings_of(request)).library_page_size
    session = _session(request)
    try:
        if payload.kind == "PAPERS":
            items, page = workspace_service.list_papers(
                session, payload, data_dir=data_dir(request), default_page=page_size
            )
            return page_envelope(
                page,
                [item.model_dump(mode="json") for item in items],
                kind="PAPERS",
            )
        if payload.kind == "CLAIMS":
            claims = library_service.claim_candidates(
                session, payload, limit=payload.limit or page_size
            )
            counts = workspace_service._evidence_counts(
                session, [claim.claim_id for claim in claims]
            )
            items = [
                {
                    "claim_id": claim.claim_id,
                    "statement": claim.statement,
                    "claim_type": claim.claim_type.value,
                    "support_state": claim.support_state.value,
                    "paper_version_id": claim.paper_version_id,
                    "paper_id": claim.paper_id,
                    "evidence_count": counts.get(claim.claim_id, 0),
                }
                for claim in claims
            ]
            page = library_service.LibraryPage(
                paper_ids=[],
                next_cursor=None,
                has_more=False,
                total=len(items),
                total_kind="EXACT",
                scope_revision=library_service.scope_revision(session),
                filter_hash=library_service.filter_hash(payload),
            )
            return page_envelope(page, items, kind="CLAIMS")
        techniques = library_service.technique_candidates(
            session, payload, limit=payload.limit or page_size
        )
        page = library_service.LibraryPage(
            paper_ids=[],
            next_cursor=None,
            has_more=False,
            total=len(techniques),
            total_kind="EXACT",
            scope_revision=library_service.scope_revision(session),
            filter_hash=library_service.filter_hash(payload),
        )
        return page_envelope(page, techniques, kind="TECHNIQUES")
    finally:
        session.close()


@router.get("/papers/{paper_id}/workspace", response_model=None)
def get_workspace(
    request: Request,
    paper_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
    paper_version_id: str | None = Query(default=None),
) -> dict:
    session = _session(request)
    try:
        model = workspace_service.workspace(
            session, paper_id, paper_version_id=paper_version_id, data_dir=data_dir(request)
        )
        return envelope(model.model_dump(mode="json"))
    finally:
        session.close()


@router.get("/papers/{paper_id}/versions", response_model=None)
def get_versions(
    request: Request,
    paper_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    session = _session(request)
    try:
        model = workspace_service.workspace(
            session, paper_id, paper_version_id=None, data_dir=data_dir(request)
        )
        return envelope({"items": [version.model_dump(mode="json") for version in model.versions]})
    finally:
        session.close()


@router.get("/papers/{paper_id}/jobs", response_model=None)
def get_paper_jobs(
    request: Request,
    paper_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
    paper_version_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> dict:
    """Jobs for ONE paper/version, scoped in SQL before the limit."""
    from sqlalchemy import select

    from paperintel.database.models import JobRow
    from paperintel.schemas.ui.models import LibraryQuery as _LQ
    from paperintel.services.ui import library as library_service

    session = _session(request)
    try:
        stmt = select(JobRow).where(JobRow.paper_id == paper_id)
        if paper_version_id:
            stmt = stmt.where(JobRow.paper_version_id == paper_version_id)
        jobs = list(
            session.scalars(stmt.order_by(JobRow.created_at.desc(), JobRow.job_id).limit(100))
        )
        items = [
            {
                "job_id": job.job_id,
                "paper_id": job.paper_id,
                "paper_version_id": job.paper_version_id,
                "state": job.state.value,
                "current_stage": job.current_stage.value,
                "tasks_completed": 0,
                "tasks_planned": 0,
                "eta": {
                    "p50_seconds": None,
                    "p90_seconds": None,
                    "includes_queue": True,
                    "sample_count": 0,
                    "reason": "NOT_MEASURED",
                },
            }
            for job in jobs
        ]
        page = library_service.LibraryPage(
            paper_ids=[],
            next_cursor=None,
            has_more=False,
            total=len(items),
            total_kind="EXACT",
            scope_revision=library_service.scope_revision(session),
            filter_hash=library_service.filter_hash(_LQ()),
        )
        return page_envelope(page, items, kind="JOBS")
    finally:
        session.close()


@router.patch("/papers/{paper_id}/personal", response_model=None)
def patch_personal(
    request: Request,
    paper_id: str,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = _session(request)
    try:
        result = personal.patch_personal_state(
            session,
            paper_id,
            saved=payload.get("saved"),
            read_state=payload.get("read_state"),
            reading_anchor=payload.get("reading_anchor"),
            expected_revision=payload.get("expected_revision"),
            owner_key=identity.owner_key,
        )
        session.commit()
        return envelope(
            {
                "paper_id": result.paper_id,
                "saved": result.saved,
                "read_state": result.read_state,
                "revision": result.revision,
                "reading_anchor": result.reading_anchor,
            }
        )
    finally:
        session.close()


@router.get("/papers/{paper_id}/notes", response_model=None)
def list_notes(
    request: Request,
    paper_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
    paper_version_id: str | None = Query(default=None),
) -> dict:
    session = _session(request)
    try:
        rows = personal.list_notes(
            session,
            paper_id=paper_id,
            paper_version_id=paper_version_id,
            owner_key=identity.owner_key,
        )
        return envelope({"items": [_note(row) for row in rows]})
    finally:
        session.close()


@router.post("/notes", response_model=None)
def create_note(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = _session(request)
    try:
        row = personal.create_note(
            session,
            paper_id=payload.get("paper_id", ""),
            paper_version_id=payload.get("paper_version_id", ""),
            body=payload.get("body", ""),
            claim_id=payload.get("claim_id"),
            evidence_id=payload.get("evidence_id"),
            owner_key=identity.owner_key,
            paper_wide=payload.get("paper_wide") is True,
        )
        session.commit()
        return envelope(_note(row))
    finally:
        session.close()


@router.patch("/notes/{note_id}", response_model=None)
def patch_note(
    request: Request,
    note_id: str,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = _session(request)
    try:
        row = personal.patch_note(
            session,
            note_id,
            body=payload.get("body"),
            expected_revision=payload.get("expected_revision"),
            owner_key=identity.owner_key,
        )
        session.commit()
        return envelope(_note(row))
    finally:
        session.close()


@router.delete("/notes/{note_id}", response_model=None)
def delete_note(
    request: Request,
    note_id: str,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = _session(request)
    try:
        personal.delete_note(session, note_id, owner_key=identity.owner_key)
        session.commit()
        return envelope({"deleted": True, "target_id": note_id})
    finally:
        session.close()


def _note(row) -> dict:
    return {
        "note_id": row.note_id,
        "paper_id": row.paper_id,
        "paper_version_id": row.paper_version_id,
        "claim_id": row.claim_id,
        "evidence_id": row.evidence_id,
        "body": row.body,
        "revision": row.revision,
        "updated_at": row.updated_at.isoformat(),
    }

"""api.ui_routes.collections_routes — collections, compare and entities (F06)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from paperintel.api.ui_routes.deps import (
    UiIdentity,
    envelope,
    page_envelope,
    require_csrf,
    ui_identity,
)
from paperintel.schemas.ui.models import LibraryQuery
from paperintel.services.ui import library as library_service

router = APIRouter(prefix="/v1/ui", tags=["ui-collections"])


def _session(request: Request):
    return request.app.state.paperintel.session_factory()


@router.post("/collections", response_model=None)
def create_collection(request: Request, payload: dict,
                      identity: Annotated[UiIdentity, Depends(require_csrf)]) -> dict:
    from paperintel.knowledge.collections import create_collection as create

    with _session(request) as session:
        row = create(session, name=str(payload.get("name") or ""))
        session.commit()
        return envelope({"collection_id": row.collection_id, "name": row.name})


@router.post("/collections/{collection_id}/papers/{paper_id}", response_model=None)
def add_collection_paper(request: Request, collection_id: str, paper_id: str,
                         identity: Annotated[UiIdentity, Depends(require_csrf)]) -> dict:
    from paperintel.knowledge.collections import add_paper

    with _session(request) as session:
        _, changed = add_paper(session, collection_id=collection_id, paper_id=paper_id)
        session.commit()
        return envelope({"changed": changed})


@router.delete("/collections/{collection_id}/papers/{paper_id}", response_model=None)
def remove_collection_paper(request: Request, collection_id: str, paper_id: str,
                            identity: Annotated[UiIdentity, Depends(require_csrf)]) -> dict:
    from paperintel.knowledge.collections import remove_paper

    with _session(request) as session:
        removed = remove_paper(session, collection_id=collection_id, paper_id=paper_id)
        session.commit()
        return envelope({"removed": removed})


@router.get("/collections/{collection_id}/workspace", response_model=None)
def get_collection_workspace(
    request: Request,
    collection_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    from paperintel.services.ui import collections_ws

    session = _session(request)
    try:
        return envelope(collections_ws.collection_workspace(session, collection_id))
    finally:
        session.close()


@router.post("/compare", response_model=None)
def post_compare(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    """Side-by-side comparison of 2–4 versions (docs/03 §S09).

    Cells carry a value only where a stored claim exists, and quantify only when
    the protocol keys agree; otherwise the cell explains why it cannot be
    compared instead of producing a number.
    """
    from paperintel.services.ui import collections_ws

    session = _session(request)
    try:
        return envelope(
            collections_ws.compare(session, list(payload.get("paper_version_ids") or []))
        )
    finally:
        session.close()


@router.post("/entities/query", response_model=None)
def post_entities_query(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    from paperintel.services.ui import collections_ws

    limit = int(payload.get("limit") or 50)
    session = _session(request)
    try:
        items = collections_ws.query_entities(
            session,
            query=str(payload.get("query") or ""),
            entity_type=payload.get("entity_type"),
            limit=limit,
        )
        page = library_service.LibraryPage(
            paper_ids=[],
            next_cursor=None,
            has_more=False,
            total=len(items),
            total_kind="EXACT",
            scope_revision=library_service.scope_revision(session),
            filter_hash=library_service.filter_hash(LibraryQuery()),
        )
        return page_envelope(page, items, kind="ENTITIES")
    finally:
        session.close()


@router.get("/entities/{entity_id}", response_model=None)
def get_entity(
    request: Request,
    entity_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    from paperintel.services.ui import collections_ws

    session = _session(request)
    try:
        return envelope(collections_ws.entity_card(session, entity_id))
    finally:
        session.close()


@router.post("/tags/actions", response_model=None, status_code=202)
def post_tag_actions(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Tag confirmation / alias merge (docs/06 §专题与实体).

    The action reports what it WOULD change (affected_count) and never rewrites
    old evidence text; a conflicting target is reported instead of merged
    silently.
    """
    from paperintel.services.ui import tags_actions

    session = _session(request)
    try:
        view = tags_actions.apply_tag_action(
            session,
            action=str(payload.get("action") or ""),
            tag_ids=list(payload.get("tag_ids") or []),
            target_tag_id=payload.get("target_tag_id"),
            canonical_name=payload.get("canonical_name"),
            idempotency_key=payload.get("idempotency_key"),
            preview_only=payload.get("preview_only", True) is not False,
        )
        session.commit()
        return envelope(view)
    finally:
        session.close()


__all__ = ["router"]

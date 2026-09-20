"""Browser-authenticated adapters for shared core read models."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from paperintel.api.ui_routes.deps import envelope, session_of, settings_of, ui_identity
from paperintel.services import read_models

router = APIRouter(prefix="/v1/ui", tags=["ui"], dependencies=[Depends(ui_identity)])


@router.get("/jobs")
def jobs(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    with session_of(request) as session:
        return envelope(read_models.job_list(session, limit=limit))


@router.get("/collections")
def collections(request: Request) -> dict:
    with session_of(request) as session:
        return envelope(read_models.collection_list(session))


@router.get("/system/status")
def system_status(request: Request) -> dict:
    with session_of(request) as session:
        return envelope(read_models.system_status(session, settings=settings_of(request)))


@router.get("/claims/{claim_id}/evidence")
def claim_evidence(request: Request, claim_id: str) -> dict:
    with session_of(request) as session:
        payload = read_models.claim_evidence(session, claim_id)
        # The audit bundle carries only text_excerpt (300 chars), not the
        # original quote the reader promises to display. Resolve canonical
        # evidence for this browser projection; keep the audit link metadata.
        for item in payload["evidence"]:
            if not item.get("missing"):
                item.update(read_models.evidence_detail(session, item["evidence_id"]))
        return envelope(payload)

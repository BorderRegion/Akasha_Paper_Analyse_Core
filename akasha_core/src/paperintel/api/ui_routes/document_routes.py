"""api.ui_routes.document_routes — authenticated originals and locators."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse

from paperintel.api.ui_routes.deps import UiIdentity, data_dir, envelope, ui_identity
from paperintel.services.ui import documents

router = APIRouter(prefix="/v1/ui", tags=["ui-documents"])


def _session(request: Request):
    return request.app.state.paperintel.session_factory()


@router.get("/paper-versions/{paper_version_id}/document", response_model=None)
def get_document(
    request: Request,
    paper_version_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> Response:
    """The stored PDF, authenticated, with Range/206 + ETag support.

    Path traversal is impossible: the file is resolved from the DB asset row.
    """
    session = _session(request)
    try:
        info = documents.document_info(session, paper_version_id, data_dir=data_dir(request))
        documents.verify_document_hash(session, paper_version_id, data_dir=data_dir(request))
        if if_none_match and if_none_match.strip() == info.etag:
            return Response(status_code=304, headers={"ETag": info.etag})
        try:
            slice_ = documents.read_slice(info, range_header)
        except Exception as exc:  # noqa: BLE001 - re-raised as 416 below
            from paperintel.errors import DomainError

            if isinstance(exc, DomainError):
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{info.size_bytes}"},
                    content=b"",
                )
            raise
        headers = {
            "ETag": info.etag,
            "Accept-Ranges": "bytes",
            "Content-Length": str(slice_.content_length),
            "Content-Type": info.mime_type,
            "Cache-Control": "private, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{paper_version_id}.pdf"',
        }
        if slice_.content_range:
            headers["Content-Range"] = slice_.content_range
        return StreamingResponse(
            documents.iter_slice(slice_), status_code=slice_.status, headers=headers
        )
    finally:
        session.close()


@router.get("/assets/{asset_id}/content", response_model=None)
def get_asset(
    request: Request,
    asset_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
    range_header: Annotated[str | None, Header(alias="Range")] = None,
) -> Response:
    session = _session(request)
    try:
        info = documents.asset_info(session, asset_id, data_dir=data_dir(request))
        slice_ = documents.read_slice(info, range_header)
        headers = {
            "ETag": info.etag,
            "Accept-Ranges": "bytes",
            "Content-Length": str(slice_.content_length),
            "Content-Type": info.mime_type,
            "X-Content-Type-Options": "nosniff",
        }
        if slice_.content_range:
            headers["Content-Range"] = slice_.content_range
        return StreamingResponse(
            documents.iter_slice(slice_), status_code=slice_.status, headers=headers
        )
    finally:
        session.close()


@router.get("/paper-versions/{paper_version_id}/pages/{page}/evidence", response_model=None)
def get_page_evidence(
    request: Request,
    paper_version_id: str,
    page: int,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    session = _session(request)
    try:
        payload = documents.page_evidence(
            session, paper_version_id, page, data_dir=data_dir(request)
        )
        session.commit()
        return envelope(payload)
    finally:
        session.close()

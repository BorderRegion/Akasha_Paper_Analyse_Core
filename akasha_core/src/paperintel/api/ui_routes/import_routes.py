"""api.ui_routes.import_routes — browser upload batches (docs/06 §导入)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile

from paperintel.api.ui_routes.deps import (
    UiIdentity,
    data_dir,
    envelope,
    require_csrf,
    settings_of,
    ui_identity,
)
from paperintel.errors import DomainError
from paperintel.services.ui import imports as imports_service

router = APIRouter(prefix="/v1/ui", tags=["ui-import"])

CHUNK = 256 * 1024


def _session(request: Request):
    return request.app.state.paperintel.session_factory()


def _stream(upload: UploadFile) -> Iterator[bytes]:
    while True:
        chunk = upload.file.read(CHUNK)
        if not chunk:
            break
        yield chunk


@router.post("/import-batches", response_model=None)
def create_batch(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = _session(request)
    try:
        row = imports_service.create_batch(
            session,
            collection_ids=payload.get("collection_ids"),
            requested_tier=payload.get("requested_tier", "T2_FULL"),
            owner_key=identity.owner_key,
        )
        session.commit()
        return envelope({"batch_id": row.batch_id, "items": []})
    finally:
        session.close()


@router.get("/import-batches/{batch_id}", response_model=None)
def get_batch(
    request: Request,
    batch_id: str,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    session = _session(request)
    try:
        return envelope(imports_service.batch_view(session, batch_id))
    finally:
        session.close()


@router.post("/import-batches/{batch_id}/files", response_model=None)
def upload_file(
    request: Request,
    batch_id: str,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
    file: Annotated[UploadFile, File()] = None,  # type: ignore[assignment]
    idempotency_key: str | None = None,
) -> dict:
    """Stream one file into the batch.

    The filename is sanitised, the size is counted while receiving, and the
    success criterion is the content hash — never the declared length.
    """
    limits = imports_service.UploadLimits.from_settings(settings_of(request))
    session = _session(request)
    try:
        key = idempotency_key or (file.filename or "upload.pdf")
        item, created = imports_service.register_item(
            session,
            batch_id=batch_id,
            filename=file.filename or "upload.pdf",
            idempotency_key=f"{batch_id}:{key}",
            limits=limits,
            owner_key=identity.owner_key,
        )
        session.commit()
        if not created and item.state in imports_service.IRREVERSIBLE_STATES:
            # Replaying a completed item is idempotent SUCCESS: the same key
            # already became a paper, so there is nothing left to receive.
            return envelope(
                {
                    "batch_id": batch_id,
                    "item_id": item.item_id,
                    "state": item.state,
                    "sha256": item.sha256,
                    "size_bytes": item.size_bytes,
                    "created": False,
                    "replayed": True,
                    "paper_id": item.paper_id,
                    "paper_version_id": item.paper_version_id,
                    "job_id": item.job_id,
                    "error_code": item.error_code,
                }
            )
        try:
            item = imports_service.store_upload(
                session,
                item,
                _stream(file),
                data_dir=data_dir(request),
                limits=limits,
                declared_size=None,
            )
        except DomainError:
            # The item's FAILED state (and error code) must OUTLIVE the failed
            # request: the batch view has to show which file failed and offer a
            # retry for it alone. Commit the item, then report the error.
            session.commit()
            raise
        session.commit()
        # A received upload becomes a real paper version here: dedup by content
        # hash, then the existing ingest domain, then the job plan. The row is
        # committed before any queue publish (transactional outbox).
        item = imports_service.ingest_item(
            session,
            item,
            data_dir=data_dir(request),
            settings=settings_of(request),
        )
        session.commit()
        return envelope(
            {
                "batch_id": batch_id,
                "item_id": item.item_id,
                "state": item.state,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "created": created,
                "paper_id": item.paper_id,
                "paper_version_id": item.paper_version_id,
                "job_id": item.job_id,
                "error_code": item.error_code,
            }
        )
    finally:
        session.close()


@router.post("/import-batches/{batch_id}/retry", response_model=None)
def retry_failed(
    request: Request,
    batch_id: str,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Retry ONLY the failed items the client names (same idempotency keys)."""
    session = _session(request)
    try:
        items = imports_service.failed_items(session, batch_id, item_ids=payload.get("item_ids"))
        retried: list[str] = []
        for item in items:
            imports_service.retry_item(
                session,
                item,
                data_dir=data_dir(request),
                settings=settings_of(request),
            )
            retried.append(item.item_id)
        session.commit()
        return envelope({"batch_id": batch_id, "retried": retried})
    finally:
        session.close()


@router.delete("/import-batches/{batch_id}/items/{item_id}", response_model=None)
def cancel_item(
    request: Request,
    batch_id: str,
    item_id: str,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = _session(request)
    try:
        row = imports_service.cancel_item(session, item_id, owner_key=identity.owner_key)
        session.commit()
        return envelope({"item_id": row.item_id, "state": row.state})
    finally:
        session.close()

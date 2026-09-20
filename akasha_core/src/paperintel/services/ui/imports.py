"""services.ui.imports — browser upload batches (docs/06「导入」).

Contract highlights:
- a batch holds items; each ITEM has its own idempotency key, so retrying a
  failed file never re-imports the files that already succeeded;
- uploads stream to a temporary file while counting bytes, checking the PDF
  magic/parse, enforcing per-file and per-batch limits from capabilities and
  the store's free space — Content-Length and the filename are never trusted;
- the success criterion is the sha256 of the received bytes (dedup is decided
  by content), and the item records the paper/version/job it produced;
- a client may cancel only items that have not entered the irreversible
  persistence stage.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import UiImportBatchRow, UiImportItemRow
from paperintel.errors import DomainError
from paperintel.ids import new_ui_import_batch_id, new_ui_import_item_id
from paperintel.schemas.common import utcnow
from paperintel.services.ui.library import DEFAULT_PAGE

ITEM_STATES = (
    "PENDING",
    "UPLOADING",
    "RECEIVED",
    "QUEUED",
    "IMPORTED",
    "DUPLICATE",
    "FAILED",
    "CANCELLED",
)
#: States after which cancellation must be refused (persistence is committed).
IRREVERSIBLE_STATES = ("IMPORTED", "DUPLICATE")
MAX_UPLOAD_FILE_BYTES = 100 * 1024 * 1024
MAX_UPLOAD_BATCH_BYTES = 500 * 1024 * 1024
MAX_UPLOAD_FILES = 50
_PDF_MAGIC = b"%PDF-"


@dataclass(slots=True)
class UploadLimits:
    file_bytes: int = MAX_UPLOAD_FILE_BYTES
    batch_bytes: int = MAX_UPLOAD_BATCH_BYTES
    files: int = MAX_UPLOAD_FILES
    library_page_size: int = DEFAULT_PAGE

    def as_dict(self) -> dict[str, int]:
        """The limits block of GET /v1/ui/capabilities (contracts/ui.schema.json)."""
        return {
            "upload_file_bytes": self.file_bytes,
            "upload_batch_bytes": self.batch_bytes,
            "upload_files": self.files,
            "library_page_size": self.library_page_size,
        }

    @classmethod
    def from_settings(cls, settings) -> UploadLimits:
        """The deployment's limits — the SAME values capabilities advertises."""
        ui = getattr(settings, "ui", None)
        if ui is None:  # pragma: no cover - settings always carry the section
            return cls()
        return cls(
            file_bytes=ui.upload_file_bytes,
            batch_bytes=ui.upload_batch_bytes,
            files=ui.upload_files,
            library_page_size=ui.library_page_size,
        )


def create_batch(
    session: Session,
    *,
    collection_ids: list[str] | None = None,
    requested_tier: str = "T2_FULL",
    owner_key: str = "local",
) -> UiImportBatchRow:
    from paperintel.schemas.enums import ResourceTier

    try:
        requested_tier = ResourceTier(requested_tier).value
    except ValueError as exc:
        raise DomainError("CFG_002", message="Invalid requested resource tier.") from exc
    if collection_ids:
        from paperintel.knowledge.collections import get_collection

        for collection_id in collection_ids:
            get_collection(session, collection_id)
    row = UiImportBatchRow(
        batch_id=new_ui_import_batch_id(),
        owner_key=owner_key,
        collection_ids=collection_ids or None,
        requested_tier=requested_tier,
    )
    session.add(row)
    session.flush()
    return row


def get_batch(session: Session, batch_id: str, *, owner_key: str = "local") -> UiImportBatchRow:
    row = session.get(UiImportBatchRow, batch_id)
    if row is None or row.owner_key != owner_key:
        raise DomainError(
            "CFG_002", message=f"Unknown import batch: {batch_id}", details={"batch_id": batch_id}
        )
    return row


def batch_bytes(session: Session, batch_id: str) -> int:
    rows = session.scalars(
        select(UiImportItemRow).where(UiImportItemRow.batch_id == batch_id)
    ).all()
    return sum(row.size_bytes for row in rows)


def register_item(
    session: Session,
    *,
    batch_id: str,
    filename: str,
    idempotency_key: str,
    limits: UploadLimits | None = None,
    owner_key: str = "local",
) -> tuple[UiImportItemRow, bool]:
    """Create (or reuse) an item slot for one file.

    Repeating the same idempotency key returns the existing item — the reason a
    "retry failed files" action cannot duplicate anything.
    """
    limits = limits or UploadLimits()
    get_batch(session, batch_id, owner_key=owner_key)
    # Browser keys include batch/name/size/mtime and can easily exceed the
    # database's 96-character public-id column. Hash, never truncate: retries
    # retain identity while distinct long filenames cannot share a prefix key.
    if len(idempotency_key) > 96:
        idempotency_key = "upload:" + hashlib.sha256(idempotency_key.encode()).hexdigest()
    existing = session.scalar(
        select(UiImportItemRow).where(UiImportItemRow.idempotency_key == idempotency_key)
    )
    if existing is not None:
        if existing.batch_id != batch_id:
            raise DomainError("CFG_002", message="Upload key belongs to another batch.")
        return existing, False
    items = session.scalars(
        select(UiImportItemRow).where(UiImportItemRow.batch_id == batch_id)
    ).all()
    if len(items) + 1 > limits.files:
        raise DomainError(
            "CFG_002",
            message=f"A batch accepts at most {limits.files} files.",
            details={"limit": limits.files, "batch_id": batch_id},
        )
    row = UiImportItemRow(
        item_id=new_ui_import_item_id(),
        batch_id=batch_id,
        filename=Path(filename).name or "upload.pdf",
        size_bytes=0,
        state="PENDING",
        idempotency_key=idempotency_key,
    )
    session.add(row)
    session.flush()
    return row, True


def _free_bytes(path: Path) -> int:
    import shutil

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:  # pragma: no cover - probe failure means "unknown"
        return 0


def store_upload(
    session: Session,
    item: UiImportItemRow,
    stream: Iterator[bytes] | BinaryIO,
    *,
    data_dir: Path,
    limits: UploadLimits | None = None,
    declared_size: int | None = None,
) -> UiImportItemRow:
    """Stream one uploaded file to a temporary object and classify it.

    Order of checks mirrors docs/06: count while receiving → size limit →
    free-space floor → magic bytes → PDF parse → content hash → dedup by hash.
    """
    limits = limits or UploadLimits()
    if item.state in IRREVERSIBLE_STATES:
        raise DomainError(
            "CFG_002",
            message=f"Item {item.item_id} is already {item.state} and cannot be re-uploaded.",
            details={"item_id": item.item_id, "state": item.state},
        )

    temp_dir = Path(data_dir) / "temp" / "ui-imports"
    temp_dir.mkdir(parents=True, exist_ok=True)
    target = temp_dir / f"{item.item_id}.part"

    batch_total = batch_bytes(session, item.batch_id)
    free = _free_bytes(Path(data_dir))
    digest = hashlib.sha256()
    written = 0
    item.state = "UPLOADING"
    try:
        with target.open("wb") as handle:
            for chunk in stream:
                if not chunk:
                    continue
                written += len(chunk)
                if written > limits.file_bytes:
                    raise DomainError(
                        "STORAGE_001",
                        message=f"File exceeds the {limits.file_bytes} byte limit.",
                        details={"item_id": item.item_id, "limit": limits.file_bytes},
                    )
                if batch_total + written > limits.batch_bytes:
                    raise DomainError(
                        "STORAGE_001",
                        message=f"Batch exceeds the {limits.batch_bytes} byte limit.",
                        details={"batch_id": item.batch_id, "limit": limits.batch_bytes},
                    )
                if written > free:
                    raise DomainError(
                        "RESOURCE_001",
                        message="Not enough free space to receive this file.",
                        details={"free_bytes": free, "received": written},
                    )
                digest.update(chunk)
                handle.write(chunk)
    except DomainError:
        item.state = "FAILED"
        target.unlink(missing_ok=True)
        session.flush()
        raise
    except OSError as exc:
        item.state = "FAILED"
        target.unlink(missing_ok=True)
        session.flush()
        raise DomainError(
            "STORAGE_001",
            message="Receiving the upload failed.",
            details={"item_id": item.item_id, "reason": type(exc).__name__},
        ) from exc

    head = b""
    with target.open("rb") as handle:
        head = handle.read(5)
    if head != _PDF_MAGIC:
        item.state = "FAILED"
        item.error_code = "PDF_001"
        target.unlink(missing_ok=True)
        session.flush()
        raise DomainError(
            "PDF_001",
            message="The uploaded file is not a PDF (magic bytes mismatch).",
            details={"item_id": item.item_id},
        )

    item.size_bytes = written
    item.sha256 = digest.hexdigest()
    item.state = "RECEIVED"
    item.updated_at = utcnow()
    session.flush()
    return item


def staged_path(item: UiImportItemRow, *, data_dir: Path) -> Path:
    return Path(data_dir) / "temp" / "ui-imports" / f"{item.item_id}.part"


def ingest_item(
    session: Session,
    item: UiImportItemRow,
    *,
    data_dir: Path,
    settings,
    dispatch: bool = True,
) -> UiImportItemRow:
    """Move a RECEIVED item into the real corpus (docs/06 §导入).

    Dedup is by CONTENT HASH, not by idempotency key: re-uploading the same PDF
    under a different name resolves to the existing version and is reported as
    DUPLICATE (the batch keeps its own history, the corpus does not grow).
    After the row is durable the existing queue dispatches the job — a dispatch
    failure marks the item RETRYABLE with its error code instead of pretending
    the work started.
    """
    from paperintel.database.models import PaperVersionRow
    from paperintel.storage.object_store import LocalObjectStore

    if item.state not in ("RECEIVED", "PENDING"):
        raise DomainError(
            "CFG_002",
            message=f"Item {item.item_id} is {item.state}; only a received upload can be ingested.",
            details={"item_id": item.item_id, "state": item.state},
        )
    path = staged_path(item, data_dir=data_dir)
    if item.state == "PENDING" and not path.exists():
        raise DomainError(
            "STORAGE_003",
            message="No staged upload for this item; upload the file first.",
            details={"item_id": item.item_id},
        )
    existing_version = session.scalar(
        select(PaperVersionRow).where(PaperVersionRow.content_sha256 == item.sha256)
    )
    if existing_version is not None:
        item.paper_id = existing_version.paper_id
        item.paper_version_id = existing_version.paper_version_id
        item.state = "DUPLICATE"
        item.updated_at = utcnow()
        path.unlink(missing_ok=True)
        session.flush()
        return item

    from paperintel.api.app import import_pdf_sync

    store = LocalObjectStore(Path(settings.core.data_dir) / "objects")
    try:
        result = import_pdf_sync(session, path, store, settings)
    except DomainError as exc:
        item.state = "FAILED"
        item.error_code = exc.code
        item.updated_at = utcnow()
        session.flush()
        raise
    item.paper_id = result.paper_id
    item.paper_version_id = result.paper_version_id
    item.state = "DUPLICATE" if result.deduplicated else "IMPORTED"
    item.error_code = None
    item.updated_at = utcnow()
    session.flush()
    path.unlink(missing_ok=True)

    if dispatch and not result.deduplicated:
        try:
            _dispatch(session, item, settings)
        except DomainError as exc:
            # The paper IS imported; only the follow-up work failed. Report it
            # as retryable so the client can retry this ONE item.
            item.state = "RETRYABLE"
            item.error_code = exc.code
            session.flush()
    return item


def retry_item(
    session: Session,
    item: UiImportItemRow,
    *,
    data_dir: Path,
    settings,
) -> UiImportItemRow:
    """Retry ONE failed item — never the whole batch (docs/06 §导入).

    Two genuinely different failures:

    - the bytes already reached the corpus (``paper_version_id`` set) and only
      the follow-up work failed → re-plan and re-dispatch, no re-upload;
    - nothing durable exists → the client re-uploads this file (the staged
      temporary object is not kept forever).
    """
    if item.state not in ("FAILED", "RETRYABLE"):
        raise DomainError(
            "CFG_002",
            message=f"Item {item.item_id} is {item.state}; only a failed item can be retried.",
            details={"item_id": item.item_id, "state": item.state},
        )
    if item.paper_version_id:
        _dispatch(session, item, settings)
        item.state = "IMPORTED"
        item.error_code = None
        item.updated_at = utcnow()
        session.flush()
        return item
    item.state = "PENDING"
    item.error_code = None
    item.updated_at = utcnow()
    session.flush()
    return item


def _dispatch(session: Session, item: UiImportItemRow, settings) -> str:
    """Plan the analysis job for a freshly imported version (transactional outbox).

    The job and its tasks are committed as rows FIRST; the existing queue
    drainer (``workflow.celery_app.dispatch_pending``) submits them, so a crash
    between commit and publish cannot lose work. In eager mode the first task is
    executed immediately through the same runner so local/CI usage makes real
    progress instead of waiting for a worker.
    """
    from paperintel.database.models import PaperVersionRow
    from paperintel.schemas.enums import PipelineStage, ResourceTier
    from paperintel.triage.service import compute_triage
    from paperintel.workflow import engine

    version_id = item.paper_version_id
    version = session.get(PaperVersionRow, version_id)
    if version is None:  # pragma: no cover - guarded by import_pdf_sync
        raise DomainError(
            "STORAGE_003",
            message="Imported version disappeared before dispatch.",
            details={"paper_version_id": version_id},
        )
    batch = session.get(UiImportBatchRow, item.batch_id)
    tier = ResourceTier(batch.requested_tier)
    decision = compute_triage(session, paper_id=item.paper_id, requested_tier=tier)
    job = engine.create_job(
        session,
        paper_id=item.paper_id,
        paper_version_id=version_id,
        current_stage=PipelineStage.ANALYZED,
        requested_tier=tier,
    )
    job.effective_tier = decision.effective_tier
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
    item.job_id = job.job_id
    session.flush()

    from paperintel.workflow.celery_app import dispatch_candidates, tasks_eager

    if tasks_eager():
        candidates = dispatch_candidates(session)
        if candidates:
            from paperintel.workflow.celery_app import submit_task

            submit_task(candidates[0], database_url=settings.database.url)
    return job.job_id


def cancel_item(session: Session, item_id: str, *, owner_key: str = "local") -> UiImportItemRow:
    """Cancel an item that has not been persisted yet."""
    row = session.get(UiImportItemRow, item_id)
    if row is None:
        raise DomainError(
            "CFG_002", message=f"Unknown import item: {item_id}", details={"item_id": item_id}
        )
    if row.state in IRREVERSIBLE_STATES:
        raise DomainError(
            "CFG_002",
            message=(
                f"Item {item_id} is already {row.state}; the paper exists and cannot be "
                "un-imported from here."
            ),
            details={"item_id": item_id, "state": row.state},
        )
    row.state = "CANCELLED"
    row.updated_at = utcnow()
    session.flush()
    return row


def failed_items(session: Session, batch_id: str, *, item_ids: list[str] | None = None):
    stmt = select(UiImportItemRow).where(
        UiImportItemRow.batch_id == batch_id, UiImportItemRow.state == "FAILED"
    )
    if item_ids:
        stmt = stmt.where(UiImportItemRow.item_id.in_(item_ids))
    return list(session.scalars(stmt.order_by(UiImportItemRow.created_at)))


def batch_view(session: Session, batch_id: str) -> dict:
    batch = get_batch(session, batch_id)
    items = session.scalars(
        select(UiImportItemRow)
        .where(UiImportItemRow.batch_id == batch_id)
        .order_by(UiImportItemRow.created_at, UiImportItemRow.item_id)
    ).all()
    return {
        "batch_id": batch.batch_id,
        "items": [
            {
                "item_id": row.item_id,
                "filename": row.filename,
                "size_bytes": row.size_bytes,
                "state": row.state,
                "paper_id": row.paper_id,
                "paper_version_id": row.paper_version_id,
                "job_id": row.job_id,
                "error_code": row.error_code,
            }
            for row in items
        ],
    }


__all__ = [
    "IRREVERSIBLE_STATES",
    "ITEM_STATES",
    "MAX_UPLOAD_BATCH_BYTES",
    "MAX_UPLOAD_FILES",
    "MAX_UPLOAD_FILE_BYTES",
    "UploadLimits",
    "batch_bytes",
    "batch_view",
    "cancel_item",
    "create_batch",
    "failed_items",
    "get_batch",
    "register_item",
    "retry_item",
    "staged_path",
    "store_upload",
]

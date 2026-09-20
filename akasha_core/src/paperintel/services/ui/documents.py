"""services.ui.documents — authenticated original-document access (docs/06).

The browser never receives a storage key as a URL: documents are streamed by
version/asset id through the API, with Range/206, ETag, Content-Length and
Accept-Ranges, and the document hash is checked against the version record
before a locator is allowed to draw a box on it.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from pathlib import Path

import fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    AssetRow,
    EvidenceRow,
    PaperRow,
    PaperVersionRow,
    UiEvidenceLocatorRow,
)
from paperintel.errors import DomainError
from paperintel.schemas.ui.models import EvidenceLocator

_RANGE_RE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")
DEFAULT_CHUNK = 512 * 1024


@dataclass(slots=True)
class DocumentInfo:
    paper_version_id: str
    asset_id: str
    path: Path
    size_bytes: int
    sha256: str
    etag: str
    mime_type: str


@dataclass(slots=True)
class DocumentSlice:
    info: DocumentInfo
    start: int
    end: int
    status: int  # 200 or 206
    content_length: int
    content_range: str | None


def document_info(session: Session, paper_version_id: str, *, data_dir: Path) -> DocumentInfo:
    version = session.get(PaperVersionRow, paper_version_id)
    if version is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown paper version: {paper_version_id}",
            details={"paper_version_id": paper_version_id},
        )
    asset = session.get(AssetRow, version.asset_id)
    if asset is None:
        raise DomainError(
            "STORAGE_003",
            message="The version's document asset row is missing.",
            details={"paper_version_id": paper_version_id, "asset_id": version.asset_id},
        )
    from paperintel.storage.object_store import resolve_storage_path

    path = resolve_storage_path(Path(data_dir) / "objects", asset.storage_key)
    if not path.is_file():
        raise DomainError(
            "STORAGE_003",
            message="The stored document is not available on disk.",
            details={"paper_version_id": paper_version_id, "asset_id": asset.asset_id},
        )
    return DocumentInfo(
        paper_version_id=paper_version_id,
        asset_id=asset.asset_id,
        path=path,
        size_bytes=path.stat().st_size,
        sha256=asset.sha256,
        etag=f'"{asset.sha256}"',
        mime_type="application/pdf",
    )


def asset_info(session: Session, asset_id: str, *, data_dir: Path) -> DocumentInfo:
    """Assets are read by DB id only; a user-supplied path is never honoured."""
    asset = session.get(AssetRow, asset_id)
    if asset is None:
        raise DomainError(
            "STORAGE_003", message=f"Unknown asset ID: {asset_id}", details={"asset_id": asset_id}
        )
    from paperintel.storage.object_store import resolve_storage_path

    path = resolve_storage_path(Path(data_dir) / "objects", asset.storage_key)
    if not path.is_file():
        raise DomainError(
            "STORAGE_003",
            message="Asset content is not available on disk.",
            details={"asset_id": asset_id},
        )
    return DocumentInfo(
        paper_version_id="",
        asset_id=asset.asset_id,
        path=path,
        size_bytes=path.stat().st_size,
        sha256=asset.sha256,
        etag=f'"{asset.sha256}"',
        mime_type=asset.mime_type,
    )


def parse_range(header: str | None, *, size: int) -> tuple[int, int, int]:
    """Parse a single-range request → (start, end, status).

    Unsatisfiable ranges raise STORAGE_003 (mapped to 416 by the route).
    """
    if not header:
        return 0, size - 1, 200
    match = _RANGE_RE.fullmatch(header.strip())
    if match is None:
        raise DomainError(
            "STORAGE_003",
            message="Only a single 'bytes=start-end' range is supported.",
            details={"range": header},
        )
    start_raw, end_raw = match.group("start"), match.group("end")
    if start_raw == "" and end_raw == "":
        raise DomainError("STORAGE_003", message="Empty byte range.", details={"range": header})
    if start_raw == "":
        length = int(end_raw)
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_raw)
        end = int(end_raw) if end_raw else size - 1
    if start >= size:
        raise DomainError(
            "STORAGE_003",
            message="Requested range starts beyond the document.",
            details={"range": header, "size": size},
        )
    end = min(end, size - 1)
    if end < start:
        raise DomainError("STORAGE_003", message="Inverted byte range.", details={"range": header})
    return start, end, 206


def read_slice(info: DocumentInfo, header: str | None) -> DocumentSlice:
    start, end, status = parse_range(header, size=info.size_bytes)
    return DocumentSlice(
        info=info,
        start=start,
        end=end,
        status=status,
        content_length=end - start + 1,
        content_range=f"bytes {start}-{end}/{info.size_bytes}" if status == 206 else None,
    )


def iter_slice(slice_: DocumentSlice, *, chunk_size: int = DEFAULT_CHUNK):
    remaining = slice_.content_length
    with slice_.info.path.open("rb") as handle:
        handle.seek(slice_.start)
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def verify_document_hash(session: Session, paper_version_id: str, *, data_dir: Path) -> str:
    """Recompute the document hash and compare it with the stored one."""
    info = document_info(session, paper_version_id, data_dir=data_dir)
    digest = hashlib.sha256()
    with info.path.open("rb") as handle:
        for block in iter(lambda: handle.read(DEFAULT_CHUNK), b""):
            digest.update(block)
    actual = digest.hexdigest()
    version = session.get(PaperVersionRow, paper_version_id)
    if actual != info.sha256 or (version is not None and version.content_sha256 != actual):
        raise DomainError(
            "STORAGE_002",
            message="Stored document hash does not match the version record.",
            details={"paper_version_id": paper_version_id, "expected": version.content_sha256},
        )
    return actual


# ---------------------------------------------------------------------------
# locator projection
# ---------------------------------------------------------------------------

LOCATOR_PRECISION_REGION = "REGION"
LOCATOR_PRECISION_PAGE = "PAGE"
LOCATOR_PRECISION_TEXT_ONLY = "TEXT_ONLY"


def rebuild_locators(
    session: Session, paper_version_id: str, *, data_dir: Path | None = None,
) -> int:
    """(Re)build the locator projection for one version.

    Only references and derived geometry are stored: evidence text and evidence
    rows are never modified (frontend spec docs/06, core invariant).
    """
    version = session.scalar(
        select(PaperVersionRow).where(PaperVersionRow.paper_version_id == paper_version_id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if version is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown paper version: {paper_version_id}",
            details={"paper_version_id": paper_version_id},
        )
    if data_dir is None:
        from paperintel.config.settings import get_settings

        data_dir = Path(get_settings().core.data_dir)
    verify_document_hash(session, paper_version_id, data_dir=data_dir)
    geometry = _document_geometry(session, paper_version_id, data_dir=data_dir)
    rows = session.scalars(
        select(EvidenceRow).where(EvidenceRow.paper_version_id == paper_version_id)
    ).all()
    written = 0
    for row in rows:
        existing = session.get(UiEvidenceLocatorRow, row.evidence_id)
        rect = None
        precision = LOCATOR_PRECISION_TEXT_ONLY
        reason = None
        if isinstance(row.bbox, dict) and {"x0", "y0", "x1", "y1"} <= set(row.bbox):
            # Evidence bboxes are PDF points; the projection stores a
            # normalized display rectangle so the reader can scale it.
            page = geometry.get(row.page_start)
            if page is not None:
                rect = _normalized_rect(row.bbox, row.source_method, page)
            if rect is not None:
                precision = LOCATOR_PRECISION_REGION
            else:
                reason = "bounding box or page geometry unusable; only the page can be located"
                precision = LOCATOR_PRECISION_PAGE
        else:
            reason = "no bounding box in the evidence; only the page can be located"
            precision = LOCATOR_PRECISION_PAGE
        values = dict(
                evidence_id=row.evidence_id,
                paper_version_id=row.paper_version_id,
                document_sha256=version.content_sha256,
                page_number=row.page_start,
                page_label=geometry.get(row.page_start, {}).get("label"),
                coordinate_space="DISPLAY_NORMALIZED_V1",
                rect_norm=rect,
                precision=precision,
                source_method=row.source_method.value
                if hasattr(row.source_method, "value")
                else str(row.source_method),
                ocr_confidence=row.ocr_confidence,
                transform_revision=f"{version.content_sha256[:12]}-norm2",
                extraction_run_id=row.extraction_run_id,
                reason=reason,
        )
        if existing is None:
            session.add(UiEvidenceLocatorRow(**values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        written += 1
    session.flush()
    return written


def _document_geometry(session: Session, paper_version_id: str, *, data_dir: Path) -> dict:
    info = document_info(session, paper_version_id, data_dir=data_dir)
    with fitz.open(info.path) as document:
        return {
            page.number + 1: {
                "width": page.rect.width, "height": page.rect.height,
                "matrix": page.rotation_matrix, "rotation": page.rotation,
                "label": (page.get_label() or "")[:64] or None,
            }
            for page in document
        }


def _normalized_rect(bbox: dict, source_method: str, geometry: dict) -> list[float] | None:
    try:
        values = [float(bbox[key]) for key in ("x0", "y0", "x1", "y1")]
        if not all(math.isfinite(value) for value in values):
            return None
        box = fitz.Rect(values)
        if box.is_empty:
            return None
        # Native text uses unrotated crop coordinates; OCR uses the displayed
        # raster. Mixed provenance cannot establish the frame on rotated pages.
        if source_method == "PDF_NATIVE":
            box = box * geometry["matrix"]
        elif source_method != "OCR" and geometry["rotation"]:
            return None
        width, height = geometry["width"], geometry["height"]
        box &= fitz.Rect(0, 0, width, height)
        if box.is_empty or width <= 0 or height <= 0:
            return None
        return [box.x0 / width, box.y0 / height, box.x1 / width, box.y1 / height]
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def page_evidence(session: Session, paper_version_id: str, page: int, *, data_dir: Path) -> dict:
    """Backing data for GET /v1/ui/paper-versions/{id}/pages/{page}/evidence.

    The document hash is verified first: if the PDF no longer matches the
    version record, the response refuses to place boxes on it.
    """
    info = document_info(session, paper_version_id, data_dir=data_dir)
    verify_document_hash(session, paper_version_id, data_dir=data_dir)
    version = session.get(PaperVersionRow, paper_version_id)
    locators = session.scalars(
        select(UiEvidenceLocatorRow).where(
            UiEvidenceLocatorRow.paper_version_id == paper_version_id,
            UiEvidenceLocatorRow.page_number == page,
        )
    ).all()
    if not locators or any(row.transform_revision != f"{info.sha256[:12]}-norm2" for row in locators):
        rebuild_locators(session, paper_version_id, data_dir=data_dir)
        locators = session.scalars(
            select(UiEvidenceLocatorRow).where(
                UiEvidenceLocatorRow.paper_version_id == paper_version_id,
                UiEvidenceLocatorRow.page_number == page,
            )
        ).all()
    geometry = _document_geometry(session, paper_version_id, data_dir=data_dir).get(page)
    size = (geometry["width"], geometry["height"]) if geometry else (612.0, 792.0)
    return {
        "paper_version_id": paper_version_id,
        "document_sha256": version.content_sha256 if version else info.sha256,
        "page_number": page,
        "page_display_width": int(size[0]),
        "page_display_height": int(size[1]),
        "reference_rotation": 0,
        "evidence": [
            EvidenceLocator(
                evidence_id=row.evidence_id,
                paper_version_id=row.paper_version_id,
                document_sha256=row.document_sha256,
                page_number=row.page_number,
                page_label=row.page_label,
                coordinate_space="DISPLAY_NORMALIZED_V1",
                rect_norm=list(row.rect_norm) if row.rect_norm else None,
                precision=row.precision,
                source_method=row.source_method,
                ocr_confidence=row.ocr_confidence,
                transform_revision=row.transform_revision,
                extraction_run_id=row.extraction_run_id,
                reason=row.reason,
            )
            for row in locators
        ],
    }


def paper_title(session: Session, paper_id: str) -> str:
    paper = session.get(PaperRow, paper_id)
    return paper.canonical_title if paper is not None else ""


__all__ = [
    "DEFAULT_CHUNK",
    "DocumentInfo",
    "DocumentSlice",
    "asset_info",
    "document_info",
    "iter_slice",
    "page_evidence",
    "paper_title",
    "parse_range",
    "read_slice",
    "rebuild_locators",
    "verify_document_hash",
]

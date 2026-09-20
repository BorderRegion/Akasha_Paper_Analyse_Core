"""ingest.import — PDF import pipeline (spec doc 05 P03).

Pipeline (doc 03 §8 stages IMPORTED → FINGERPRINTED → PDF_INSPECTED →
EXTRACTED; METADATA_RESOLVED is owned by P08):

1. read + fingerprint (sha256);
2. duplicate detection: identical content_sha256 → return the EXISTING
   paper/version/asset (versions never overwrite or duplicate);
3. open/inspect (PDF_001 malformed, PDF_002 encrypted);
4. classify every page BEFORE OCR; native extraction everywhere; OCR
   fallback only for pages that need it (failures recorded per page in the
   report — never swallowed, never silently replaced);
5. mixed-page merge with honest per-page source methods;
6. build the extraction report + quality checks IN MEMORY; zero-page or
   no-usable-text documents raise EXTRACT_001 BEFORE any row is persisted —
   a failed import leaves no partial state (the diagnostic report is still
   written to the content-addressed cache path);
7. persist asset (content-addressed dedup) + paper identity (DOI, else
   normalized title) + new version row;
8. attach the version id to the report, persist the report cache;
9. figure/table crops persisted as canonical assets.

Transaction policy: the service flushes but does not commit; the caller
(CLI session_scope / test transaction) owns commit/rollback.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import AssetRow, PaperRow, PaperVersionRow
from paperintel.errors import DomainError
from paperintel.evidence import store as store_evidence
from paperintel.extraction.merge import merge_page_units
from paperintel.extraction.native import extract_document_units
from paperintel.extraction.page_classifier import classify_page
from paperintel.extraction.pdf_document import (
    OCR_RENDER_DPI,
    inspect_document,
    open_pdf,
    render_clip_png,
)
from paperintel.extraction.report import build_report
from paperintel.ids import new_asset_id, new_paper_id, new_paper_version_id, new_run_id
from paperintel.ingest import fingerprint
from paperintel.ocr.fallback import ocr_page, units_from_ocr_result
from paperintel.providers.base import OCRProvider
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import AssetKind, RetentionClass
from paperintel.schemas.extraction import (
    ExtractedUnit,
    ExtractionReport,
    ImportResult,
    PageExtraction,
)
from paperintel.storage.object_store import LocalObjectStore


def _report_cache_path(data_dir: Path, content_sha256: str) -> Path:
    return Path(data_dir) / "cache" / "extraction_reports" / f"{content_sha256}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Same-directory temp + fsync + os.replace (doc 06: atomic writes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{os.getpid()}-{path.name}")
    data = (json.dumps(payload, indent=2, default=str) + "\n").encode("utf-8")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _load_cached_report(path: Path) -> ExtractionReport | None:
    if not path.is_file():
        return None
    try:
        return ExtractionReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValueError) as exc:
        # A corrupt cache file must not masquerade as a report; recompute.
        logging.getLogger("paperintel.ingest").warning(
            "cached extraction report unreadable, will recompute",
            extra={"path": str(path), "reason": type(exc).__name__},
        )
        return None


def _title_from_units(
    pages_units: dict[int, list[ExtractedUnit]], fallback: str
) -> tuple[str, str]:
    """(title, derivation note): first HEADING candidate on page 1, else the
    longest early text unit, else the fallback (filename stem)."""
    first_page = pages_units.get(1, [])
    for unit in first_page:
        if unit.unit_type.value == "HEADING" and unit.text:
            return " ".join(unit.text.split()), "title: first page-1 heading candidate"
    candidates = [unit.text for unit in first_page if unit.text and 12 <= len(unit.text) <= 300]
    if candidates:
        best = max(candidates[:5], key=len)
        return " ".join(best.split()), "title: longest page-1 text unit (heuristic)"
    return fallback, "title: filename stem (no text candidate)"


async def import_pdf(
    path: str | os.PathLike[str],
    *,
    session: Session,
    store: LocalObjectStore,
    data_dir: str | os.PathLike[str],
    ocr_provider: OCRProvider | None = None,
    source_type: str = "local_file",
    version_label: str | None = None,
    title: str | None = None,
    doi: str | None = None,
    enable_ocr: bool = True,
    render_dpi: int = OCR_RENDER_DPI,
    persist_crops: bool = True,
    persist_evidence: bool = True,
    run_id: str | None = None,
) -> ImportResult:
    """Import one PDF. See module docstring for the full pipeline."""
    started_at: datetime = utcnow()
    run_id = run_id or new_run_id()
    source_path = Path(path)
    notes: list[str] = []

    if not source_path.is_file():
        raise DomainError(
            "PDF_001",
            message="Input file does not exist or is not readable.",
            details={"filename": source_path.name},
        )
    from paperintel.extraction.pdf_document import MAX_PDF_BYTES

    if source_path.stat().st_size > MAX_PDF_BYTES:
        raise DomainError("PDF_001", message="PDF exceeds the 100 MiB size limit.")
    with source_path.open("rb") as source:
        data = source.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise DomainError("PDF_001", message="PDF exceeds the 100 MiB size limit.")
    content_sha = fingerprint.fingerprint_bytes(data)

    # --- 2. duplicate detection (identical content → identical version) ----
    existing_version = fingerprint.find_version_by_content(session, content_sha)
    report_cache = _report_cache_path(Path(data_dir), content_sha)
    if existing_version is not None:
        cached = _load_cached_report(report_cache)
        if cached is None:
            cached = await _rebuild_report(
                data,
                run_id=run_id,
                started_at=started_at,
                paper_version_id=existing_version.paper_version_id,
                ocr_provider=None,
                render_dpi=render_dpi,
            )
            notes.append("dedup: cached report missing; rebuilt WITHOUT OCR fallback")
        evidence_result = None
        if persist_evidence:
            # Self-healing: versions imported before P04 (or interrupted
            # stores) get their evidence persisted now. Idempotent — a
            # healthy store reuses every row and creates nothing.
            evidence_result = store_evidence.persist_extraction(
                session, cached, paper_id=existing_version.paper_id
            )
            notes.append(
                "evidence store: "
                f"{evidence_result.evidence_created} created, "
                f"{evidence_result.evidence_reused} reused"
            )
        return ImportResult(
            paper_id=existing_version.paper_id,
            paper_version_id=existing_version.paper_version_id,
            asset_id=existing_version.asset_id,
            deduplicated=True,
            reused_asset=True,
            version_label=existing_version.version_label,
            report=cached,
            report_path=str(report_cache),
            evidence=evidence_result,
            notes=notes,
        )

    # --- 3. open + inspect + extract (all in memory first) ------------------
    # Nothing is persisted until the document proves it has usable text:
    # a failed import leaves NO partial rows behind (transactional honesty).
    doc = open_pdf(data)
    try:
        inspection = inspect_document(doc)
        native_units = extract_document_units(doc)

        # --- 4. classify → OCR fallback → merge ------------------------------
        pages, ocr_failures, warnings = await _extract_all_pages(
            doc,
            inspection,
            native_units,
            ocr_provider=ocr_provider if enable_ocr else None,
            enable_ocr=enable_ocr,
            render_dpi=render_dpi,
        )

        # --- 5. probe report + validation BEFORE persistence -----------------
        probe_report = build_report(
            run_id=run_id,
            paper_version_id=None,
            started_at=started_at,
            finished_at=utcnow(),
            inspection=inspection,
            pages=pages,
            ocr_failures=ocr_failures,
            warnings=warnings,
        )
        if inspection.is_zero_page or probe_report.quality_checks.suspiciously_empty_text:
            # Write the diagnostic report (content-addressed cache path, no
            # rows involved) then fail loudly — an empty document is imported
            # by NOBODY: no asset, no version, no paper identity.
            _atomic_write_json(report_cache, json.loads(probe_report.model_dump_json()))
            if inspection.is_zero_page:
                raise DomainError(
                    "EXTRACT_001",
                    message="PDF contains zero pages; extraction produced no usable text.",
                    details={"report_path": str(report_cache)},
                )
            raise DomainError(
                "EXTRACT_001",
                message="Extraction produced no usable text.",
                details={
                    "page_count": inspection.page_count,
                    "ocr_available": ocr_provider is not None and enable_ocr,
                    "report_path": str(report_cache),
                },
            )

        # --- 6. asset (content-addressed dedup) ------------------------------
        asset = fingerprint.find_asset_by_sha256(session, content_sha)
        reused_asset = asset is not None
        if asset is None:
            sha, key = store.put(data, kind=AssetKind.PDF.value)
            if sha != content_sha:  # pragma: no cover - defensive invariant
                raise DomainError(
                    "STORAGE_002",
                    message="Object store hash does not match import fingerprint.",
                    details={"expected": content_sha, "stored": sha},
                )
            asset = AssetRow(
                asset_id=new_asset_id(),
                kind=AssetKind.PDF,
                sha256=sha,
                storage_key=key,
                mime_type="application/pdf",
                size_bytes=len(data),
                retention_class=RetentionClass.KEEP,
            )
            session.add(asset)
            session.flush()
        elif not store.exists(asset.storage_key):
            # Row exists but the canonical object is gone (e.g. store wiped):
            # re-put deterministically restores it under the same key.
            store.put(data, kind=AssetKind.PDF.value)
            notes.append("asset row existed but object was missing; re-stored")

        # --- 7. paper identity + version row ---------------------------------
        pdf_title = str(inspection.metadata.get("title") or "").strip() or None
        if title:
            resolved_title, note = title, "title: explicit import option"
        elif pdf_title:
            resolved_title, note = pdf_title, "title: PDF metadata"
        else:
            resolved_title, note = _title_from_units(native_units, source_path.stem)
        notes.append(note)

        paper: PaperRow | None = None
        if doi:
            paper = fingerprint.find_paper_by_doi(session, doi)
        if paper is None and resolved_title:
            paper = fingerprint.find_paper_by_normalized_title(
                session, fingerprint.normalize_title(resolved_title)
            )
        if paper is None:
            paper = PaperRow(
                paper_id=new_paper_id(),
                canonical_title=resolved_title or source_path.stem,
                normalized_title=fingerprint.normalize_title(resolved_title or source_path.stem),
                doi=doi,
            )
            session.add(paper)
            session.flush()
        elif doi and paper.doi is None:
            paper.doi = doi  # enrich identity; never overwrite a differing DOI

        label = version_label or _next_version_label(session, paper.paper_id)
        if version_label:
            clash = session.scalar(
                select(PaperVersionRow).where(
                    PaperVersionRow.paper_id == paper.paper_id,
                    PaperVersionRow.version_label == version_label,
                )
            )
            if clash is not None:
                raise DomainError(
                    "CFG_002",
                    message=(
                        f"version_label {version_label!r} already exists for this paper "
                        "with different content."
                    ),
                    details={"paper_id": paper.paper_id, "version_label": version_label},
                )
        version = PaperVersionRow(
            paper_version_id=new_paper_version_id(),
            paper_id=paper.paper_id,
            version_label=label,
            source_type=source_type,
            source_locator=str(source_path),
            asset_id=asset.asset_id,
            page_count=inspection.page_count,
            content_sha256=content_sha,
        )
        session.add(version)
        session.flush()

        # --- 8. figure/table crops as canonical assets ------------------------
        # Runs BEFORE the final report build so crop hashes are part of the
        # units the report carries (rows already exist: validation passed).
        if persist_crops:
            _persist_crop_assets(doc, pages, session=session, store=store)

        # --- 9. final report (version id + crops) → cache ---------------------
        report = build_report(
            run_id=run_id,
            paper_version_id=version.paper_version_id,
            started_at=started_at,
            finished_at=utcnow(),
            inspection=inspection,
            pages=pages,
            ocr_failures=ocr_failures,
            warnings=warnings,
        )
        _atomic_write_json(report_cache, json.loads(report.model_dump_json()))

        # --- 10. P04 evidence store (immutable rows + section tree) -----------
        evidence_result = None
        if persist_evidence:
            evidence_result = store_evidence.persist_extraction(
                session, report, paper_id=paper.paper_id
            )

        return ImportResult(
            paper_id=paper.paper_id,
            paper_version_id=version.paper_version_id,
            asset_id=asset.asset_id,
            deduplicated=False,
            reused_asset=reused_asset,
            version_label=label,
            report=report,
            report_path=str(report_cache),
            evidence=evidence_result,
            notes=notes,
        )
    finally:
        doc.close()


def _next_version_label(session: Session, paper_id: str) -> str:
    existing = session.scalars(
        select(PaperVersionRow.version_label).where(PaperVersionRow.paper_id == paper_id)
    ).all()
    candidate = len(existing) + 1
    while f"v{candidate}" in existing:
        candidate += 1
    return f"v{candidate}"


async def _extract_all_pages(
    doc,
    inspection,
    native_units: dict[int, list[ExtractedUnit]],
    *,
    ocr_provider: OCRProvider | None,
    enable_ocr: bool,
    render_dpi: int,
) -> tuple[list[PageExtraction], dict[int, str], list[str]]:
    pages: list[PageExtraction] = []
    ocr_failures: dict[int, str] = {}
    warnings: list[str] = []
    for page_inspection in inspection.pages:
        classification = classify_page(page_inspection)
        page_units = native_units.get(page_inspection.page_number, [])
        ocr_units: list[ExtractedUnit] = []
        page_warnings: list[str] = []
        mean_confidence: float | None = None

        if classification.needs_ocr:
            if ocr_provider is None or not enable_ocr:
                page_warnings.append(
                    f"OCR_UNAVAILABLE: page classified {classification.mode.value} "
                    "needs OCR but no OCR provider is configured/enabled"
                )
            else:
                try:
                    result = await ocr_page(
                        ocr_provider, doc, page_inspection.page_number, dpi=render_dpi
                    )
                except DomainError as exc:
                    ocr_failures[page_inspection.page_number] = exc.code
                    page_warnings.append(f"OCR_FAILED: {exc.code} ({exc.message})")
                else:
                    ocr_units = units_from_ocr_result(
                        result,
                        page_number=page_inspection.page_number,
                        page_width_pt=page_inspection.width_pt,
                        page_height_pt=page_inspection.height_pt,
                        dpi=render_dpi,
                    )
                    page_warnings.extend(result.warnings)
                    mean_confidence = result.mean_confidence

        merged, method = merge_page_units(classification.mode, page_units, ocr_units)
        if page_inspection.is_blank:
            page_warnings.append("QUALITY: page is blank (no text, no images)")
        pages.append(
            PageExtraction(
                page_number=page_inspection.page_number,
                classification=classification,
                units=merged,
                page_source_method=method,
                ocr_used=bool(ocr_units),
                mean_ocr_confidence=mean_confidence,
                warnings=page_warnings,
            )
        )
        warnings.extend(
            f"page {page_inspection.page_number}: {warning}" for warning in page_warnings
        )
    return pages, ocr_failures, warnings


def _persist_crop_assets(
    doc,
    pages: list[PageExtraction],
    *,
    session: Session,
    store: LocalObjectStore,
) -> None:
    """Render FIGURE/TABLE regions to canonical crop assets (figures/ and
    table_images/) and record the object hash on the unit."""
    updated: list[PageExtraction] = []
    for page in pages:
        new_units = []
        for unit in page.units:
            if unit.unit_type.value in {"FIGURE", "TABLE"} and unit.bbox is not None:
                kind = (
                    AssetKind.FIGURE if unit.unit_type.value == "FIGURE" else AssetKind.TABLE_IMAGE
                )
                try:
                    png = render_clip_png(
                        doc,
                        unit.page_number,
                        (unit.bbox.x0, unit.bbox.y0, unit.bbox.x1, unit.bbox.y1),
                    )
                except ValueError:
                    new_units.append(unit)  # degenerate clip: keep unit, no crop
                    continue
                sha, key = store.put(png, kind=kind.value)
                _ensure_asset_row(session, sha, key, kind, len(png))
                flags = [*unit.flags, "crop:rendered_png"]
                new_units.append(unit.model_copy(update={"asset_sha256": sha, "flags": flags}))
            else:
                new_units.append(unit)
        updated.append(page.model_copy(update={"units": new_units}))
    pages[:] = updated


def _ensure_asset_row(session: Session, sha: str, key: str, kind: AssetKind, size: int) -> None:
    existing = fingerprint.find_asset_by_sha256(session, sha)
    if existing is not None:
        return
    session.add(
        AssetRow(
            asset_id=new_asset_id(),
            kind=kind,
            sha256=sha,
            storage_key=key,
            mime_type="image/png",
            size_bytes=size,
            retention_class=RetentionClass.KEEP,
        )
    )
    session.flush()


async def _rebuild_report(
    data: bytes,
    *,
    run_id: str,
    started_at: datetime,
    paper_version_id: str,
    ocr_provider: OCRProvider | None,
    render_dpi: int,
) -> ExtractionReport:
    """Recompute a report for an already-imported document (dedup path with
    a missing cache file). Native extraction only unless a provider is given.
    """
    doc = open_pdf(data)
    try:
        inspection = inspect_document(doc)
        native_units = extract_document_units(doc)
        pages, ocr_failures, warnings = await _extract_all_pages(
            doc,
            inspection,
            native_units,
            ocr_provider=ocr_provider,
            enable_ocr=ocr_provider is not None,
            render_dpi=render_dpi,
        )
        return build_report(
            run_id=run_id,
            paper_version_id=paper_version_id,
            started_at=started_at,
            finished_at=utcnow(),
            inspection=inspection,
            pages=pages,
            ocr_failures=ocr_failures,
            warnings=warnings,
        )
    finally:
        doc.close()


__all__ = ["import_pdf"]

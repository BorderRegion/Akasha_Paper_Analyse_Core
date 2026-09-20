"""operations.gc — garbage collection with dry-run (P10, doc 07 §7).

GC lists (dry-run) and removes (execute) only objects that are provably
unreferenced or expired by policy:
- TEMP files older than ``temp_max_age_hours``;
- DEBUG_TTL files older than ``debug_ttl_days``;
- CACHE entries that are regenerable and unreferenced;
- asset rows whose retention class is TEMP/DEBUG_TTL and whose stored
  object is missing.

CANONICAL OBJECTS ARE NEVER REMOVED BY ORDINARY GC: a KEEP asset still
referenced by evidence, a paper version, or any active record is listed as
protected — never as a candidate, even with ``--force`` semantics left out
of scope. Each candidate reports object, size, retention class, reason,
last reference and reclaimable bytes (doc 07 §7).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paperintel.config.settings import AppConfig, get_settings
from paperintel.database.models import (
    AssetRow,
    EvidenceRow,
    PaperVersionRow,
)
from paperintel.schemas.enums import RetentionClass


@dataclass(slots=True)
class GcCandidate:
    """One reclaimable object (doc 07 §7 reporting contract)."""

    object_path: str
    size_bytes: int
    retention_class: str
    reason: str
    last_reference: str | None
    #: True when the object is a canonical record's backing store.
    protected: bool = False


@dataclass(slots=True)
class GcReport:
    dry_run: bool
    candidates: list[GcCandidate] = field(default_factory=list)
    protected: list[GcCandidate] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reclaimed_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def candidate_bytes(self) -> int:
        return sum(candidate.size_bytes for candidate in self.candidates)


def find_candidates(
    session: Session, data_dir: str | Path, *, settings: AppConfig | None = None
) -> tuple[list[GcCandidate], list[GcCandidate]]:
    """Return (reclaimable candidates, protected canonical objects).

    Protected objects are reported explicitly so an operator can see what
    GC deliberately refuses to touch.
    """
    settings = settings or get_settings()
    root = Path(data_dir)
    now = time.time()
    temp_cutoff = now - settings.disk.temp_max_age_hours * 3600
    debug_cutoff = now - settings.disk.debug_ttl_days * 86400

    candidates: list[GcCandidate] = []
    protected: list[GcCandidate] = []

    # --- filesystem: TEMP / DEBUG_TTL / CACHE -----------------------------
    for name, retention in (
        ("temp", RetentionClass.TEMP),
        ("tmp", RetentionClass.TEMP),  # legacy v1.0.0 temporary directory
        ("debug", RetentionClass.DEBUG_TTL),
        ("cache", RetentionClass.CACHE),
    ):
        directory = root / name
        if not directory.exists():
            continue
        for entry in sorted(directory.rglob("*")):
            if not entry.is_file():
                continue
            try:
                stat = entry.stat()
            except OSError:  # pragma: no cover - transient FS races
                continue
            if retention is RetentionClass.TEMP and stat.st_mtime <= temp_cutoff:
                candidates.append(
                    GcCandidate(
                        object_path=str(entry),
                        size_bytes=stat.st_size,
                        retention_class=retention.value,
                        reason=(f"temporary file older than {settings.disk.temp_max_age_hours}h"),
                        last_reference=entry.name,
                    )
                )
            elif retention is RetentionClass.DEBUG_TTL and stat.st_mtime <= debug_cutoff:
                candidates.append(
                    GcCandidate(
                        object_path=str(entry),
                        size_bytes=stat.st_size,
                        retention_class=retention.value,
                        reason=(f"debug artifact older than {settings.disk.debug_ttl_days}d"),
                        last_reference=entry.name,
                    )
                )
            elif retention is RetentionClass.CACHE:
                if "extraction_reports" in entry.relative_to(directory).parts:
                    protected.append(GcCandidate(
                        object_path=str(entry), size_bytes=stat.st_size,
                        retention_class=retention.value,
                        reason="extraction report required for replay; OCR rebuild is not lossless",
                        last_reference=entry.name, protected=True,
                    ))
                    continue
                # Cache is regenerable by definition (doc 07 §4); it is
                # eligible regardless of age, but never silently: the
                # reason names the rebuild path.
                candidates.append(
                    GcCandidate(
                        object_path=str(entry),
                        size_bytes=stat.st_size,
                        retention_class=retention.value,
                        reason="regenerable cache entry",
                        last_reference=entry.name,
                    )
                )

    # --- database assets ---------------------------------------------------
    from paperintel.storage.object_store import resolve_storage_path

    for asset in session.scalars(select(AssetRow)):
        stored = resolve_storage_path(root / "objects", asset.storage_key)
        size = stored.stat().st_size if stored.exists() else asset.size_bytes
        if asset.retention_class is RetentionClass.KEEP:
            if _asset_referenced(session, asset.asset_id):
                protected.append(
                    GcCandidate(
                        object_path=str(stored),
                        size_bytes=size,
                        retention_class=asset.retention_class.value,
                        reason="canonical asset referenced by active records",
                        last_reference=asset.asset_id,
                        protected=True,
                    )
                )
            else:
                # An unreferenced KEEP asset is orphaned data, not garbage:
                # report it as protected-by-policy so an operator decides.
                protected.append(
                    GcCandidate(
                        object_path=str(stored),
                        size_bytes=size,
                        retention_class=asset.retention_class.value,
                        reason=(
                            "canonical asset with no current reference "
                            "(orphan data: operator decision, never automatic)"
                        ),
                        last_reference=asset.asset_id,
                        protected=True,
                    )
                )
            continue
        if not stored.exists():
            candidates.append(
                GcCandidate(
                    object_path=str(stored),
                    size_bytes=asset.size_bytes,
                    retention_class=asset.retention_class.value,
                    reason="asset row without stored object (dangling metadata)",
                    last_reference=asset.asset_id,
                )
            )
        elif asset.retention_class in (RetentionClass.TEMP, RetentionClass.DEBUG_TTL):
            candidates.append(
                GcCandidate(
                    object_path=str(stored),
                    size_bytes=size,
                    retention_class=asset.retention_class.value,
                    reason=f"{asset.retention_class.value} asset is not canonical",
                    last_reference=asset.asset_id,
                )
            )

    return candidates, protected


def _asset_referenced(session: Session, asset_id: str) -> bool:
    """Whether ANY active record still points at the asset.

    Checked across every canonical table that can reference an asset: the
    PDF backing a paper version, evidence crops/figures, and (future)
    supplementary assets. An unreferenced KEEP asset is reported as
    protected-by-policy rather than silently deleted.
    """
    evidence_count = session.scalar(
        select(func.count()).select_from(EvidenceRow).where(EvidenceRow.asset_id == asset_id)
    )
    if evidence_count:
        return True
    version_count = session.scalar(
        select(func.count())
        .select_from(PaperVersionRow)
        .where(PaperVersionRow.asset_id == asset_id)
    )
    return bool(version_count)


def run_gc(
    session: Session,
    data_dir: str | Path,
    *,
    dry_run: bool = True,
    settings: AppConfig | None = None,
) -> GcReport:
    """GC dry-run (default) or execution.

    Execution removes only the reported candidates; protected canonical
    objects are never touched. Failures are collected per object (never
    aborting the whole sweep) and reported.
    """
    candidates, protected = find_candidates(session, data_dir, settings=settings)
    report = GcReport(dry_run=dry_run, candidates=candidates, protected=protected)
    if dry_run:
        report.reclaimed_bytes = report.candidate_bytes
        return report

    for candidate in candidates:
        path = Path(candidate.object_path)
        try:
            if path.exists() and path.is_file():
                size = path.stat().st_size
                path.unlink()
                report.reclaimed_bytes += size
            else:
                report.reclaimed_bytes += candidate.size_bytes
            report.removed.append(str(path))
        except OSError as exc:
            report.errors.append(f"{path}: {exc}")

    # Remove dangling asset rows whose objects are gone (metadata cleanup).
    for candidate in candidates:
        if not candidate.reason.startswith("asset row without stored object"):
            continue
        asset = session.get(AssetRow, candidate.last_reference)
        if asset is not None:
            session.delete(asset)
    session.flush()
    return report


def content_dedup_report(session: Session, data_dir: str | Path) -> dict:
    """Content-addressed dedup inventory (doc 07 §4 KEEP canonical objects).

    Assets are unique by sha256 by schema, so this reports the dedup
    efficiency and any stray duplicate FILES under the object store that
    are not the canonical path for their hash.
    """
    root = Path(data_dir)
    objects_dir = root / "objects"
    assets = list(session.scalars(select(AssetRow)))
    canonical_keys = {asset.storage_key for asset in assets}
    canonical_bytes = sum(asset.size_bytes for asset in assets)

    duplicates: list[dict] = []
    duplicate_bytes = 0
    if objects_dir.exists():
        for entry in sorted(objects_dir.rglob("*")):
            if not entry.is_file():
                continue
            relative = str(entry.relative_to(objects_dir))
            if relative in canonical_keys:
                continue
            try:
                size = entry.stat().st_size
            except OSError:  # pragma: no cover
                continue
            duplicates.append({"path": str(entry), "size_bytes": size})
            duplicate_bytes += size

    versions = session.scalar(select(func.count()).select_from(PaperVersionRow)) or 0
    return {
        "assets_total": len(assets),
        "assets_bytes": canonical_bytes,
        "paper_versions_total": versions,
        "duplicate_objects": duplicates,
        "duplicate_bytes": duplicate_bytes,
        "dedup_effective": duplicate_bytes == 0,
    }

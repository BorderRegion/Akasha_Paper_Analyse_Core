"""operations.disk — disk state, low-space protection, breakdown (P10,
doc 07 §5-§8).

Low-space policy (doc 07 §8):
- at WARNING: reduce nonessential cache growth, surface DEGRADED storage
  health;
- at CRITICAL: block new T3 expansion, block large temporary rendering,
  keep minimal metadata/status operations working, never corrupt or
  partially overwrite canonical objects — raise RESOURCE_001.

The guard is checked BEFORE heavy work is planned or started, so a full
disk degrades the pipeline instead of failing halfway through it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from paperintel.config.settings import AppConfig, get_settings
from paperintel.errors import DomainError
from paperintel.schemas.enums import ResourceTier, RetentionClass

#: Directories (relative to the store data dir) that map to a retention
#: class (doc 07 §4): this is how a breakdown attributes bytes.
DATA_DIR_CLASSES: dict[str, RetentionClass] = {
    "objects": RetentionClass.KEEP,
    "cache": RetentionClass.CACHE,
    "temp": RetentionClass.TEMP,
    "tmp": RetentionClass.TEMP,  # recognize legacy files until reclaimed
    "debug": RetentionClass.DEBUG_TTL,
}


@dataclass(slots=True)
class DiskStatus:
    path: str
    total_bytes: int
    free_bytes: int
    used_bytes: int
    free_percent: float
    level: str  # OK | WARNING | CRITICAL
    reasons: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.level == "OK"


def disk_status(path: str | Path | None = None, *, settings: AppConfig | None = None) -> DiskStatus:
    """Current disk state for the store root, classified by the configured
    thresholds (percent thresholds OR absolute byte thresholds)."""
    settings = settings or get_settings()
    target = Path(path) if path is not None else Path(settings.core.data_dir)
    # Measure the nearest existing ancestor (a fresh store may not exist yet).
    probe = target
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    free_percent = (usage.free / usage.total * 100.0) if usage.total else 0.0

    policy = settings.disk
    reasons: list[str] = []
    level = "OK"

    if free_percent <= policy.critical_free_percent:
        level = "CRITICAL"
        reasons.append(
            f"free space {free_percent:.2f}% <= critical {policy.critical_free_percent}%"
        )
    elif free_percent <= policy.warning_free_percent:
        level = "WARNING"
        reasons.append(f"free space {free_percent:.2f}% <= warning {policy.warning_free_percent}%")

    if policy.critical_free_bytes is not None and usage.free <= policy.critical_free_bytes:
        level = "CRITICAL"
        reasons.append(f"free bytes {usage.free} <= critical {policy.critical_free_bytes}")
    elif (
        policy.warning_free_bytes is not None
        and usage.free <= policy.warning_free_bytes
        and level == "OK"
    ):
        level = "WARNING"
        reasons.append(f"free bytes {usage.free} <= warning {policy.warning_free_bytes}")

    return DiskStatus(
        path=str(target),
        total_bytes=usage.total,
        free_bytes=usage.free,
        used_bytes=usage.used,
        free_percent=round(free_percent, 4),
        level=level,
        reasons=reasons,
    )


def require_capacity(
    *,
    needed_bytes: int = 0,
    tier: ResourceTier | None = None,
    operation: str,
    path: str | Path | None = None,
    settings: AppConfig | None = None,
) -> DiskStatus:
    """Low-space gate for an operation about to allocate space.

    Raises RESOURCE_001 when the disk is CRITICAL and the work is
    nonessential (deep expansion or bulk temporary rendering); returns the
    (possibly WARNING) status otherwise so the caller can record DEGRADED
    health instead of failing outright.
    """
    status = disk_status(path, settings=settings)
    deep_work = tier is not None and tier is ResourceTier.T3_DEEP
    if status.level == "CRITICAL":
        if deep_work or needed_bytes > 0:
            raise DomainError(
                "RESOURCE_001",
                message=(
                    f"insufficient disk space for {operation}: "
                    f"{status.free_percent:.2f}% free "
                    f"({status.free_bytes} bytes). "
                    "Deep expansion and large temporary rendering are blocked "
                    "while the disk is critical."
                ),
                details={
                    "operation": operation,
                    "level": status.level,
                    "free_bytes": status.free_bytes,
                    "free_percent": status.free_percent,
                    "reasons": status.reasons,
                    "tier": tier.value if tier else None,
                },
            )
    return status


def directory_bytes(path: Path) -> int:
    """Total size of the regular files under a directory (missing → 0)."""
    if not path.exists():
        return 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file():
            try:
                total += entry.stat().st_size
            except OSError:  # pragma: no cover - transient FS races
                continue
    return total


def disk_report(data_dir: str | Path | None = None, *, settings: AppConfig | None = None) -> dict:
    """Disk breakdown by retention class + free space + health level."""
    settings = settings or get_settings()
    root = Path(data_dir) if data_dir is not None else Path(settings.core.data_dir)

    breakdown: dict[str, dict] = {}
    unattributed = 0
    for name, retention in DATA_DIR_CLASSES.items():
        directory = root / name
        size = directory_bytes(directory)
        entry = breakdown.setdefault(
            retention.value, {"retention_class": retention.value, "bytes": 0, "paths": []}
        )
        entry["bytes"] += size
        if directory.exists():
            entry["paths"].append(str(directory))

    known = [root / name for name in DATA_DIR_CLASSES]
    for entry in sorted(root.iterdir()) if root.exists() else []:
        if entry.is_dir() and entry not in known:
            unattributed += directory_bytes(entry)

    status = disk_status(root, settings=settings)
    total_attributed = sum(entry["bytes"] for entry in breakdown.values())
    return {
        "data_dir": str(root),
        "store_bytes": total_attributed + unattributed,
        "unattributed_bytes": unattributed,
        "by_retention_class": sorted(
            breakdown.values(), key=lambda entry: entry["retention_class"]
        ),
        "disk": {
            "path": status.path,
            "total_bytes": status.total_bytes,
            "free_bytes": status.free_bytes,
            "used_bytes": status.used_bytes,
            "free_percent": status.free_percent,
            "level": status.level,
            "reasons": status.reasons,
        },
        "policy": {
            "warning_free_percent": settings.disk.warning_free_percent,
            "critical_free_percent": settings.disk.critical_free_percent,
            "warning_free_bytes": settings.disk.warning_free_bytes,
            "critical_free_bytes": settings.disk.critical_free_bytes,
            "temp_max_age_hours": settings.disk.temp_max_age_hours,
            "debug_ttl_days": settings.disk.debug_ttl_days,
        },
    }

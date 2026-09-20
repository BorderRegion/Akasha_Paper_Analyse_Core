"""Local content-addressed object store (spec doc 01 §6.2, doc 02 §1.4/§1.17).

Canonical object key layout::

    <root>/<kind_dir>/sha256/<first2>/<full_sha256>

Guarantees:
- content-addressed: the sha256 of the bytes is the identity;
- deduplicated: storing identical bytes twice yields one canonical object;
- atomic: writes land via a same-directory temp file + ``os.replace``; an
  interrupted write can only leave a ``.tmp-*`` scratch file, never a corrupt
  canonical object;
- verified: reads re-hash the content; a mismatch raises STORAGE_002 instead
  of returning corrupt bytes;
- retention-aware: every object belongs to a kind with a default retention
  class (canonical metadata lives in the assets table).

Temporary page rasters live under ``data/temp`` / ``data/cache`` and MUST NOT
become canonical data (spec doc 01 §6.3).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from pathlib import Path

from paperintel.errors import DomainError
from paperintel.providers.base import StorageProvider
from paperintel.schemas.enums import AssetKind, RetentionClass
from paperintel.schemas.health import ModuleHealthRecord

#: Default retention class per object kind (spec doc 07 §4). The assets table
#: stores the authoritative retention metadata; this mapping is the default.
RETENTION_BY_KIND: dict[AssetKind, RetentionClass] = {
    AssetKind.PDF: RetentionClass.KEEP,
    AssetKind.FIGURE: RetentionClass.KEEP,
    AssetKind.TABLE_IMAGE: RetentionClass.KEEP,
    AssetKind.EVIDENCE_CROP: RetentionClass.KEEP,
    AssetKind.LLM_OUTPUT: RetentionClass.KEEP,
    AssetKind.DEBUG: RetentionClass.DEBUG_TTL,
}

_TMP_PREFIX = ".tmp-"
_KEY_RE = re.compile(r"^(?P<kind>[a-z_]+)/sha256/(?P<first2>[0-9a-f]{2})/(?P<sha>[0-9a-f]{64})$")
_CHUNK = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def storage_key_for(sha256_hex: str, kind: AssetKind) -> str:
    return f"{kind.value}/{object_key_for(sha256_hex)}"


def object_key_for(sha256_hex: str) -> str:
    """Frozen logical key relative to its kind directory (doc 01 §6.2).

    storage_key qualifies this reference within data/objects, preserving old
    asset rows without moving or overwriting their immutable contents.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", sha256_hex):
        raise DomainError("STORAGE_003", message="Invalid content hash.")
    return f"sha256/{sha256_hex[:2]}/{sha256_hex}"


def parse_storage_key(storage_key: str) -> tuple[AssetKind, str]:
    """Split a canonical storage key into (kind, sha256).

    Raises:
        DomainError: STORAGE_003 when the key is malformed.
    """
    match = _KEY_RE.fullmatch(storage_key)
    if match is None:
        raise DomainError(
            "STORAGE_003",
            message="Object key is malformed or does not exist.",
            details={"storage_key": storage_key},
        )
    try:
        kind = AssetKind(match.group("kind"))
    except ValueError as exc:
        raise DomainError("STORAGE_003", message="Unknown object kind.") from exc
    sha = match.group("sha")
    if match.group("first2") != sha[:2]:
        raise DomainError(
            "STORAGE_003",
            message="Object key is malformed or does not exist.",
            details={"storage_key": storage_key},
        )
    return kind, sha


def resolve_storage_path(root: str | os.PathLike[str], storage_key: str) -> Path:
    """Validate a read path without creating directories; reject symlink escapes."""
    kind, sha = parse_storage_key(storage_key)
    root_path = Path(root).resolve()
    path = (root_path / kind.value / "sha256" / sha[:2] / sha).resolve()
    if not path.is_relative_to(root_path):
        raise DomainError("STORAGE_003", message="Object path escapes the store.")
    return path


class LocalObjectStore(StorageProvider):
    """Canonical local object store implementation."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        provider_id: str = "prv_local_object_store",
        temp_dir: str | os.PathLike[str] | None = None,
        warning_free_percent: float = 15.0,
        critical_free_percent: float = 5.0,
    ) -> None:
        super().__init__(provider_id)
        self.root = Path(root)
        self.temp_dir = Path(temp_dir) if temp_dir else self.root.parent / "temp"
        self.warning_free_percent = warning_free_percent
        self.critical_free_percent = critical_free_percent
        self.root.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        for kind in AssetKind:
            (self.root / kind.value).mkdir(parents=True, exist_ok=True)

    # -- paths --------------------------------------------------------------

    def path_for(self, storage_key: str) -> Path:
        kind, sha = parse_storage_key(storage_key)
        return self.root / kind.value / "sha256" / sha[:2] / sha

    # -- disk policy ----------------------------------------------------------

    def disk_free_percent(self) -> float:
        usage = shutil.disk_usage(self.root)
        return usage.free / usage.total * 100.0

    def _guard_free_space(self) -> None:
        """Block canonical writes at the critical low-water mark (doc 07 §8).

        Raises RESOURCE_001; never corrupts or partially overwrites objects.
        """
        free_percent = self.disk_free_percent()
        if free_percent < self.critical_free_percent:
            raise DomainError(
                "RESOURCE_001",
                message="Disk low-water mark reached; canonical writes are blocked.",
                details={
                    "free_percent": round(free_percent, 2),
                    "critical_free_percent": self.critical_free_percent,
                },
            )

    # -- writes ----------------------------------------------------------------

    def put(self, data: bytes, *, kind: str) -> tuple[str, str]:
        """Store bytes; returns (sha256_hex, storage_key). Idempotent + dedup."""
        try:
            asset_kind = AssetKind(kind)
        except ValueError:
            raise DomainError(
                "STORAGE_001",
                message=f"Unknown object kind {kind!r}.",
                details={"kind": kind},
            ) from None
        self._guard_free_space()
        sha = sha256_bytes(data)
        key = storage_key_for(sha, asset_kind)
        final_path = self.path_for(key)
        if final_path.exists():
            # A pre-existing path is not proof of integrity.
            self.get(key)
            return sha, key
        self._atomic_write(final_path, data)
        return sha, key

    def put_file(self, source: str | os.PathLike[str], *, kind: str) -> tuple[str, str]:
        """Stream a file into the store without loading it fully into memory."""
        try:
            asset_kind = AssetKind(kind)
        except ValueError:
            raise DomainError(
                "STORAGE_001",
                message=f"Unknown object kind {kind!r}.",
                details={"kind": kind},
            ) from None
        source_path = Path(source)
        if not source_path.is_file():
            raise DomainError(
                "STORAGE_001",
                message="Source file does not exist.",
                details={"source": str(source_path)},
            )
        self._guard_free_space()
        sha = sha256_file(source_path)
        key = storage_key_for(sha, asset_kind)
        final_path = self.path_for(key)
        if final_path.exists():
            return sha, key
        tmp_path = self._temp_sibling(final_path)
        try:
            with source_path.open("rb") as src, tmp_path.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=_CHUNK)
                dst.flush()
                os.fsync(dst.fileno())
            self._finalize(tmp_path, final_path, sha)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return sha, key

    def _temp_sibling(self, final_path: Path) -> Path:
        final_path.parent.mkdir(parents=True, exist_ok=True)
        return final_path.parent / f"{_TMP_PREFIX}{uuid.uuid4().hex}"

    def _atomic_write(self, final_path: Path, data: bytes) -> None:
        tmp_path = self._temp_sibling(final_path)
        try:
            with tmp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            self._finalize(tmp_path, final_path, sha256_bytes(data))
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _finalize(self, tmp_path: Path, final_path: Path, expected_sha: str) -> None:
        """Atomically publish the temp file and verify content integrity."""
        try:
            os.replace(tmp_path, final_path)
        except OSError as exc:
            raise DomainError(
                "STORAGE_001",
                message="Object write failed during atomic publish.",
                details={"target": str(final_path), "reason": type(exc).__name__},
            ) from exc
        actual_sha = sha256_file(final_path)
        if actual_sha != expected_sha:
            # Never leave a corrupt canonical object behind.
            final_path.unlink(missing_ok=True)
            raise DomainError(
                "STORAGE_002",
                message="Object content hash mismatch after write; object was removed.",
                details={"target": str(final_path), "expected": expected_sha},
            )

    # -- reads ------------------------------------------------------------------

    def get(self, storage_key: str) -> bytes:
        """Read and verify a canonical object.

        Raises:
            DomainError: STORAGE_003 when missing, STORAGE_002 on hash mismatch.
        """
        _kind, sha = parse_storage_key(storage_key)
        path = self.path_for(storage_key)
        if not path.is_file():
            raise DomainError(
                "STORAGE_003",
                message="Object not found.",
                details={"storage_key": storage_key},
            )
        data = path.read_bytes()
        actual = sha256_bytes(data)
        if actual != sha:
            raise DomainError(
                "STORAGE_002",
                message="Object content hash mismatch; stored object is corrupt.",
                details={"storage_key": storage_key, "expected": sha, "actual": actual},
            )
        return data

    def exists(self, storage_key: str) -> bool:
        try:
            return self.path_for(storage_key).is_file()
        except DomainError:
            return False

    def delete(self, storage_key: str) -> bool:
        """Delete one object. Returns True when a file was removed.

        GC policy (spec doc 07 §7) decides *whether* an object may be deleted;
        this primitive only executes it. Canonical referenced assets are never
        deleted by ordinary GC.
        """
        path = self.path_for(storage_key)
        if path.is_file():
            path.unlink()
            return True
        return False

    # -- maintenance ------------------------------------------------------------

    def sweep_temp_files(self, *, prefix: str = _TMP_PREFIX) -> int:
        """Remove interrupted-write scratch files from the canonical tree.

        Only files with the reserved temp prefix are touched; canonical
        objects never carry it. Returns the number of files removed.
        """
        removed = 0
        for path in self.root.rglob(f"{prefix}*"):
            if path.is_file():
                path.unlink()
                removed += 1
        return removed

    def disk_usage_by_kind(self) -> dict[str, int]:
        usage: dict[str, int] = {}
        for kind in AssetKind:
            kind_dir = self.root / kind.value
            total = sum(path.stat().st_size for path in kind_dir.rglob("*") if path.is_file())
            usage[kind.value] = total
        return usage

    def object_count(self) -> int:
        return sum(
            1
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.startswith(_TMP_PREFIX)
        )

    # -- health --------------------------------------------------------------------

    async def health(self) -> ModuleHealthRecord:
        """Cheap probe: temp object write/read/delete + free-space threshold."""
        from paperintel.schemas.enums import ModuleHealthState  # noqa: PLC0415

        checks: dict[str, str] = {}
        state = ModuleHealthState.HEALTHY
        last_error: dict | None = None
        probe_path = self.temp_dir / f"{_TMP_PREFIX}health-probe-{uuid.uuid4().hex}"
        try:
            probe_path.write_bytes(b"paperintel-health-probe")
            read_back = probe_path.read_bytes()
            checks["probe_write_read"] = (
                "PASS" if read_back == b"paperintel-health-probe" else "FAIL"
            )
            probe_path.unlink(missing_ok=True)
            checks["probe_delete"] = "PASS" if not probe_path.exists() else "FAIL"
        except OSError as exc:
            checks["probe_write_read"] = "FAIL"
            state = ModuleHealthState.UNAVAILABLE
            last_error = {"type": type(exc).__name__, "message": str(exc)}

        free_percent = self.disk_free_percent()
        if free_percent < self.critical_free_percent:
            checks["free_space"] = "FAIL"
            state = ModuleHealthState.DEGRADED
            last_error = last_error or {
                "error_code": "RESOURCE_001",
                "free_percent": round(free_percent, 2),
            }
        elif free_percent < self.warning_free_percent:
            checks["free_space"] = "WARN"
            if state is ModuleHealthState.HEALTHY:
                state = ModuleHealthState.DEGRADED
        else:
            checks["free_space"] = "PASS"

        if any(result == "FAIL" for result in checks.values()):
            state = ModuleHealthState.FAILED if state is ModuleHealthState.HEALTHY else state

        return ModuleHealthRecord(
            module_id="storage.object_store",
            state=state,
            checks=checks,
            metrics={
                "free_percent": round(free_percent, 2),
                "objects_total": self.object_count(),
            },
            last_error=last_error,
        )


__all__ = [
    "RETENTION_BY_KIND",
    "LocalObjectStore",
    "parse_storage_key",
    "sha256_bytes",
    "sha256_file",
    "storage_key_for",
]

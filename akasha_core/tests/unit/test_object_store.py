"""Local object store tests (P01: hashing, dedup, atomic writes, retention)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from paperintel.errors import DomainError
from paperintel.schemas.enums import AssetKind, ModuleHealthState, RetentionClass
from paperintel.storage.object_store import (
    RETENTION_BY_KIND,
    LocalObjectStore,
    parse_storage_key,
    sha256_bytes,
    storage_key_for,
)


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects", temp_dir=tmp_path / "temp")


def test_key_layout_is_content_addressed(store: LocalObjectStore) -> None:
    data = b"%PDF-1.5 test bytes"
    sha, key = store.put(data, kind="pdf")
    assert sha == sha256_bytes(data)
    assert key == f"pdf/sha256/{sha[:2]}/{sha}"
    path = store.path_for(key)
    assert path.is_file()
    assert path.read_bytes() == data
    # Layout: <root>/<kind>/sha256/<first2>/<full>
    assert path.parent.name == sha[:2]
    assert path.parent.parent.name == "sha256"


def test_roundtrip_get_verifies_hash(store: LocalObjectStore) -> None:
    data = b"evidence crop bytes"
    _sha, key = store.put(data, kind="evidence_crops")
    assert store.get(key) == data
    assert store.exists(key)


def test_dedup_same_content_single_object(store: LocalObjectStore) -> None:
    data = b"identical pdf content"
    sha1, key1 = store.put(data, kind="pdf")
    sha2, key2 = store.put(data, kind="pdf")
    assert (sha1, key1) == (sha2, key2)
    objects = list((store.root / "pdf").rglob("*"))
    files = [p for p in objects if p.is_file()]
    assert len(files) == 1


def test_hash_mismatch_detected_on_read(store: LocalObjectStore) -> None:
    data = b"canonical bytes"
    _sha, key = store.put(data, kind="pdf")
    path = store.path_for(key)
    path.write_bytes(b"corrupted bytes!")  # simulate bit rot / bad actor
    with pytest.raises(DomainError) as excinfo:
        store.get(key)
    assert excinfo.value.code == "STORAGE_002"


def test_dedup_does_not_silently_reuse_corrupted_object(store: LocalObjectStore) -> None:
    data = b"canonical bytes"
    _, key = store.put(data, kind="pdf")
    store.path_for(key).write_bytes(b"damaged")
    with pytest.raises(DomainError) as error:
        store.put(data, kind="pdf")
    assert error.value.code == "STORAGE_002"
    assert store.path_for(key).read_bytes() == b"damaged"


def test_missing_object_raises_storage_003(store: LocalObjectStore) -> None:
    key = storage_key_for("f" * 64, AssetKind.PDF)
    assert not store.exists(key)
    with pytest.raises(DomainError) as excinfo:
        store.get(key)
    assert excinfo.value.code == "STORAGE_003"


def test_malformed_key_rejected(store: LocalObjectStore) -> None:
    for bad in ("", "pdf", "pdf/sha256/zz/" + "a" * 64, "../etc/passwd", "pdf/sha256/aa/short"):
        with pytest.raises(DomainError) as excinfo:
            store.get(bad)
        assert excinfo.value.code == "STORAGE_003"


def test_path_traversal_key_rejected(store: LocalObjectStore) -> None:
    with pytest.raises(DomainError):
        parse_storage_key("pdf/sha256/aa/..%2f..%2fsecret")
    # Even a crafted existing-looking key cannot escape the root:
    with pytest.raises(DomainError):
        store.path_for("pdf/../../secret")


def test_interrupted_write_leaves_no_corrupt_canonical_object(
    store: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If publishing fails mid-way, the canonical path must not exist and the
    scratch file must not masquerade as an object."""
    data = b"payload that never lands"
    sha = sha256_bytes(data)
    final_path = store.path_for(storage_key_for(sha, AssetKind.PDF))

    real_replace = os.replace

    def flaky_replace(src, dst, *args, **kwargs):
        if str(dst) == str(final_path):
            raise OSError("simulated power loss")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(DomainError) as excinfo:
        store.put(data, kind="pdf")
    assert excinfo.value.code == "STORAGE_001"
    assert not final_path.exists()
    # No temp litter remains after a failed put (cleanup on exception path).
    temps = [p for p in final_path.parent.glob(".tmp-*")] if final_path.parent.exists() else []
    assert temps == []

    # Recovery: after the interruption, a retry succeeds cleanly.
    monkeypatch.undo()
    sha2, key2 = store.put(data, kind="pdf")
    assert sha2 == sha
    assert store.get(key2) == data


def test_put_file_streaming_and_dedup(store: LocalObjectStore, tmp_path: Path) -> None:
    source = tmp_path / "big.pdf"
    source.write_bytes(b"x" * (3 * 1024 * 1024))
    sha, key = store.put_file(source, kind="pdf")
    assert sha == sha256_bytes(source.read_bytes())
    assert store.get(key) == source.read_bytes()
    # Dedup with put(): same content, same key, no second object.
    sha2, key2 = store.put(source.read_bytes(), kind="pdf")
    assert (sha2, key2) == (sha, key)


def test_unknown_kind_rejected(store: LocalObjectStore) -> None:
    with pytest.raises(DomainError) as excinfo:
        store.put(b"x", kind="not_a_kind")
    assert excinfo.value.code == "STORAGE_001"


def test_delete_and_sweep(store: LocalObjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    _sha, key = store.put(b"delete me", kind="debug")
    assert store.delete(key) is True
    assert store.delete(key) is False  # idempotent report

    # Interrupted-write litter is sweepable; canonical objects are untouched.
    sha2, key2 = store.put(b"keep me", kind="pdf")
    litter = store.root / "pdf" / "sha256" / sha2[:2] / ".tmp-orphan"
    litter.write_bytes(b"partial")
    removed = store.sweep_temp_files()
    assert removed == 1
    assert not litter.exists()
    assert store.get(key2) == b"keep me"


def test_low_disk_critical_blocks_writes(
    store: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "disk_free_percent", lambda: 1.0)
    with pytest.raises(DomainError) as excinfo:
        store.put(b"blocked", kind="pdf")
    assert excinfo.value.code == "RESOURCE_001"


def test_retention_defaults_match_spec_doc07() -> None:
    assert RETENTION_BY_KIND[AssetKind.PDF] is RetentionClass.KEEP
    assert RETENTION_BY_KIND[AssetKind.EVIDENCE_CROP] is RetentionClass.KEEP
    assert RETENTION_BY_KIND[AssetKind.LLM_OUTPUT] is RetentionClass.KEEP
    assert RETENTION_BY_KIND[AssetKind.DEBUG] is RetentionClass.DEBUG_TTL


def test_disk_usage_by_kind(store: LocalObjectStore) -> None:
    store.put(b"a" * 100, kind="pdf")
    store.put(b"b" * 50, kind="debug")
    usage = store.disk_usage_by_kind()
    assert usage["pdf"] == 100
    assert usage["debug"] == 50
    assert usage["figures"] == 0
    assert store.object_count() == 2


def test_health_probe_healthy(store: LocalObjectStore, monkeypatch: pytest.MonkeyPatch) -> None:
    # Environment-independent: the CI/dev machine may genuinely be low on disk.
    monkeypatch.setattr(store, "disk_free_percent", lambda: 90.0)
    record = asyncio.run(store.health())
    assert record.module_id == "storage.object_store"
    assert record.state is ModuleHealthState.HEALTHY
    assert record.checks["probe_write_read"] == "PASS"
    assert record.checks["free_space"] == "PASS"


def test_health_probe_degraded_on_warning_disk(
    store: LocalObjectStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "disk_free_percent", lambda: 10.0)  # < 15 warning
    record = asyncio.run(store.health())
    assert record.state is ModuleHealthState.DEGRADED
    assert record.checks["free_space"] == "WARN"

    monkeypatch.setattr(store, "disk_free_percent", lambda: 2.0)  # < 5 critical
    record = asyncio.run(store.health())
    assert record.state is ModuleHealthState.DEGRADED
    assert record.checks["free_space"] == "FAIL"
    assert record.last_error is not None
    assert record.last_error["error_code"] == "RESOURCE_001"

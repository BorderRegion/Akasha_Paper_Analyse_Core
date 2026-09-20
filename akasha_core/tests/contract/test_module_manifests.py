"""All module manifests validate (P00 gate check P00-C08)."""

from __future__ import annotations

import re

import pytest
import yaml

from paperintel.errors import DomainError
from paperintel.errors.catalog import ERROR_NAMESPACES
from paperintel.modules import (
    load_bundled_manifests,
    load_manifest_file,
    validate_manifest_data,
)
from paperintel.version import SPEC_VERSION


def test_bundled_manifests_load_and_are_unique() -> None:
    manifests = load_bundled_manifests()
    assert manifests, "no bundled manifests found"
    ids = [manifest.module_id for manifest in manifests.values()]
    assert len(ids) == len(set(ids))
    for module_id, manifest in manifests.items():
        assert manifest.module_id == module_id
        assert manifest.version == SPEC_VERSION


def test_bundled_manifest_ids_use_frozen_namespaces() -> None:
    """Top-level module namespace must come from spec doc 02 §4."""
    frozen_top_level = {
        "config",
        "storage",
        "database",
        "providers",
        "ingest",
        "extraction",
        "ocr",
        "structure",
        "evidence",
        "triage",
        "workflow",
        "agents",
        "verification",
        "knowledge",
        "search",
        "graph",
        "corpus",
        "operations",
        "mcp",
        "cli",
        "web",
    }
    manifests = load_bundled_manifests()
    for module_id in manifests:
        top = module_id.split(".", 1)[0]
        assert top in frozen_top_level, f"{module_id}: non-frozen top-level namespace {top}"


def test_manifest_error_namespaces_are_frozen() -> None:
    manifests = load_bundled_manifests()
    for manifest in manifests.values():
        for namespace in manifest.error_namespaces:
            assert namespace in ERROR_NAMESPACES


def test_manifest_healthcheck_declared() -> None:
    manifests = load_bundled_manifests()
    for manifest in manifests.values():
        assert manifest.healthcheck.name
        # internal probes use snake_case names; command probes may carry a
        # short command line (e.g. "paperctl version").
        assert re.fullmatch(r"[a-z0-9_. -]+", manifest.healthcheck.name)


def test_invalid_manifest_rejected() -> None:
    with pytest.raises(DomainError) as excinfo:
        validate_manifest_data({"module_id": "NoDots", "version": "1.0.0"}, source="test")
    assert excinfo.value.code == "CFG_002"

    with pytest.raises(DomainError):
        validate_manifest_data(
            {
                "module_id": "extraction.pdf",
                "version": "1.0.0",
                "healthcheck": {"type": "internal", "name": "x"},
                "error_namespaces": ["NOT_A_NAMESPACE"],
            },
            source="test",
        )

    with pytest.raises(DomainError):
        validate_manifest_data(
            {
                "module_id": "extraction.pdf",
                "version": "1.0.0",
                "healthcheck": {"type": "telepathy", "name": "x"},
            },
            source="test",
        )


def test_missing_manifest_file_raises(tmp_path) -> None:
    with pytest.raises(DomainError) as excinfo:
        load_manifest_file(tmp_path / "absent.yaml")
    assert excinfo.value.code == "CFG_001"


def test_manifest_file_roundtrip(tmp_path) -> None:
    path = tmp_path / "m.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "module_id": "verification.numeric",
                "version": "1.0.0",
                "inputs": ["Claim[]"],
                "outputs": ["Verification[]"],
                "dependencies": ["database"],
                "healthcheck": {"type": "internal", "name": "numeric_fixture"},
                "idempotent": True,
                "required_checks": ["numeric_fixture"],
                "error_namespaces": ["VERIFY"],
            }
        ),
        encoding="utf-8",
    )
    manifest = load_manifest_file(path)
    assert manifest.module_id == "verification.numeric"

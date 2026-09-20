"""Module manifest registry (spec doc 02 §4, templates/module_manifest.example.yaml).

Every deployable module ships a YAML manifest declaring its identity,
inputs/outputs, dependencies, healthcheck, idempotency and error namespaces.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from paperintel.errors import DomainError
from paperintel.schemas.operations import ModuleManifest

_MANIFEST_PACKAGE_DIR = "manifests"


def validate_manifest_data(data: object, *, source: str) -> ModuleManifest:
    """Validate one manifest document.

    Raises:
        DomainError: CFG_002 with structured details when invalid.
    """
    try:
        return ModuleManifest.model_validate(data)
    except ValidationError as exc:
        raise DomainError(
            "CFG_002",
            message=f"Invalid module manifest: {source}",
            details={
                "source": source,
                "errors": [
                    {
                        "loc": ".".join(str(part) for part in err["loc"]),
                        "type": err["type"],
                        "msg": err["msg"],
                    }
                    for err in exc.errors()
                ],
            },
        ) from exc


def load_manifest_file(path: str | Path) -> ModuleManifest:
    path = Path(path)
    if not path.is_file():
        raise DomainError(
            "CFG_001",
            message=f"Module manifest not found: {path}",
            details={"path": str(path)},
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return validate_manifest_data(data, source=str(path))


def load_bundled_manifests() -> dict[str, ModuleManifest]:
    """Load every manifest bundled with the package, keyed by module_id.

    Raises:
        DomainError: CFG_002 when two manifests declare the same module_id.
    """
    manifests: dict[str, ModuleManifest] = {}
    package_dir = resources.files("paperintel.modules") / _MANIFEST_PACKAGE_DIR
    for resource in sorted(package_dir.iterdir(), key=lambda p: p.name):
        if resource.name.endswith((".yaml", ".yml")):
            manifest = validate_manifest_data(
                yaml.safe_load(resource.read_text(encoding="utf-8")),
                source=f"paperintel.modules/{_MANIFEST_PACKAGE_DIR}/{resource.name}",
            )
            if manifest.module_id in manifests:
                raise DomainError(
                    "CFG_002",
                    message=f"Duplicate module_id {manifest.module_id!r} in bundled manifests.",
                    details={"module_id": manifest.module_id},
                )
            manifests[manifest.module_id] = manifest
    return manifests


__all__ = ["load_bundled_manifests", "load_manifest_file", "validate_manifest_data"]

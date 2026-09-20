"""Canonical SHA-256 of effective non-secret analysis configuration."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from paperintel.config.settings import get_settings
from paperintel.version import PIPELINE_VERSION, SPEC_VERSION


def analysis_config_hash(*, settings=None, **policy):
    from paperintel.agents.prompts import BUILTIN_PROMPTS
    from paperintel.config.provider_config import load_provider_config

    settings = settings or get_settings()
    providers = (
        load_provider_config(settings.providers_file).model_dump(mode="json")
        if settings.providers_file
        else {}
    )
    payload = {
        "spec_version": SPEC_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "providers": providers,
        "prompts": {
            f"{name}@{version}": spec.body for (name, version), spec in BUILTIN_PROMPTS.items()
        },
        "policy": policy,
        "concurrency": settings.concurrency.model_dump(mode="json"),
    }
    # Persist the exact non-secret configuration whose digest enters replay keys.
    from paperintel.operations.debug import redact
    from paperintel.providers.runtime import runtime_root

    payload = redact(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    root = Path(runtime_root.get() or settings.core.data_dir) / "operations" / "configurations"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{digest}.json"
    if not target.exists():
        descriptor, temporary = tempfile.mkstemp(prefix="config-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return digest

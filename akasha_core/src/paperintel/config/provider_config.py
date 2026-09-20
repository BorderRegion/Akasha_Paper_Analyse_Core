"""Provider configuration loading (spec templates/provider_config.example.yaml).

Rules:
- ``${ENV_VAR}`` references are expanded before validation; a missing
  environment variable raises CFG_001 (never silently falls back);
- API keys are referenced by env-var name only and are never expanded into
  the config object, logged, or fingerprinted;
- the sanitized fingerprint (sha256 over the normalized, secret-free document)
  is what debug bundles may carry.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from paperintel.errors import DomainError
from paperintel.schemas.operations import ProviderConfigFile, ProviderEntry

_ENV_REF_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_ENV_REF_INLINE_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _expand_env(value: Any, path: str, env: dict[str, str]) -> Any:
    if isinstance(value, str):
        full_match = _ENV_REF_RE.match(value)
        if full_match:
            name = full_match.group(1)
            if name not in env:
                raise DomainError(
                    "CFG_001",
                    message=f"Missing required environment variable ${{{name}}} for {path}.",
                    details={"config_path": path, "env_var": name},
                )
            return env[name]

        def _sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in env:
                raise DomainError(
                    "CFG_001",
                    message=f"Missing required environment variable ${{{name}}} for {path}.",
                    details={"config_path": path, "env_var": name},
                )
            return env[name]

        return _ENV_REF_INLINE_RE.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v, f"{path}.{k}", env) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v, f"{path}[{i}]", env) for i, v in enumerate(value)]
    return value


def load_provider_config(
    path: str | os.PathLike[str],
    *,
    env: dict[str, str] | None = None,
) -> ProviderConfigFile:
    """Parse, expand and validate a providers.yaml document.

    Raises:
        DomainError: CFG_001 for missing file/env vars, CFG_002 for invalid
            structure or values.
    """
    config_path = Path(path)
    if not config_path.is_file():
        raise DomainError(
            "CFG_001",
            message=f"Provider configuration file not found: {config_path}",
            details={"path": str(config_path)},
        )
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise DomainError(
            "CFG_002",
            message="Provider configuration must be a YAML mapping.",
            details={"path": str(config_path)},
        )
    environment = dict(os.environ) if env is None else env
    expanded = _expand_env(loaded, str(config_path), environment)
    try:
        return ProviderConfigFile.model_validate(expanded)
    except ValidationError as exc:
        raise DomainError(
            "CFG_002",
            message="Invalid provider configuration.",
            details={
                "path": str(config_path),
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


def resolve_api_key(entry: ProviderEntry, env: dict[str, str] | None = None) -> str | None:
    """Return the provider API key from the environment, or None when unset.

    The key is never copied into configuration objects, logs, or databases.
    """
    if entry.api_key_env is None:
        return None
    environment = os.environ if env is None else env
    value = environment.get(entry.api_key_env)
    return value or None


def provider_config_fingerprint(config: ProviderConfigFile) -> str:
    """sha256 over the normalized secret-free document (for debug bundles).

    api_key_env NAMES are part of the fingerprint (they are not secrets);
    key VALUES are never touched here.
    """
    normalized = json.dumps(
        config.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


__all__ = [
    "load_provider_config",
    "provider_config_fingerprint",
    "resolve_api_key",
]

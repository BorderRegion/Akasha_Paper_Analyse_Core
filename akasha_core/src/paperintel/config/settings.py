"""Application settings (P00).

Configuration model rules:
- critical sections reject unknown keys (``extra="forbid"``) → CFG_002;
- invalid values are reported with field paths → CFG_002;
- missing required values raise CFG_001;
- environment variables override config-file values;
- secrets are referenced by environment variable name and never stored in
  config files, logs, or database rows (spec doc 01 §22).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from paperintel.errors import DomainError

#: Environment variable pointing at the YAML config file.
CONFIG_PATH_ENV = "PAPERINTEL_CONFIG"

#: Environment overrides recognized without a prefix (also settable in .env).
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "core.env": ("PAPERINTEL_ENV",),
    "core.data_dir": ("PAPERINTEL_DATA_DIR",),
    "core.log_level": ("PAPERINTEL_LOG_LEVEL",),
    "database.url": ("DATABASE_URL",),
    "redis.url": ("REDIS_URL",),
    "celery.broker_url": ("CELERY_BROKER_URL",),
    "celery.result_backend": ("CELERY_RESULT_BACKEND",),
    "api.host": ("API_HOST",),
    "api.port": ("API_PORT",),
    "api.token": ("API_TOKEN",),
    "ui.upload_file_bytes": ("PAPERINTEL_UI_UPLOAD_FILE_BYTES",),
    "ui.upload_batch_bytes": ("PAPERINTEL_UI_UPLOAD_BATCH_BYTES",),
    "ui.upload_files": ("PAPERINTEL_UI_UPLOAD_FILES",),
    "ui.library_page_size": ("PAPERINTEL_UI_LIBRARY_PAGE_SIZE",),
    "disk.warning_free_percent": ("DISK_WARNING_FREE_PERCENT",),
    "disk.critical_free_percent": ("DISK_CRITICAL_FREE_PERCENT",),
    "disk.warning_free_bytes": ("DISK_WARNING_FREE_BYTES",),
    "disk.critical_free_bytes": ("DISK_CRITICAL_FREE_BYTES",),
    "providers_file": ("PAPERINTEL_PROVIDERS_FILE",),
}


class _StrictSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoreSettings(_StrictSection):
    env: str = "dev"
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    log_json: bool = True


class DatabaseSettings(_StrictSection):
    url: str = "postgresql+psycopg://paperintel:paperintel@127.0.0.1:5432/paperintel"
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=20, ge=0)
    echo: bool = False


class RedisSettings(_StrictSection):
    url: str = "redis://127.0.0.1:6379/0"


class CelerySettings(_StrictSection):
    broker_url: str = "redis://127.0.0.1:6379/1"
    result_backend: str = "redis://127.0.0.1:6379/2"
    task_acks_late: bool = True
    worker_prefetch_multiplier: int = Field(default=1, ge=1)


class ApiSettings(_StrictSection):
    host: str = "127.0.0.1"
    port: int = Field(default=8420, ge=1, le=65535)
    #: Local token authentication is sufficient for v1.0 (spec doc 04 §16).
    token: str | None = None


class UiSettings(_StrictSection):
    """Workbench product limits (docs/06 §导入: configurable initial values).

    These are the numbers GET /v1/ui/capabilities advertises AND the numbers the
    upload path enforces — one source of truth, so a deployment cannot advertise
    a limit it does not apply. They are product limits, never server config.
    """

    upload_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    upload_batch_bytes: int = Field(default=500 * 1024 * 1024, ge=1)
    upload_files: int = Field(default=50, ge=1, le=1000)
    library_page_size: int = Field(default=50, ge=1, le=100)


class DiskPolicySettings(_StrictSection):
    """Low-disk protection thresholds (spec doc 07 §8)."""

    warning_free_percent: float = Field(default=15.0, ge=0.0, le=100.0)
    critical_free_percent: float = Field(default=5.0, ge=0.0, le=100.0)
    warning_free_bytes: int | None = Field(default=None, ge=0)
    critical_free_bytes: int | None = Field(default=None, ge=0)
    debug_ttl_days: int = Field(default=14, ge=0)
    temp_max_age_hours: int = Field(default=24, ge=0)


class AppConfig(BaseModel):
    """Root configuration document. Unknown top-level or section keys are
    rejected (frozen critical-key policy)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    core: CoreSettings = Field(default_factory=CoreSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    ui: UiSettings = Field(default_factory=lambda: UiSettings())
    disk: DiskPolicySettings = Field(default_factory=DiskPolicySettings)
    concurrency: ConcurrencySettings = Field(default_factory=lambda: ConcurrencySettings())
    #: Path to providers.yaml (provider entries live in their own document,
    #: matching spec templates/provider_config.example.yaml).
    providers_file: Path | None = None


class ConcurrencySettings(_StrictSection):
    """Deployment-wide slot counts; all local workers share core.data_dir."""

    llm_analyst: int = Field(default=8, ge=1, le=256)
    llm_verifier: int = Field(default=4, ge=1, le=256)
    llm_synthesizer: int = Field(default=2, ge=1, le=256)
    ocr: int = Field(default=2, ge=1, le=256)
    embedding: int = Field(default=4, ge=1, le=256)
    metadata: int = Field(default=2, ge=1, le=256)


AppConfig.model_rebuild()


def _wrap_validation_error(exc: ValidationError, source: str) -> DomainError:
    details = {
        "source": source,
        "errors": [
            {
                "loc": ".".join(str(part) for part in err["loc"]),
                "type": err["type"],
                "msg": err["msg"],
            }
            for err in exc.errors()
        ],
    }
    unknown = [e for e in exc.errors() if e["type"] == "extra_forbidden"]
    if unknown:
        keys = [e["loc"] for e in unknown]
        return DomainError(
            "CFG_002",
            message=f"Unknown configuration keys rejected: {keys}",
            details=details,
        )
    return DomainError("CFG_002", message="Invalid configuration value.", details=details)


def _set_dotted(config: dict[str, Any], dotted: str, value: Any) -> None:
    section, dot, key = dotted.partition(".")
    if not dot:
        # Top-level key (e.g. providers_file) — set directly.
        config[section] = value
        return
    config.setdefault(section, {})[key] = value


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    for dotted, env_names in _ENV_ALIASES.items():
        for env_name in env_names:
            value = os.environ.get(env_name)
            if value is not None and value != "":
                _set_dotted(raw, dotted, value)
                break
    return raw


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """Load configuration from YAML (optional) + environment overrides.

    Raises:
        DomainError: CFG_001 when an explicitly referenced config file is
            missing; CFG_002 when keys/values are unknown or invalid.
    """
    config_path = Path(path) if path is not None else None
    if config_path is None:
        env_path = os.environ.get(CONFIG_PATH_ENV)
        if env_path:
            config_path = Path(env_path)

    raw: dict[str, Any] = {}
    source = "defaults+env"
    if config_path is not None:
        if not config_path.is_file():
            raise DomainError(
                "CFG_001",
                message=f"Configuration file not found: {config_path}",
                details={"path": str(config_path)},
            )
        source = str(config_path)
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise DomainError(
                "CFG_002",
                message="Configuration file must contain a YAML mapping.",
                details={"path": source},
            )
        raw = loaded

    raw = _apply_env_overrides(raw)

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise _wrap_validation_error(exc, source) from exc


@lru_cache(maxsize=1)
def get_settings() -> AppConfig:
    """Process-wide cached settings (call ``reset_settings_cache`` in tests)."""
    return load_config()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


__all__ = [
    "CONFIG_PATH_ENV",
    "ApiSettings",
    "AppConfig",
    "CelerySettings",
    "CoreSettings",
    "DatabaseSettings",
    "DiskPolicySettings",
    "RedisSettings",
    "UiSettings",
    "get_settings",
    "load_config",
    "reset_settings_cache",
]

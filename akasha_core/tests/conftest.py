"""Shared pytest configuration and fixtures.

Tests must be deterministic (spec doc 06 §17). Mock/golden tests never touch
the network; integration tests requiring services are marked needs_db /
needs_redis and skipped automatically when the service is unreachable.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_DIR = REPO_ROOT / "spec" / "paperintel_final_spec"
TEMPLATES_DIR = SPEC_DIR / "templates"


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every test at throwaway data dirs and neutral env defaults.

    Hermeticity: an operator may have sourced the gitignored .env into the
    shell (real credentials, log levels, model names). Every app-facing env
    variable is scrubbed per test so unit/contract results never depend on
    ambient operator configuration. Gate-control variables that steer pytest
    itself (strict mode, optional real-provider checks) are kept.
    """
    keep = {"PAPERINTEL_GATE_STRICT", "PAPERINTEL_REAL_PROVIDERS"}
    scrub_prefixes = (
        "PAPERINTEL_",
        "LLM_",
        "GATEWAY_",
        "PADDLE_OCR_",
        "DISK_",
        "API_",
        "CELERY_",
    )
    scrub_exact = {"DATABASE_URL", "REDIS_URL"}
    for name in list(os.environ):
        if name in keep:
            continue
        if name in scrub_exact or name.startswith(scrub_prefixes):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERINTEL_ENV", "test")
    monkeypatch.delenv("PAPERINTEL_CONFIG", raising=False)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def spec_dir() -> Path:
    return SPEC_DIR


@pytest.fixture(scope="session")
def templates_dir() -> Path:
    return TEMPLATES_DIR


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def db_available() -> bool:
    url = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://paperintel:paperintel@127.0.0.1:5432/paperintel",
    )
    if "@127.0.0.1:5432" in url or "@localhost:5432" in url:
        return _port_open("127.0.0.1", 5432)
    return False


@pytest.fixture(scope="session")
def redis_available() -> bool:
    return _port_open("127.0.0.1", 6379)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip service-dependent tests when the service is not reachable.

    When PAPERINTEL_GATE_STRICT=1 (phase-gate runs) skipping is disabled:
    service-dependent tests must execute against live infrastructure or fail
    loudly — a gate may never pass silently on skips (spec doc 00 §2: no
    silent fallbacks).
    """
    if os.environ.get("PAPERINTEL_GATE_STRICT") == "1":
        return
    skip_db = pytest.mark.skip(reason="PostgreSQL not reachable at 127.0.0.1:5432")
    skip_redis = pytest.mark.skip(reason="Redis not reachable at 127.0.0.1:6379")
    for item in items:
        if "needs_db" in item.keywords and not _port_open("127.0.0.1", 5432):
            item.add_marker(skip_db)
        if "needs_redis" in item.keywords and not _port_open("127.0.0.1", 6379):
            item.add_marker(skip_redis)

"""P08 integration fixtures (store / data_dir / imported paper).

Provider configuration is applied through ENV (config-driven resolution):
the workflow handlers build providers from the configured providers file,
never from injected objects, so tests configure them the same way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from tests.fixtures.generators import build_f01_native

from paperintel.ingest.service import import_pdf
from paperintel.storage.object_store import LocalObjectStore

REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def providers_env(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at the mock providers file and drop the settings cache
    (the autouse env scrub runs before this fixture, so the value sticks
    for the duration of the test)."""
    config_path = REPO_ROOT / "config" / "providers.mock.yaml"
    monkeypatch.setenv("PAPERINTEL_PROVIDERS_FILE", str(config_path))

    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()
    return config_path


@pytest.fixture()
def imported(session, store, data_dir, tmp_path, providers_env):
    """Imported F01 with the mock provider config in place."""
    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    session.commit()
    return result

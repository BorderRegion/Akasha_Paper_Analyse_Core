"""F00 preflight fixtures — real FastAPI + real PostgreSQL, no mocks.

Every test here runs against the same app object the CLI/UI serve and the
same disposable database the backend suites use, so "fix verified" means
verified through the real stack (spec doc 01 §范围与证据).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.generators import build_f01_native, build_f03_mixed

from paperintel.api.app import ApiState, create_app
from paperintel.ingest.service import import_pdf
from paperintel.storage.object_store import LocalObjectStore

REPO_ROOT = Path(__file__).parents[3]
FRONTEND_DIR = Path(
    # The workbench lives in its own repository (user instruction); its lock
    # file is part of the F00 baseline.
    __import__("os").environ.get("PAPERINTEL_FRONTEND_DIR", str(Path(__file__).resolve().parents[4] / "akasha_core_front"))
)


@pytest.fixture()
def api_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real app + disposable DB + mock providers (no network)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE", str(REPO_ROOT / "config" / "providers.mock.yaml")
    )
    from paperintel.config.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()
    state = ApiState(settings)
    app = create_app(settings, state=state)
    with TestClient(app) as client:
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield {"client": client, "state": state, "settings": settings, "data_dir": data_dir}
    state.engine.dispose()
    reset_settings_cache()


@pytest.fixture()
def import_pdf_into(api_env):
    """Import a generated PDF through the real ingest service + API session."""
    from tests.integration.p08.fixtures import ensure_run  # noqa: F401  (fixture parity)

    def _import(builder=build_f01_native, name: str = "f01.pdf", *, persist_evidence: bool = True):
        session = api_env["state"].session_factory()
        try:
            path = api_env["data_dir"] / name
            path.write_bytes(builder())
            store = LocalObjectStore(Path(api_env["settings"].core.data_dir) / "objects")
            result = asyncio.run(
                import_pdf(
                    path,
                    session=session,
                    store=store,
                    data_dir=Path(api_env["settings"].core.data_dir),
                    persist_evidence=persist_evidence,
                )
            )
            session.commit()
            return result
        finally:
            session.close()

    return _import


__all__ = [
    "FRONTEND_DIR",
    "REPO_ROOT",
    "api_env",
    "build_f01_native",
    "build_f03_mixed",
    "import_pdf_into",
]

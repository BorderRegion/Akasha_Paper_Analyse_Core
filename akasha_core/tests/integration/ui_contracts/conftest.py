"""F02 fixtures — real FastAPI + real PostgreSQL over the /v1/ui surface.

No mocks: every assertion goes through the same app object the browser talks to
and the same disposable database the backend suites use. Two environments are
provided on purpose:

- ``api_env``      — auth DISABLED (local single-user); requests are anonymous.
- ``token_env``    — auth ENABLED; the tests then exercise 401/CSRF/session.

``browser`` wraps ``token_env`` with the real session flow (POST /v1/ui/session,
cookie + CSRF) so the tests use the browser path rather than a shortcut.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.generators import build_f01_native, build_f03_mixed, build_f04_rich

from paperintel.api.app import ApiState, create_app
from paperintel.ingest.service import import_pdf
from paperintel.storage.object_store import LocalObjectStore

REPO_ROOT = Path(__file__).parents[3]
APP_TOKEN = "test-app-token-0123456789"


def _build_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token):
    data_dir = tmp_path / f"data-{token or 'anon'}"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE", str(REPO_ROOT / "config" / "providers.mock.yaml")
    )
    if token:
        monkeypatch.setenv("API_TOKEN", token)
    else:
        monkeypatch.delenv("API_TOKEN", raising=False)
    from paperintel.config.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()
    state = ApiState(settings)
    app = create_app(settings, state=state)
    return settings, state, app, data_dir


@pytest.fixture()
def api_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Auth-disabled environment (the documented local mode)."""
    settings, state, app, data_dir = _build_env(test_db_url, tmp_path, monkeypatch, token=None)
    with TestClient(app) as client:
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield {"client": client, "state": state, "settings": settings, "data_dir": data_dir}
    state.engine.dispose()
    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()


@pytest.fixture()
def token_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Auth-enabled environment: the token must be presented on every read."""
    settings, state, app, data_dir = _build_env(test_db_url, tmp_path, monkeypatch, token=APP_TOKEN)
    with TestClient(app) as client:
        client.data_dir = data_dir  # type: ignore[attr-defined]
        client.app_token = APP_TOKEN  # type: ignore[attr-defined]
        yield {"client": client, "state": state, "settings": settings, "data_dir": data_dir}
    state.engine.dispose()
    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()


@pytest.fixture()
def bearer(token_env):
    """Bearer-authenticated headers (automation path)."""
    return {"Authorization": f"Bearer {APP_TOKEN}"}


@pytest.fixture()
def browser(token_env):
    """A real browser session: cookie + CSRF token, no bearer header.

    Returns a dict with the client, the CSRF headers for writes, and the raw
    session payload, so a test can assert on the actual cookie/session contract.
    """
    from paperintel.services.ui.session import SESSION_COOKIE

    client = token_env["client"]
    response = client.post("/v1/ui/session", json={"token": APP_TOKEN})
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    csrf = payload["csrf_token"]
    cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie, "session cookie was not set by POST /v1/ui/session"
    return {
        "client": client,
        "csrf_headers": {"X-CSRF-Token": csrf},
        "session": payload,
        "cookie": cookie,
        "cookie_header": response.headers.get("set-cookie", ""),
        "state": token_env["state"],
        "settings": token_env["settings"],
        "data_dir": token_env["data_dir"],
    }


@pytest.fixture()
def import_pdf_into(api_env):
    """Import a generated PDF through the real ingest service."""

    def _import(
        builder=build_f01_native,
        name: str = "f01.pdf",
        *,
        persist_evidence: bool = True,
        title: str | None = None,
    ):
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
                    title=title,
                )
            )
            session.commit()
            return result
        finally:
            session.close()

    return _import


@pytest.fixture()
def scoped_library(api_env, import_pdf_into):
    """Import documents into a PRIVATE collection so counts are test-local.

    The integration database is module-scoped (see tests/integration/conftest),
    so a test may not assert on global totals: it scopes its library query by a
    collection it created itself and then sees exactly its own papers.
    """
    from paperintel.knowledge.collections import add_paper, create_collection

    def _build(*, documents: int = 2, name: str = "ui-contract-scope", builders=None):
        session = api_env["state"].session_factory()
        try:
            collection = create_collection(session, name=name)
            session.commit()
            collection_id = collection.collection_id
        finally:
            session.close()
        builders = list(builders or [build_f01_native, build_f03_mixed, build_f04_rich])
        imported = []
        for index in range(documents):
            imported.append(
                import_pdf_into(builder=builders[index % len(builders)], name=f"scope-{index}.pdf")
            )
        session = api_env["state"].session_factory()
        try:
            for result in imported:
                add_paper(session, collection_id=collection_id, paper_id=result.paper_id)
            session.commit()
        finally:
            session.close()
        return {"collection_id": collection_id, "imported": imported}

    return _build


@pytest.fixture()
def fresh_paper(import_pdf_into):
    """A paper whose content is UNIQUE to this call.

    The integration database is module-scoped and import deduplicates by content
    fingerprint, so a deterministic fixture would hand two tests the SAME paper
    and let one test's personal state leak into the next.
    """

    def _make(name: str | None = None, builder=build_f03_mixed, *, own_paper: bool = True):
        token = uuid.uuid4().hex[:8]
        # The import path attaches a new VERSION to an existing paper when the
        # normalized title matches, so a test that needs its own personal state
        # must also ask for its own title.
        return import_pdf_into(
            builder=builder,
            name=name or f"fresh-{token}.pdf",
            title=f"UI contract paper {token}" if own_paper else None,
        )

    return _make


@pytest.fixture()
def add_claim(api_env):
    """Insert a real claim row for a version (used to prove UI writes never
    mutate scientific state)."""

    def _add(version_id: str, statement: str = "UI writes must not touch claims."):
        from tests.integration.p08.fixtures import ensure_run

        from paperintel.database.models import ClaimRow, PaperVersionRow
        from paperintel.ids import new_claim_id
        from paperintel.schemas.enums import ClaimType, SupportState

        session = api_env["state"].session_factory()
        try:
            version = session.get(PaperVersionRow, version_id)
            assert version is not None
            run_id = ensure_run(session, version_id)
            claim = ClaimRow(
                claim_id=new_claim_id(),
                paper_id=version.paper_id,
                paper_version_id=version_id,
                claim_type=ClaimType.FACT,
                category="result.main",
                statement=statement,
                support_state=SupportState.SUPPORTED,
                created_by_run_id=run_id,
                pipeline_version="1.0.0",
            )
            session.add(claim)
            session.commit()
            return claim.claim_id
        finally:
            session.close()

    return _add


@pytest.fixture()
def import_pdf_token_env(token_env):
    """Same as ``import_pdf_into`` but bound to the auth-enabled environment."""

    def _import(builder=build_f01_native, name: str = "f01.pdf", *, persist_evidence: bool = True):
        session = token_env["state"].session_factory()
        try:
            path = token_env["data_dir"] / name
            path.write_bytes(builder())
            store = LocalObjectStore(Path(token_env["settings"].core.data_dir) / "objects")
            result = asyncio.run(
                import_pdf(
                    path,
                    session=session,
                    store=store,
                    data_dir=Path(token_env["settings"].core.data_dir),
                    persist_evidence=persist_evidence,
                )
            )
            session.commit()
            return result
        finally:
            session.close()

    return _import


__all__ = [
    "APP_TOKEN",
    "REPO_ROOT",
    "add_claim",
    "api_env",
    "bearer",
    "browser",
    "build_f01_native",
    "build_f03_mixed",
    "build_f04_rich",
    "fresh_paper",
    "import_pdf_into",
    "import_pdf_token_env",
    "scoped_library",
    "token_env",
]

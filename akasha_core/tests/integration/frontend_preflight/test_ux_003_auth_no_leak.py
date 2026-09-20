"""UX-003 — unauthenticated access to the legacy UI and /metrics must not
leak paper data (spec docs/01 B02, task card F00).

The legacy pages render real database content, so they are private reads:
without the configured bearer token they must fail closed (401) and the body
must contain no paper title, no paper id and no evidence text. /metrics is
likewise gated. The public root page stays a link-only shell.
"""

from __future__ import annotations

import pytest

from paperintel.api.app import ApiState, create_app


@pytest.fixture()
def secure_env(test_db_url, tmp_path, monkeypatch):
    """The same app with an API token configured (auth enforced)."""
    import asyncio

    from fastapi.testclient import TestClient
    from tests.fixtures.generators import build_f01_native
    from tests.integration.frontend_preflight.conftest import REPO_ROOT

    from paperintel.config.settings import get_settings, reset_settings_cache
    from paperintel.ingest.service import import_pdf
    from paperintel.storage.object_store import LocalObjectStore

    data_dir = tmp_path / "secure-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE", str(REPO_ROOT / "config" / "providers.mock.yaml")
    )
    monkeypatch.setenv("API_TOKEN", "preflight-secret-token")
    reset_settings_cache()
    settings = get_settings()
    state = ApiState(settings)

    # Seed one real paper so a leak would be observable.
    session = state.session_factory()
    try:
        pdf = data_dir / "f01.pdf"
        pdf.write_bytes(build_f01_native())
        imported = asyncio.run(
            import_pdf(
                pdf,
                session=session,
                store=LocalObjectStore(data_dir / "objects"),
                data_dir=data_dir,
            )
        )
        session.commit()
    finally:
        session.close()

    app = create_app(settings, state=state)
    with TestClient(app) as client:
        yield {
            "client": client,
            "imported": imported,
            "token": "preflight-secret-token",
            "state": state,
        }
    state.engine.dispose()
    reset_settings_cache()


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-003"], ids=["UX-003"])
def test_ux_003_legacy_ui_and_metrics_require_auth(requirement: str, secure_env) -> None:
    assert requirement == "UX-003", "test/requirement mapping drift"
    client = secure_env["client"]
    imported = secure_env["imported"]
    title = "Native Extraction: A Clean Digital Paper"

    protected = [
        "/ui",
        f"/ui/papers/{imported.paper_id}",
        "/metrics",
        "/v1/system/status",
        f"/v1/papers/{imported.paper_id}/context",
    ]
    for path in protected:
        response = client.get(path)
        assert response.status_code == 401, f"{path} served data without a token"
        body = response.text
        assert title not in body, f"{path} leaked a paper title while unauthenticated"
        assert imported.paper_id not in body, f"{path} leaked a paper id"
        assert "ev_" not in body and "clm_" not in body, f"{path} leaked identifiers"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-003"], ids=["UX-003"])
def test_ux_003_authenticated_reads_still_work(requirement: str, secure_env) -> None:
    assert requirement == "UX-003", "test/requirement mapping drift"
    client = secure_env["client"]
    imported = secure_env["imported"]
    headers = {"Authorization": f"Bearer {secure_env['token']}"}

    ui = client.get("/ui", headers=headers)
    assert ui.status_code == 200
    assert "PaperIntel" in ui.text

    paper_ui = client.get(f"/ui/papers/{imported.paper_id}", headers=headers)
    assert paper_ui.status_code == 200

    metrics = client.get("/metrics", headers=headers)
    assert metrics.status_code == 200
    assert "paperintel_" in metrics.text

    context = client.get(f"/v1/papers/{imported.paper_id}/context", headers=headers)
    assert context.status_code == 200
    assert context.json()["paper"]["paper_id"] == imported.paper_id


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-003"], ids=["UX-003"])
def test_ux_003_wrong_token_is_rejected(requirement: str, secure_env) -> None:
    assert requirement == "UX-003", "test/requirement mapping drift"
    client = secure_env["client"]
    for headers in (
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic abc"},
        {},
    ):
        response = client.get("/ui", headers=headers)
        assert response.status_code == 401


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-003"], ids=["UX-003"])
def test_ux_003_public_root_is_a_link_shell_only(requirement: str, secure_env) -> None:
    assert requirement == "UX-003", "test/requirement mapping drift"
    """The unauthenticated entry point may link to surfaces but must not
    embed private data (docs/01 B02: 跳转/登录入口)."""
    response = secure_env["client"].get("/")
    assert response.status_code == 200
    body = response.text
    assert "/ui" in body  # a link is fine
    assert "Native Extraction" not in body
    assert secure_env["imported"].paper_id not in body

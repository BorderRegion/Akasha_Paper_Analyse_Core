"""Deployment contract: the SPA deep links work and /v1 stays JSON (UX-059).

A single-origin deployment must serve the built workbench at /app and answer a
deep link with index.html, while an unknown API path remains a JSON 404 — if the
SPA fallback swallowed /v1, a client bug would look like an HTML page instead of
a real error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _build_dist(root: Path) -> Path:
    dist = root / "web" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id=root>PaperIntel 静研</div></body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "index-abc.js").write_text("console.log('app')", encoding="utf-8")
    (dist / "favicon.ico").write_bytes(b"\x00\x01")
    return dist


@pytest.mark.needs_db
def test_spa_deep_links_and_api_404(tmp_path, monkeypatch, test_db_url) -> None:
    dist = _build_dist(tmp_path)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERINTEL_WEB_DIST", str(dist))
    from paperintel.config.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    from paperintel.api.app import ApiState, create_app

    state = ApiState(get_settings())
    app = create_app(get_settings(), state=state)
    with TestClient(app) as client:
        # 1. The shell is served at /app.
        shell = client.get("/app")
        assert shell.status_code == 200
        assert "PaperIntel" in shell.text
        assert shell.headers["content-type"].startswith("text/html")

        # 2. A DEEP LINK returns the same shell (refresh on /app/papers/pap_x).
        for path in ("/app/library", "/app/papers/pap_01HZZ", "/app/operations"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "id=root" in response.text, f"{path} did not fall back to the SPA shell"

        # 3. Real files are served as themselves.
        asset = client.get("/app/assets/index-abc.js")
        assert asset.status_code == 200
        assert "console.log" in asset.text
        icon = client.get("/app/favicon.ico")
        assert icon.status_code == 200
        assert icon.content == b"\x00\x01"

        # 4. /v1 NEVER falls back to HTML.
        missing = client.get("/v1/does-not-exist")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/json")
        payload = json.loads(missing.text)
        assert "error" in payload or "detail" in payload

        # 5. Path traversal inside /app cannot escape the dist directory.
        escape = client.get("/app/../../etc/passwd")
        assert escape.status_code in {200, 404}
        assert "root:" not in escape.text

        # 6. Health endpoints stay JSON while the SPA is mounted.
        status = client.get("/v1/system/status")
        assert status.status_code == 200
        assert status.headers["content-type"].startswith("application/json")
    state.engine.dispose()
    reset_settings_cache()


@pytest.mark.needs_db
def test_spa_absence_is_reported_not_faked(tmp_path, monkeypatch, test_db_url) -> None:
    """An API-only deployment says the workbench is not deployed."""
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERINTEL_WEB_DIST", str(tmp_path / "missing-dist"))
    from paperintel.config.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    from paperintel.api.app import ApiState, create_app

    state = ApiState(get_settings())
    app = create_app(get_settings(), state=state)
    with TestClient(app) as client:
        response = client.get("/app")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "STORAGE_003"
        assert "not deployed" in response.json()["error"]["message"]
    state.engine.dispose()
    reset_settings_cache()

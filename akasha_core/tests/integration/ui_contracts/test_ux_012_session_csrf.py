"""UX-012 — bootstrap / session / CSRF, and what logout must clear.

Requirement (frontend spec docs/06 §会话与能力):
- GET  /v1/ui/bootstrap   : minimal while unauthenticated — no papers, no
                            paths, no provider detail;
- POST /v1/ui/session     : constant-time token check, rate limited, generic
                            failure, HttpOnly/SameSite=Strict cookie, CSRF
                            token returned in memory only;
- DELETE /v1/ui/session   : revoke the session so a stale cookie cannot read
                            private data back, and tell the client to clear its
                            private caches;
- cookie WRITES must present CSRF; a Bearer request does not substitute for
  CSRF on cookie requests, but is itself a credential.
"""

from __future__ import annotations

import pytest

from paperintel.services.ui.session import (
    CSRF_HEADER,
    SESSION_ATTEMPT_LIMIT,
    SESSION_COOKIE,
)

#: Strings that must never appear in the unauthenticated bootstrap payload.
_FORBIDDEN_IN_BOOTSTRAP = (
    "data_dir",
    "/home/",
    "/tmp/",
    "paper_id",
    "pap_",
    "provider",
    "api_key",
    "Bearer",
)


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_bootstrap_is_minimal_and_needs_no_session(requirement: str, token_env) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    client = token_env["client"]

    response = client.get("/v1/ui/bootstrap")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert set(payload["data"]) == {
        "app_version",
        "ui_contract_version",
        "auth_required",
        "mode",
    }, "bootstrap must expose exactly the minimum login metadata"
    assert payload["data"]["auth_required"] is True
    assert payload["data"]["ui_contract_version"] == "1.0.0"
    assert payload["meta"]["contract_version"] == "1.0.0"

    body = response.text
    leaked = [needle for needle in _FORBIDDEN_IN_BOOTSTRAP if needle in body]
    assert not leaked, f"bootstrap leaked environment detail: {leaked}"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_bootstrap_reports_auth_disabled_locally(requirement: str, api_env) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    """Auth disabled is a DECLARED mode, not a silent one."""
    payload = api_env["client"].get("/v1/ui/bootstrap").json()
    assert payload["data"]["auth_required"] is False
    # An unauthenticated local deployment still serves the workbench surface.
    assert api_env["client"].get("/v1/ui/capabilities").status_code == 200


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_session_rejects_a_wrong_token_without_detail(requirement: str, token_env) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    client = token_env["client"]

    wrong = client.post("/v1/ui/session", json={"token": "not-the-token"})
    missing = client.post("/v1/ui/session", json={})
    assert wrong.status_code == 401, wrong.text
    assert missing.status_code == 401
    assert wrong.json()["error"]["code"] == "AUTH_001"
    # Indistinguishable failures: no hint about which part was wrong, and the
    # presented token is never echoed.
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]
    assert "not-the-token" not in wrong.text
    assert SESSION_COOKIE not in wrong.headers.get("set-cookie", "")


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_session_cookie_is_hardened_and_csrf_is_returned_in_memory(
    requirement: str, token_env
) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    response = token_env["client"].post(
        "/v1/ui/session", json={"token": token_env["client"].app_token}
    )
    assert response.status_code == 200, response.text
    headers = response.headers.get("set-cookie", "")
    assert "HttpOnly" in headers, "the session cookie must not be readable from JS"
    assert "samesite=strict" in headers.lower()
    assert "path=/" in headers.lower()

    data = response.json()["data"]
    assert data["authenticated"] is True
    assert isinstance(data["csrf_token"], str) and len(data["csrf_token"]) >= 16
    # The session id itself stays inside the cookie: never in the body.
    cookie_value = token_env["client"].cookies.get(SESSION_COOKIE)
    assert cookie_value and cookie_value not in response.text


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_cookie_writes_require_csrf(requirement: str, browser) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    client = browser["client"]

    assert client.get("/v1/ui/preferences").status_code == 200

    missing = client.patch("/v1/ui/preferences", json={"theme": "DARK"})
    assert missing.status_code == 403, missing.text
    assert missing.json()["error"]["code"] == "AUTH_001"

    wrong = client.patch(
        "/v1/ui/preferences", json={"theme": "DARK"}, headers={CSRF_HEADER: "nope"}
    )
    assert wrong.status_code == 403

    ok = client.patch("/v1/ui/preferences", json={"theme": "DARK"}, headers=browser["csrf_headers"])
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["theme"] == "DARK"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_bearer_is_a_credential_and_never_replaces_csrf(
    requirement: str, token_env, bearer
) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    """A bearer request needs no CSRF (the token IS the credential); a COOKIE
    request still needs it — the bearer path must not weaken that."""
    client = token_env["client"]
    ok = client.patch("/v1/ui/preferences", json={"density": "COMPACT"}, headers=bearer)
    assert ok.status_code == 200, ok.text

    invalid = client.patch(
        "/v1/ui/preferences",
        json={"density": "COMPACT"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert invalid.status_code == 401

    client.post("/v1/ui/session", json={"token": token_env["client"].app_token})
    with_cookie = client.patch(
        "/v1/ui/preferences",
        json={"density": "COMFORTABLE"},
        headers={"X-CSRF-Token": "still-wrong"},
    )
    assert with_cookie.status_code == 403, "cookie writes must keep requiring CSRF"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_logout_revokes_the_session_and_tells_the_client_to_clear_caches(
    requirement: str, browser
) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    client = browser["client"]
    assert client.get("/v1/ui/capabilities").status_code == 200

    assert client.delete("/v1/ui/session").status_code == 403
    revoked = client.delete("/v1/ui/session", headers=browser["csrf_headers"])
    assert revoked.status_code == 200, revoked.text
    data = revoked.json()["data"]
    assert data["authenticated"] is False
    assert data["revoked"] is True
    assert data["clear_client_caches"] is True, (
        "logout must instruct the client to drop private caches"
    )

    stale = client.get("/v1/ui/capabilities")
    assert stale.status_code == 401, "a revoked cookie must not be able to read private data back"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-012"], ids=["UX-012"])
def test_repeated_authentication_failures_are_rate_limited(requirement: str, token_env) -> None:
    assert requirement == "UX-012", "test/requirement mapping drift"
    client = token_env["client"]
    statuses = [
        client.post("/v1/ui/session", json={"token": "wrong"}).status_code
        for _ in range(SESSION_ATTEMPT_LIMIT + 1)
    ]
    assert statuses[:SESSION_ATTEMPT_LIMIT] == [401] * SESSION_ATTEMPT_LIMIT
    last = client.post("/v1/ui/session", json={"token": "wrong"})
    assert last.status_code == 429, last.text
    error = last.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["retryable"] is True
    assert "window_seconds" in error["details"]

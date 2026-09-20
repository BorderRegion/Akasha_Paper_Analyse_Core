"""api.ui_routes.deps — shared dependencies for the /v1/ui surface.

Authentication model (frontend spec docs/06 §会话与能力):
- automation may keep using `Authorization: Bearer <app token>`;
- a browser signs in once (POST /v1/ui/session) and then sends the HttpOnly
  session cookie; WRITES with a cookie session must also present the CSRF
  header. A Bearer request is not required to carry CSRF (the token itself is
  the credential), but it never substitutes for CSRF on cookie requests.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from paperintel.services.ui.session import (
    CSRF_HEADER,
    SESSION_COOKIE,
    verify_app_token,
)


class UiIdentity:
    """Who is calling: an automation token or a cookie session."""

    def __init__(self, *, kind: str, owner_key: str) -> None:
        self.kind = kind
        self.owner_key = owner_key

    @property
    def is_session(self) -> bool:
        return self.kind == "session"


def _state(request: Request):
    return request.app.state.paperintel


def ui_identity(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UiIdentity:
    """Resolve the caller, or raise 401 with the frozen code."""
    settings = _state(request).settings
    token_required = bool(settings.api.token)

    # 1. Automation Bearer token.
    presented = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization.split(" ", 1)[1].strip()
    if presented is not None:
        if verify_app_token(presented, settings.api.token):
            return UiIdentity(kind="token", owner_key="local")
        raise HTTPException(status_code=401, detail="invalid bearer token")

    # 2. Browser session cookie.
    session_id = request.cookies.get(SESSION_COOKIE)
    store = _state(request).sessions
    session = store.get(session_id)
    if session is not None:
        return UiIdentity(kind="session", owner_key=session.owner_key)

    if not token_required:
        # Auth disabled by configuration is reported by bootstrap, never silent:
        # the request is still served as the local owner.
        return UiIdentity(kind="anonymous-local", owner_key="local")
    raise HTTPException(status_code=401, detail="authentication required")


def require_csrf(
    request: Request,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> UiIdentity:
    """Write guard: cookie sessions must present the CSRF token."""
    if identity.is_session:
        store = _state(request).sessions
        session = store.get(request.cookies.get(SESSION_COOKIE))
        if session is None or not csrf_token or csrf_token != session.csrf_token:
            raise HTTPException(status_code=403, detail="missing or invalid CSRF token")
    return identity


def envelope(data: Any, *, warnings: list[dict] | None = None, partial: bool = False) -> dict:
    """Wrap a payload in the /v1/ui envelope with a fresh snapshot id."""
    from paperintel.schemas.common import utcnow

    observed = utcnow()
    return {
        "data": data,
        "meta": {
            "contract_version": "1.0.0",
            "snapshot_id": f"snap_{int(observed.timestamp() * 1000)}",
            "observed_at": observed.isoformat(),
            "partial": partial,
            "warnings": warnings or [],
        },
    }


def page_envelope(page, items: list[Any], *, kind: str | None = None) -> dict:
    """The pagination payload (docs/06 §通用响应).

    ``kind`` is the result discriminator required by contracts/endpoints.json:
    a CLAIM hit must be distinguishable from a PAPER card without guessing from
    the item fields.
    """
    data: dict[str, Any] = {
        "items": items,
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
        "total": page.total,
        "total_kind": page.total_kind,
        "scope_revision": page.scope_revision,
    }
    if kind is not None:
        data["kind"] = kind
    return envelope(data)


def data_dir(request: Request):
    from pathlib import Path

    return Path(_state(request).settings.core.data_dir)


def settings_of(request: Request):
    return _state(request).settings


def session_of(request: Request) -> Session:
    """A session bound to this request (the caller closes it)."""
    return _state(request).session_factory()


__all__ = [
    "UiIdentity",
    "data_dir",
    "envelope",
    "page_envelope",
    "require_csrf",
    "session_of",
    "settings_of",
    "ui_identity",
]

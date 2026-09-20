"""api.ui_routes.session — bootstrap, session, capabilities, preferences."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response

from paperintel.api.ui_routes.deps import (
    UiIdentity,
    envelope,
    require_csrf,
    settings_of,
    ui_identity,
)
from paperintel.schemas.ui.session import (
    Capability,
    SessionInfo,
)
from paperintel.services.ui import personal
from paperintel.services.ui.session import (
    SESSION_COOKIE,
    bootstrap_payload,
    cookie_flags,
)

router = APIRouter(prefix="/v1/ui", tags=["ui"])

#: Capability codes the workbench asks about. Availability must reflect what the
#: server actually registered — never a frontend constant (docs/06).
CAPABILITY_CODES = (
    "library.query",
    "library.pagination",
    "paper.workspace",
    "reader.document",
    "reader.locators",
    "import.upload",
    "personal.state",
    "notes",
    "review.decisions",
    "review.queue",
    "operations.snapshot",
    "compare",
    "entities",
    "collections",
    "handoff.export",
    "diagnostics",
)


def _capabilities(settings) -> dict[str, Any]:
    """GET /v1/ui/capabilities — exactly the schema in contracts/ui.schema.json.

    Availability reflects what the server ACTUALLY registered; the frontend must
    never hard-code it. Paths and token material are never part of this payload.
    """
    from paperintel.services.ui.imports import UploadLimits

    limits = UploadLimits.from_settings(settings)
    providers_configured = settings.providers_file is not None
    entries: list[Capability] = []
    for code in CAPABILITY_CODES:
        availability = "AVAILABLE"
        reason = None
        if code == "handoff.export" and not providers_configured:
            availability = "DEGRADED"
            reason = "export works; analysis depth depends on the configured providers"
        entries.append(Capability(code=code, availability=availability, reason=reason))
    return {
        "ui_contract_version": "1.0.0",
        "capabilities": [entry.model_dump() for entry in entries],
        "limits": limits.as_dict(),
        "mode": "TEST" if settings.providers_file is None else "LIVE",
    }


@router.get("/bootstrap", response_model=None)
def bootstrap(request: Request) -> dict:
    """Public, minimal: no papers, no paths, no provider detail."""
    settings = settings_of(request)
    payload = bootstrap_payload(settings, auth_required=bool(settings.api.token))
    # data_dir is deliberately NOT part of the public bootstrap payload.
    return envelope(payload)


@router.post("/session", response_model=None)
def create_session(
    request: Request,
    response: Response,
    payload: dict | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """Exchange the configured application token for a browser session.

    The token may be presented as ``Authorization: Bearer …`` (automation and
    curl) or as ``{"token": "…"}`` in the body (the browser form). Comparison is
    constant time, failures are indistinguishable and never echo the token, and
    repeated attempts are rate limited. The session id lives in an HttpOnly
    cookie; the CSRF token is returned in the body for the client to keep in
    memory only.
    """
    from datetime import UTC, datetime

    from paperintel.services.ui.session import token_from_authorization, verify_app_token

    settings = settings_of(request)
    state = request.app.state.paperintel
    client = request.client.host if request.client else "unknown"
    state.sessions.check_rate_limit(client)

    presented = token_from_authorization(authorization)
    if presented is None and isinstance(payload, dict):
        candidate = payload.get("token")
        presented = candidate if isinstance(candidate, str) else None
    if not verify_app_token(presented, settings.api.token):
        # One generic 401: never reveal whether a token exists or which part
        # of the request was wrong.
        raise HTTPException(status_code=401, detail="Authentication failed.")

    session = state.sessions.create()
    response.set_cookie(SESSION_COOKIE, session.session_id, **cookie_flags(settings))
    return envelope(
        SessionInfo(
            authenticated=True,
            csrf_token=session.csrf_token,
            expires_at=datetime.fromtimestamp(session.expires_at, tz=UTC),
        ).model_dump(mode="json")
    )


@router.get("/session", response_model=None)
def current_session(
    request: Request,
    response: Response,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    """Recover the in-memory CSRF token after a same-origin page reload."""
    from datetime import UTC, datetime

    session = request.app.state.paperintel.sessions.get(request.cookies.get(SESSION_COOKIE))
    response.headers["Cache-Control"] = "no-store"
    return envelope(SessionInfo(
        authenticated=True,
        csrf_token=session.csrf_token if session else None,
        expires_at=datetime.fromtimestamp(session.expires_at, tz=UTC) if session else None,
    ).model_dump(mode="json"))


@router.delete("/session", response_model=None)
def delete_session(
    request: Request, response: Response,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    """Revoke the session and clear the cookie.

    The response tells the client to drop its private caches: everything the
    browser held for this session (workspace payloads, reader state, notes
    drafts) must be discarded, and the revoked cookie can no longer read them
    back (a stale cookie is rejected by ui_identity).
    """
    state = request.app.state.paperintel
    store = state.sessions
    revoked = store.revoke(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return envelope(
        {
            "authenticated": False,
            "revoked": revoked,
            "csrf_token": None,
            "clear_client_caches": True,
        }
    )


@router.get("/capabilities", response_model=None)
def capabilities(
    request: Request,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    return envelope(_capabilities(settings_of(request)))


@router.get("/preferences", response_model=None)
def get_preferences(
    request: Request,
    identity: Annotated[UiIdentity, Depends(ui_identity)],
) -> dict:
    session = request.app.state.paperintel.session_factory()
    try:
        row = personal.get_preferences(session, owner_key=identity.owner_key)
        session.commit()
        return envelope(
            {
                "theme": row.theme,
                "density": row.density,
                "reduce_motion": row.reduce_motion,
                "single_key_shortcuts": row.single_key_shortcuts,
                "reader_font_px": row.reader_font_px,
                "focus_default": row.focus_default,
                "revision": row.revision,
            }
        )
    finally:
        session.close()


@router.patch("/preferences", response_model=None)
def patch_preferences(
    request: Request,
    payload: dict,
    identity: Annotated[UiIdentity, Depends(require_csrf)],
) -> dict:
    session = request.app.state.paperintel.session_factory()
    try:
        allowed = {
            "theme",
            "density",
            "reduce_motion",
            "single_key_shortcuts",
            "reader_font_px",
            "focus_default",
            "expected_revision",
        }
        unknown = set(payload) - allowed
        if unknown:
            from paperintel.errors import DomainError

            raise DomainError(
                "CFG_002",
                message=f"Unknown preference keys: {sorted(unknown)}",
                details={"unknown": sorted(unknown)},
            )
        row = personal.patch_preferences(session, owner_key=identity.owner_key, **payload)
        session.commit()
        return envelope(
            {
                "theme": row.theme,
                "density": row.density,
                "reduce_motion": row.reduce_motion,
                "single_key_shortcuts": row.single_key_shortcuts,
                "reader_font_px": row.reader_font_px,
                "focus_default": row.focus_default,
                "revision": row.revision,
            }
        )
    finally:
        session.close()

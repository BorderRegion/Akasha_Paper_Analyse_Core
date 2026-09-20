"""services.ui.session — bootstrap, app-token sessions and CSRF (docs/06).

Design notes:
- the APPLICATION token (api.token) and LLM provider keys are different things;
- the token is compared in CONSTANT TIME and never logged;
- the session cookie is HttpOnly + SameSite=Strict, Secure unless the server is
  explicitly bound to loopback in local HTTP mode;
- writes with a cookie session must present the CSRF token; an automation
  Bearer token does not replace CSRF for cookie requests.
"""

from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

from paperintel.config.settings import AppConfig
from paperintel.errors.ui_errors import UiError
from paperintel.version import SPEC_VERSION

SESSION_COOKIE = "paperintel_session"
CSRF_HEADER = "X-CSRF-Token"
#: Single-user local deployment: the session table is in-process, short-lived.
SESSION_TTL_SECONDS = 12 * 3600
#: Rate limit for session creation attempts (per client, per window).
SESSION_ATTEMPT_LIMIT = 10
SESSION_ATTEMPT_WINDOW_SECONDS = 60


@dataclass(slots=True)
class UiSession:
    session_id: str
    csrf_token: str
    expires_at: float
    owner_key: str = "local"


@dataclass(slots=True)
class _Attempts:
    hits: list[float] = field(default_factory=list)


class SessionStore:
    """In-process session store for the local single-user deployment.

    Documented limitation: this is NOT a distributed session store; a
    multi-host deployment would move it to Redis. It never holds credentials —
    only an opaque session id, a CSRF token and an expiry.
    """

    def __init__(self, clock=time.monotonic) -> None:
        self._sessions: dict[str, UiSession] = {}
        self._attempts: dict[str, _Attempts] = {}
        self._clock = clock

    # -- rate limiting ------------------------------------------------------
    def check_rate_limit(self, client: str) -> None:
        now = self._clock()
        record = self._attempts.setdefault(client, _Attempts())
        record.hits = [hit for hit in record.hits if now - hit < SESSION_ATTEMPT_WINDOW_SECONDS]
        if len(record.hits) >= SESSION_ATTEMPT_LIMIT:
            raise UiError(
                "RATE_LIMITED",
                message="Too many authentication attempts; wait before retrying.",
                details={"window_seconds": SESSION_ATTEMPT_WINDOW_SECONDS},
            )
        record.hits.append(now)

    # -- lifecycle ----------------------------------------------------------
    def create(self, *, owner_key: str = "local") -> UiSession:
        session = UiSession(
            session_id=secrets.token_urlsafe(32),
            csrf_token=secrets.token_urlsafe(32),
            expires_at=self._clock() + SESSION_TTL_SECONDS,
            owner_key=owner_key,
        )
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str | None) -> UiSession | None:
        if not session_id:
            return None
        session = self._sessions.get(session_id)
        if session is None:
            return None
        if session.expires_at <= self._clock():
            self._sessions.pop(session.session_id, None)
            return None
        return session

    def revoke(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        return self._sessions.pop(session_id, None) is not None

    def count(self) -> int:
        return len(self._sessions)


def verify_app_token(presented: str | None, expected: str | None) -> bool:
    """Constant-time comparison of the application token."""
    if not expected:
        return True  # auth disabled by configuration (reported, never silent)
    if not presented:
        return False
    return hmac.compare_digest(presented.encode(), expected.encode())


def token_from_authorization(header: str | None) -> str | None:
    if not header or not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def cookie_flags(settings: AppConfig) -> dict[str, object]:
    """Cookie attributes for the session.

    Secure is on unless this is an explicitly loopback-bound local HTTP
    deployment; a non-loopback bind without auth is refused at startup
    (checked by app wiring, not here).
    """
    loopback = settings.api.host in {"127.0.0.1", "localhost", "::1"}
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": not loopback,
        "max_age": SESSION_TTL_SECONDS,
        "path": "/",
    }


def bootstrap_payload(settings: AppConfig, *, auth_required: bool) -> dict[str, object]:
    """The minimal unauthenticated payload: no papers, paths or providers."""
    return {
        "app_version": SPEC_VERSION,
        "ui_contract_version": "1.0.0",
        "auth_required": auth_required,
        "mode": "TEST" if settings.providers_file is None else "LIVE",
    }


__all__ = [
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "SESSION_TTL_SECONDS",
    "SessionStore",
    "UiSession",
    "bootstrap_payload",
    "cookie_flags",
    "token_from_authorization",
    "verify_app_token",
]

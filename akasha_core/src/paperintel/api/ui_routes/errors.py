"""api.ui_routes.errors — the /v1/ui error + trace contract (docs/06 §通用响应).

Frozen rules implemented here:

- every /v1/ui response carries ``X-Trace-ID``; the client may send
  ``X-Client-Request-ID`` and it is echoed back so a UI action can be followed
  through the logs;
- errors use ONE shape: ``{"error": {code, message, retryable, trace_id, details}}``
  — DomainError, UiError, HTTPException (401/403) and FastAPI's own 422 are all
  adapted into it, so the client never has to branch on "which layer failed";
- the HTTP status is meaningful: 409 for cursor/revision conflicts, 403 for
  scope refusals and CSRF, 404 for unknown entities, 507 for storage, 400 for a
  rejected request, 422 for schema validation, 500 for an unexpected failure.

The /v1 REST surface is untouched: its envelope (including ``severity``) is the
frozen automation contract.
"""

from __future__ import annotations

import secrets

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from paperintel.api.app import error_response
from paperintel.errors import DomainError
from paperintel.errors.ui_errors import UiError
from paperintel.operations.debug import redact

UI_PREFIX = "/v1/ui"
TRACE_HEADER = "X-Trace-ID"
CLIENT_REQUEST_HEADER = "X-Client-Request-ID"

#: DomainError code → HTTP status on the UI surface.
_DOMAIN_STATUS: dict[str, int] = {
    "INTERNAL_002": 409,  # invalid state transition: the state changed underneath
    "EVIDENCE_001": 404,
    "STORAGE_003": 404,
    "STORAGE_002": 409,
    "RESOURCE_001": 507,
    "RESOURCE_002": 507,
    "DB_002": 503,
    "REDIS_001": 503,
    "QUEUE_001": 503,
}

_ENTITY_KEYS = (
    "paper_id",
    "paper_version_id",
    "claim_id",
    "evidence_id",
    "job_id",
    "task_id",
    "collection_id",
    "note_id",
    "operation_id",
    "asset_id",
    "batch_id",
    "item_id",
)


def new_trace_id() -> str:
    """A trace id in the frozen ``trc_`` namespace (no ULID ordering needed)."""
    return f"trc_{secrets.token_hex(12)}"


def request_trace_id(request: Request) -> str:
    existing = getattr(request.state, "trace_id", None)
    if isinstance(existing, str) and existing:
        return existing
    trace_id = new_trace_id()
    request.state.trace_id = trace_id
    return trace_id


#: Codes that mean "the thing you named does not exist" when an id is present.
_UNKNOWN_ENTITY_CODES = frozenset({"CFG_002", "STORAGE_003", "EVIDENCE_001", "GRAPH_001"})


def _status_for(exc: DomainError) -> int:
    if exc.code.startswith("RESOURCE"):
        return 507
    if exc.code in _DOMAIN_STATUS:
        return _DOMAIN_STATUS[exc.code]
    details = exc.details or {}
    if exc.code == "CFG_002" and "state" in details:
        # "this item is already IMPORTED" is a STATE conflict, not a missing
        # resource: the client must show it as a conflict and refresh.
        return 409
    if exc.code == "STORAGE_001" and ({"limit", "max_bytes"} & set(details)):
        # The payload is larger than the limit the caller set or the server
        # advertises (upload size, handoff bundle size).
        return 413
    if (
        exc.code in _UNKNOWN_ENTITY_CODES
        and len(details) == 1
        and set(details) <= set(_ENTITY_KEYS)
    ):
        # EXACTLY one identifier was named and nothing matched: 404. An
        # inconsistent PAIR (a version that belongs to another paper) or an
        # extra field (a limit) is a rejected request and stays 400.
        return 404
    # PDF_001/PDF_002, a rejected payload, an unsupported operation kind … are
    # a bad REQUEST (the client must fix it), never a missing resource.
    return 400


def _envelope(
    *,
    code: str,
    message: str,
    retryable: bool,
    trace_id: str,
    details: dict | None,
) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "trace_id": trace_id,
            "details": details or {},
        }
    }


def register(app: FastAPI) -> None:
    """Install the /v1/ui error handlers and the trace-id middleware."""

    previous_domain_handler = app.exception_handlers.get(DomainError)

    @app.exception_handler(UiError)
    async def _ui_error(request: Request, exc: UiError) -> JSONResponse:
        trace_id = request_trace_id(request)
        exc.trace_id = exc.trace_id or trace_id
        return JSONResponse(redact(exc.to_envelope(trace_id=trace_id)), status_code=exc.http_status)

    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        if not request.url.path.startswith(UI_PREFIX):
            if previous_domain_handler is not None:
                return await previous_domain_handler(request, exc)
            return error_response(exc)
        trace_id = request_trace_id(request)
        exc.trace_id = exc.trace_id or trace_id
        payload = _envelope(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            trace_id=trace_id,
            details=exc.details,
        )
        return JSONResponse(redact(payload), status_code=_status_for(exc))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if not request.url.path.startswith(UI_PREFIX):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        trace_id = request_trace_id(request)
        code = {
            401: "AUTH_001",
            403: "AUTH_001",
            404: "STORAGE_003",
            405: "CFG_002",
            507: "RESOURCE_001",
        }.get(exc.status_code, "CFG_002")
        payload = _envelope(
            code=code,
            message=str(exc.detail) or "Request rejected.",
            retryable=False,
            trace_id=trace_id,
            details={"http_status": exc.status_code},
        )
        return JSONResponse(payload, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        if not request.url.path.startswith(UI_PREFIX):
            return JSONResponse({"detail": exc.errors()}, status_code=422)
        trace_id = request_trace_id(request)
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", [])],
                "type": str(error.get("type", "")),
                "message": str(error.get("msg", "")),
            }
            for error in exc.errors()
        ]
        payload = _envelope(
            code="REQUEST_001",
            message="Request validation failed.",
            retryable=False,
            trace_id=trace_id,
            details={"errors": errors},
        )
        return JSONResponse(payload, status_code=422)

    @app.middleware("http")
    async def _trace_middleware(request: Request, call_next):
        trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
        if len(trace_id) > 128 or any(not (c.isascii() and (c.isalnum() or c in "_-.:")) for c in trace_id):
            trace_id = new_trace_id()
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers[TRACE_HEADER] = trace_id
        client_request_id = request.headers.get(CLIENT_REQUEST_HEADER)
        if client_request_id:
            # Echoed so a UI action can be correlated with its server work.
            response.headers[CLIENT_REQUEST_HEADER] = client_request_id[:128]
        return response


__all__ = [
    "CLIENT_REQUEST_HEADER",
    "TRACE_HEADER",
    "UI_PREFIX",
    "new_trace_id",
    "register",
    "request_trace_id",
]

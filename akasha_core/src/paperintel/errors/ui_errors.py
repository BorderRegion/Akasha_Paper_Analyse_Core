"""errors.ui_errors — the /v1/ui error namespace (ui_contract_version 1.0.0).

Why a separate namespace: the CORE error catalog (``errors.catalog``) is frozen
by the backend spec and may not grow at runtime — but the frontend contract
(docs/06 §通用响应) names two additional codes that are specific to the workbench
surface:

- ``RESET_CURSOR`` — the presented cursor no longer matches the query's
  filter_hash/scope_revision/sort; the client restarts from the first page
  while KEEPING its filters (409, never a silent jump to page 1).
- ``REVISION_CONFLICT`` — an optimistic-concurrency write lost the race; the
  client keeps its draft and merges (409).

The UI envelope is ``{"error": {code, message, retryable, trace_id, details}}``:
the same fields as the core envelope minus ``severity`` (a server-side log
concern), plus an HTTP status chosen per code so 409/422/404/507 are real
statuses and not "everything is 400".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paperintel.errors.exceptions import PaperIntelError

UI_ERROR_CONTRACT_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class UiErrorSpec:
    code: str
    message: str
    http_status: int
    retryable: bool
    operator_action: str


UI_ERROR_CATALOG: dict[str, UiErrorSpec] = {
    "RESET_CURSOR": UiErrorSpec(
        code="RESET_CURSOR",
        message="The cursor does not match the current query scope or filters.",
        http_status=409,
        retryable=False,
        operator_action="Restart the listing from the first page with the same filters.",
    ),
    "SCOPE_EXPIRED": UiErrorSpec(
        code="SCOPE_EXPIRED",
        message="The confirmed scope is stale; preview it again.",
        http_status=409,
        retryable=False,
        operator_action="Preview the candidates again and confirm the new scope.",
    ),
    "REVISION_CONFLICT": UiErrorSpec(
        code="REVISION_CONFLICT",
        message="The record changed on the server since the client last read it.",
        http_status=409,
        retryable=False,
        operator_action="Reload the record, keep the local draft, and merge explicitly.",
    ),
    "RATE_LIMITED": UiErrorSpec(
        code="RATE_LIMITED",
        message="Too many requests in a short window.",
        http_status=429,
        retryable=True,
        operator_action="Wait for the window to clear, then retry the same request.",
    ),
    "SCOPE_MISMATCH": UiErrorSpec(
        code="SCOPE_MISMATCH",
        message="The requested resource is outside the current query scope.",
        http_status=403,
        retryable=False,
        operator_action="Re-query the item's scope before acting on it.",
    ),
    "REQUEST_001": UiErrorSpec(
        code="REQUEST_001",
        message="Request validation failed.",
        http_status=422,
        retryable=False,
        operator_action="Fix the request fields listed in details; the schema did not change.",
    ),
}

#: Codes that exist ONLY on the /v1/ui surface. The core catalog is unchanged.
UI_ONLY_CODES = tuple(sorted(UI_ERROR_CATALOG))


class UiError(PaperIntelError):
    """Error raised by the /v1/ui surface, bound to the UI error catalog."""

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        if code not in UI_ERROR_CATALOG:
            raise KeyError(
                f"UI error code {code!r} is not in the /v1/ui error catalog; "
                "codes may not be invented at runtime"
            )
        self.spec = UI_ERROR_CATALOG[code]
        self.code = code
        super().__init__(message or self.spec.message, details=details, trace_id=trace_id)

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    def to_envelope(self, *, trace_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "trace_id": trace_id or self.trace_id,
                "details": self.details,
            }
        }


__all__ = [
    "UI_ERROR_CATALOG",
    "UI_ERROR_CONTRACT_VERSION",
    "UI_ONLY_CODES",
    "UiError",
    "UiErrorSpec",
]

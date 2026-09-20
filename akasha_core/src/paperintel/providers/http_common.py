"""Shared HTTP machinery for remote providers (OpenAI-compatible family).

Keeps transport concerns (client construction, auth headers, status/error
classification, safe body excerpts) in one place so the LLM, OCR and
embedding adapters differ only in payload/parse logic.

Security: the API key lives only in the client's default headers. It is
never written into DomainError details, logs, stats, or any serializable
status surface; body excerpts pass through the logging redactor.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from weakref import WeakKeyDictionary

import httpx

from paperintel.errors import DomainError
from paperintel.operations.logging import redact_value
from paperintel.providers.resilience import FailureClassification

#: Cap for server-provided Retry-After so a hostile/misconfigured server
#: cannot stall a worker beyond the bounded-retry budget.
MAX_RETRY_AFTER_S = 60.0


class LoopLocalHTTPClient:
    """Never reuse a connection pool on a different event loop.

    Owners must call aclose before their loop exits. Loop objects, not their
    recyclable integer IDs, identify pools; weak keys do not retain dead loops.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._kwargs = kwargs
        self._clients: WeakKeyDictionary = WeakKeyDictionary()

    def current(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(**self._kwargs)
            self._clients[loop] = client
        return client

    async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.current().post(*args, **kwargs)

    async def aclose(self) -> None:
        client = self._clients.pop(asyncio.get_running_loop(), None)
        if client is not None:
            await client.aclose()


def build_client(
    *,
    base_url: str,
    timeout_s: float,
    api_key: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    extra_headers: dict[str, str] | None = None,
) -> LoopLocalHTTPClient:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    kwargs: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "headers": headers,
        "timeout": httpx.Timeout(timeout_s, connect=min(10.0, timeout_s)),
    }
    if transport is not None:
        kwargs["transport"] = transport
    return LoopLocalHTTPClient(**kwargs)


def safe_body_excerpt(response: httpx.Response, limit: int = 200) -> str:
    """Redacted, length-capped response body for error details."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - unreadable body must not mask the error
        return "<unreadable body>"
    excerpt = text[:limit]
    return str(redact_value("body_excerpt", excerpt))


def parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        return None  # HTTP-date form not honored; fall back to backoff
    if seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)


def classify_http_failure(
    exc: Exception,
    *,
    timeout_code: str,
    rate_limit_code: str,
    provider_label: str,
) -> FailureClassification:
    """Map httpx exceptions onto catalog DomainErrors + retry decisions."""
    if isinstance(exc, httpx.TimeoutException):
        return FailureClassification(
            error=DomainError(
                timeout_code,
                message=f"{provider_label} call timed out.",
                details={},
            ),
            retry=True,
            stats_hook="transport",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        status = response.status_code
        excerpt = safe_body_excerpt(response)
        if status == 429:
            return FailureClassification(
                error=DomainError(
                    rate_limit_code,
                    message=f"{provider_label} rate limited (HTTP 429).",
                    details={"http_status": status, "body_excerpt": excerpt},
                ),
                retry=True,
                retry_after_s=parse_retry_after(response),
                stats_hook="rate_limit",
            )
        if status >= 500:
            return FailureClassification(
                error=DomainError(
                    "PROVIDER_001",
                    message=f"{provider_label} server error (HTTP {status}).",
                    details={"http_status": status, "body_excerpt": excerpt},
                ),
                retry=True,
                stats_hook="transport",
            )
        if status in (401, 403):
            # Credentials problem: retrying cannot help. The message stays
            # user-safe; no header or key material is ever included.
            return FailureClassification(
                error=DomainError(
                    "PROVIDER_001",
                    message=f"{provider_label} authentication failed (HTTP {status}); check the API key configuration.",
                    details={"http_status": status},
                ),
                retry=False,
                counts_toward_breaker=True,
                stats_hook="transport",
            )
        if status == 404:
            return FailureClassification(
                error=DomainError(
                    "PROVIDER_001",
                    message=f"{provider_label} endpoint or model not found (HTTP 404).",
                    details={"http_status": status, "body_excerpt": excerpt},
                ),
                retry=False,
                stats_hook="transport",
            )
        return FailureClassification(
            error=DomainError(
                "PROVIDER_001",
                message=f"{provider_label} request rejected (HTTP {status}).",
                details={"http_status": status, "body_excerpt": excerpt},
            ),
            retry=False,
            stats_hook="transport",
        )
    if isinstance(exc, httpx.HTTPError):
        return FailureClassification(
            error=DomainError(
                "PROVIDER_001",
                message=f"{provider_label} transport failure ({type(exc).__name__}).",
                details={},
            ),
            retry=True,
            stats_hook="transport",
        )
    if isinstance(exc, DomainError):
        from paperintel.providers.resilience import classify_domain_error

        return classify_domain_error(exc)
    return FailureClassification(
        error=DomainError(
            "PROVIDER_001",
            message=f"Unexpected {provider_label} failure.",
            details={"exception_type": type(exc).__name__},
        ),
        retry=False,
        stats_hook="transport",
    )


def parse_json_body(response: httpx.Response, *, invalid_code: str, label: str) -> Any:
    """Transport-level JSON parse of an HTTP body.

    Raises:
        DomainError: ``invalid_code`` when the body is not valid JSON.
    """
    try:
        return json.loads(response.text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise DomainError(
            invalid_code,
            message=f"{label} response is not valid JSON.",
            details={"body_excerpt": safe_body_excerpt(response)},
        ) from exc


__all__ = [
    "MAX_RETRY_AFTER_S",
    "build_client",
    "classify_http_failure",
    "parse_json_body",
    "parse_retry_after",
    "safe_body_excerpt",
]

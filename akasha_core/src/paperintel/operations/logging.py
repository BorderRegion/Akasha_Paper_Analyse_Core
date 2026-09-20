"""Structured JSON logging (spec doc 06 §6, frozen module: operations.logging).

All logs use JSON fields with the required context where applicable:
timestamp, level, module, event, paper_id, paper_version_id, job_id, task_id,
run_id, trace_id, provider_id, model_id, error_code, retryable, duration_ms.

Secrets (API keys, authorization headers, cookies, tokens, passwords) are
redacted before serialization — logs must never contain them (spec doc 01 §22).
Bare print() is not used for production debugging.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

#: Attribute names whose values are always redacted.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "api_key",
        "api_keys",
        "apikey",
        "authorization",
        "auth_header",
        "cookie",
        "cookies",
        "password",
        "secret",
        "secrets",
        "token",
        "access_token",
        "refresh_token",
        "bearer",
    }
)

#: Context fields promoted to top-level JSON keys when present (spec doc 06 §6).
CONTEXT_FIELDS: tuple[str, ...] = (
    "paper_id",
    "paper_version_id",
    "job_id",
    "task_id",
    "run_id",
    "trace_id",
    "provider_id",
    "model_id",
    "error_code",
    "retryable",
    "duration_ms",
    "module",
    "event",
)

_BEARER_RE = re.compile(r"(?i)\b(bearer|api[-_]?key|authorization)\b[^,;\n]*")
_SK_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b")

_REDACTED = "***REDACTED***"

_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


def redact_value(key: str, value: Any) -> Any:
    """Recursively redact secret-looking keys and scrub secret patterns."""
    from paperintel.operations.debug import sensitive_key

    if key.lower() in REDACTED_KEYS or sensitive_key(key):
        return _REDACTED
    if isinstance(value, str):
        value = _BEARER_RE.sub(lambda m: f"{m.group(1)} {_REDACTED}", value)
        return _SK_KEY_RE.sub(_REDACTED, value)
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(key, item) for item in value]
    return value


class JsonLogFormatter(logging.Formatter):
    """Formats records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "module": getattr(record, "module_id", None) or record.name,
            "event": getattr(record, "event", None) or record.getMessage(),
        }
        message = record.getMessage()
        if message != payload["event"]:
            payload["message"] = redact_value("message", message)

        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None and field not in ("module", "event"):
                payload[field] = value

        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload or key in CONTEXT_FIELDS:
                continue
            if key.startswith("_"):
                continue
            payload[key] = value

        payload = {k: redact_value(k, v) for k, v in payload.items()}

        if record.exc_info:
            # Stack traces go to logs (internal), never to public API responses.
            payload["exc_info"] = redact_value("exc_info", self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure root logging for the application process."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonLogFormatter()
        if json_output
        else logging.Formatter("%(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


def get_logger(name: str, **defaults: Any) -> logging.LoggerAdapter:
    """Return a logger adapter pre-bound with context fields.

    Example: ``get_logger("extraction.pdf", paper_id="pap_x")``.
    """
    adapter_extra = {"module_id": name}
    adapter_extra.update({k: v for k, v in defaults.items() if v is not None})
    return logging.LoggerAdapter(logging.getLogger(name), adapter_extra)


__all__ = [
    "CONTEXT_FIELDS",
    "REDACTED_KEYS",
    "JsonLogFormatter",
    "get_logger",
    "redact_value",
    "setup_logging",
]

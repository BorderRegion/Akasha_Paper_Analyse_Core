"""Domain exceptions and the frozen error envelope (spec doc 03 §11).

All public/internal domain errors map to::

    {
      "error": {
        "code": "LLM_003",
        "message": "Model response is not valid JSON.",
        "retryable": true,
        "severity": "WARNING",
        "trace_id": "trc_x",
        "details": {}
      }
    }

Stack traces must never leak through public API responses; ``details`` carries
only structured, user-safe values chosen by the raiser.
"""

from __future__ import annotations

from typing import Any

from paperintel.errors.catalog import ERROR_CATALOG, ErrorSpec, Severity


class PaperIntelError(Exception):
    """Base class for all domain errors raised by PaperIntel."""

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.message = message
        self.details: dict[str, Any] = details or {}
        self.trace_id = trace_id
        super().__init__(message or self.__class__.__name__)


class DomainError(PaperIntelError):
    """Error bound to a code in the frozen error catalog.

    The catalog entry supplies the user-safe message, retryability and default
    severity unless the caller overrides message/severity explicitly.
    """

    def __init__(
        self,
        code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
        severity: Severity | None = None,
    ) -> None:
        if code not in ERROR_CATALOG:
            raise KeyError(
                f"error code {code!r} is not in the frozen catalog; "
                "codes may not be invented at runtime"
            )
        self.spec: ErrorSpec = ERROR_CATALOG[code]
        self.code = code
        self.severity = severity or self.spec.severity
        super().__init__(message or self.spec.message, details=details, trace_id=trace_id)

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    @property
    def operator_action(self) -> str:
        return self.spec.operator_action

    def to_envelope(self) -> dict[str, Any]:
        """Render the frozen error envelope (spec doc 03 §11)."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "severity": str(self.severity),
                "trace_id": self.trace_id,
                "details": self.details,
            }
        }


class InvalidStateTransitionError(DomainError):
    """Raised when a task/job/pipeline state transition is not allowed.

    Spec doc 03 §9: "Invalid transitions must raise a domain error and be
    tested." Maps to ``INTERNAL_002`` with the attempted transition in details.
    """

    def __init__(
        self,
        from_state: str,
        to_state: str,
        *,
        machine: str = "task",
        trace_id: str | None = None,
    ) -> None:
        super().__init__(
            "INTERNAL_002",
            message=f"Invalid {machine} state transition: {from_state} -> {to_state}.",
            details={"machine": machine, "from_state": from_state, "to_state": to_state},
            trace_id=trace_id,
        )


__all__ = [
    "DomainError",
    "InvalidStateTransitionError",
    "PaperIntelError",
]

"""Error envelope schema (spec doc 03 §11)."""

from __future__ import annotations

from typing import Any

from pydantic import Field, field_validator

from paperintel.errors.catalog import ERROR_CATALOG, Severity
from paperintel.schemas.common import FrozenModel


class ErrorBody(FrozenModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    severity: Severity
    trace_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code")
    @classmethod
    def _code_in_catalog(cls, value: str) -> str:
        if value not in ERROR_CATALOG:
            raise ValueError(f"error code {value!r} is not in the frozen catalog")
        return value


class ErrorEnvelope(FrozenModel):
    """All public/internal domain errors map to this envelope.

    Stack traces must never leak through public API responses.
    """

    error: ErrorBody


__all__ = ["ErrorBody", "ErrorEnvelope"]

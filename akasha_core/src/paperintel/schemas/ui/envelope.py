"""schemas.ui.envelope — the UI response envelope (frontend spec docs/06).

Every `/v1/ui` read returns `{data, meta}` where meta carries the contract
version, a snapshot id, the observation time, a partial flag and warnings.
Pagination returns `{data:{items,next_cursor,has_more,total,total_kind},meta}`.

These are explicit DTOs with response_model bindings: deep `dict` shapes are
not allowed to become the client contract (docs/06 「服务与版本」).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from paperintel.schemas.common import FrozenModel, utcnow
from paperintel.version import SPEC_VERSION

UI_CONTRACT_VERSION = "1.0.0"

CorpusKind = Literal["EXACT", "ESTIMATE", "UNKNOWN"]
CapabilityAvailability = Literal["AVAILABLE", "UNAVAILABLE", "DEGRADED"]
RunMode = Literal["LIVE", "TEST"]


class UiWarning(FrozenModel):
    code: str = Field(min_length=1)
    message: str = ""


class UiMeta(FrozenModel):
    contract_version: str = UI_CONTRACT_VERSION
    snapshot_id: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=utcnow)
    partial: bool = False
    warnings: list[UiWarning] = Field(default_factory=list)


class Envelope[T](FrozenModel):
    data: T
    meta: UiMeta


class PageData[T](FrozenModel):
    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None
    has_more: bool = False
    total: int | None = None
    total_kind: CorpusKind = "UNKNOWN"
    scope_revision: str | None = None


class Page[T](FrozenModel):
    data: PageData[T]
    meta: UiMeta


class UiError(FrozenModel):
    """Error envelope returned by /v1/ui (docs/06 「错误」)."""

    code: str
    message: str
    retryable: bool = False
    trace_id: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class UiErrorEnvelope(FrozenModel):
    error: UiError


__all__ = [
    "UI_CONTRACT_VERSION",
    "CapabilityAvailability",
    "CorpusKind",
    "Envelope",
    "Page",
    "PageData",
    "RunMode",
    "UiError",
    "UiErrorEnvelope",
    "UiMeta",
    "UiWarning",
    "SPEC_VERSION",
]

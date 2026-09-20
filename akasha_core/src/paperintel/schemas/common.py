"""Shared schema primitives: typed public IDs, versioned bases, geometry.

ID typing policy
----------------
Canonical persisted entities carry STRICT prefixed IDs (full ULID payload,
spec doc 02 §2). LLM-originating candidate payloads (agent results) carry LOOSE
prefixed IDs: the Pydantic firewall layer only enforces the frozen prefix,
while strict format, existence and scope checks run in the semantic/evidence
validation layers (spec doc 02 §6). This keeps the schema layer a shape
firewall and the evidence layer the authority on references.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from paperintel.ids import IdPrefix, validate_id
from paperintel.version import SPEC_VERSION

# ---------------------------------------------------------------------------
# Base models
# ---------------------------------------------------------------------------


class FrozenModel(BaseModel):
    """Immutable base for canonical contract models."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrictModel(BaseModel):
    """Mutable base for internal/service models; still rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


class SchemaVersioned(FrozenModel):
    """Mixin binding a payload to its schema identity (spec doc 03 §17).

    Defaults let externally produced examples (templates/) validate, while
    persisted copies always carry explicit values.
    """

    schema_name: str = ""
    schema_version: str = SPEC_VERSION

    @model_validator(mode="after")
    def _default_schema_name(self) -> SchemaVersioned:
        # frozen models allow object.__setattr__-free mutation via
        # model_post_init instead; validator keeps semantics explicit.
        return self

    def model_post_init(self, context: Any) -> None:
        if not self.schema_name:
            object.__setattr__(self, "schema_name", type(self).__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Typed public IDs
# ---------------------------------------------------------------------------


def _strict_id_validator(prefix: IdPrefix):
    def _validate(value: str) -> str:
        return validate_id(value, prefix)

    return _validate


def _loose_id_validator(prefix: IdPrefix):
    pattern = re.compile(rf"^{re.escape(prefix.value)}[A-Za-z0-9]+$")

    def _validate(value: str) -> str:
        if not pattern.match(value):
            raise ValueError(f"value {value!r} is not a {prefix.value}-prefixed identifier")
        return value

    return _validate


def StrictId(prefix: IdPrefix) -> Any:
    """Annotated type: a well-formed public ID with the frozen prefix."""
    return Annotated[str, AfterValidator(_strict_id_validator(prefix))]


def LooseId(prefix: IdPrefix) -> Any:
    """Annotated type: prefix-checked ID reference from untrusted producers."""
    return Annotated[str, AfterValidator(_loose_id_validator(prefix))]


PaperId = StrictId(IdPrefix.PAPER)
PaperVersionId = StrictId(IdPrefix.PAPER_VERSION)
AssetId = StrictId(IdPrefix.ASSET)
EvidenceId = StrictId(IdPrefix.EVIDENCE)
ClaimId = StrictId(IdPrefix.CLAIM)
VerificationId = StrictId(IdPrefix.VERIFICATION)
EntityId = StrictId(IdPrefix.ENTITY)
RelationId = StrictId(IdPrefix.RELATION)
TagId = StrictId(IdPrefix.TAG)
CollectionId = StrictId(IdPrefix.COLLECTION)
JobId = StrictId(IdPrefix.JOB)
TaskId = StrictId(IdPrefix.TASK)
RunId = StrictId(IdPrefix.RUN)
ModelCallId = StrictId(IdPrefix.MODEL_CALL)
TraceId = StrictId(IdPrefix.TRACE)
ProviderId = StrictId(IdPrefix.PROVIDER)
PromptVersionId = StrictId(IdPrefix.PROMPT_VERSION)

# Loose variants for LLM-produced references (validated strictly downstream).
LooseEvidenceId = LooseId(IdPrefix.EVIDENCE)
LooseClaimId = LooseId(IdPrefix.CLAIM)
LoosePaperId = LooseId(IdPrefix.PAPER)
LoosePaperVersionId = LooseId(IdPrefix.PAPER_VERSION)
LooseSectionId = Annotated[str, AfterValidator(lambda v: _require_nonempty(v, "section_id"))]

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def _require_nonempty(value: str, field: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class BBox(FrozenModel):
    """Axis-aligned bounding box in PDF page coordinates (points, origin at
    top-left of the page). ``page`` in Evidence is 1-based."""

    x0: float = Field(ge=0)
    y0: float = Field(ge=0)
    x1: float = Field(ge=0)
    y1: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> BBox:
        # "no impossible bbox" (spec doc 06 §5): reject inverted/NaN geometry.
        if not (self.x1 >= self.x0 and self.y1 >= self.y0):
            raise ValueError(f"impossible bbox: ({self.x0},{self.y0})-({self.x1},{self.y1})")
        return self

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


class PageRange(FrozenModel):
    """Inclusive 1-based page range."""

    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> PageRange:
        if self.page_end < self.page_start:
            raise ValueError(f"page_end ({self.page_end}) < page_start ({self.page_start})")
        return self


# ---------------------------------------------------------------------------
# External provenance (spec doc 03 §1.6)
# ---------------------------------------------------------------------------


class ExternalProvenance(FrozenModel):
    """Provenance for EXTERNAL claims / externally verified facts.

    All external facts must record provenance and retrieval time
    (spec doc 01 §14).
    """

    source_provider: str = Field(min_length=1)
    source_identifier: str = Field(min_length=1)
    source_url: str | None = None
    retrieved_at: datetime
    content_hash: Sha256Hex | None = None
    citation_text: str | None = None

    @field_validator("source_url")
    @classmethod
    def _absolute_url(cls, value: str | None) -> str | None:
        if value is not None and not re.match(r"^https?://", value):
            raise ValueError("source_url must be an absolute http(s) URL")
        return value


__all__ = [n for n in dir() if not n.startswith("_")]

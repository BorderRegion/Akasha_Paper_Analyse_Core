"""Provider contracts (spec doc 02 §5 — FROZEN abstraction).

Business logic may not call provider-specific SDK code directly; all calls go
through these abstractions. Concrete adapters (OpenAI-compatible LLM, Paddle
OCR HTTP, ...) are implemented in P02; deterministic mocks live in
``paperintel.providers.mocks``.

A provider returning HTTP 200 is not automatically analytically healthy
(spec doc 00 §7.14): health is derived from canaries and quality profiles,
not reachability alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from paperintel.schemas.common import BBox, FrozenModel
from paperintel.schemas.enums import ModelRole, ProviderFamily
from paperintel.schemas.health import ModuleHealthRecord


class Provider(ABC):
    """Common base for every provider family.

    Every provider exposes an identity and a cheap health probe (spec doc 06
    §9); health is cached by callers for a bounded time.
    """

    family: ProviderFamily

    def __init__(self, provider_id: str) -> None:
        if not provider_id or not provider_id.strip():
            raise ValueError("provider_id must be non-empty")
        self.provider_id = provider_id

    @abstractmethod
    async def health(self) -> ModuleHealthRecord: ...


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class ChatMessage(FrozenModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class LlmRequest(FrozenModel):
    """One structured model call request.

    ``response_json`` requests JSON output; the schema firewall (spec doc 02
    §6) validates the parsed payload against ``expected_schema`` downstream.
    """

    messages: list[ChatMessage] = Field(min_length=1)
    model_role: ModelRole = ModelRole.ANALYST
    response_json: bool = True
    temperature: float | None = Field(default=None, ge=0.0)
    max_output_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    #: Free-form routing/diagnostic context (task/run/trace IDs); never
    #: contains secret values.
    context: dict[str, Any] = Field(default_factory=dict)


class LlmUsage(FrozenModel):
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class LlmResponse(FrozenModel):
    """Transport-level response. Content is UNVALIDATED at this point: it
    must pass the schema firewall before any canonical persistence."""

    content: str
    finish_reason: str | None = None
    usage: LlmUsage = Field(default_factory=LlmUsage)
    latency_ms: int = Field(default=0, ge=0)
    model_id: str = ""
    provider_id: str = ""
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class LLMProvider(Provider):
    """Frozen provider family: LLMProvider."""

    family = ProviderFamily.LLM

    @abstractmethod
    async def complete(self, request: LlmRequest) -> LlmResponse:
        """Run one completion. Transient failures raise DomainError with
        retryable codes (LLM_001/LLM_002/PROVIDER_001/PROVIDER_002); the
        caller owns bounded retry policy."""


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


class OcrPageRequest(FrozenModel):
    """Rendered page image handed to OCR.

    Page rasters are TEMP retention class and disposable (spec doc 07 §5);
    only evidence crops survive as canonical objects.
    """

    image_sha256: str = Field(min_length=64, max_length=64)
    page_number: int = Field(ge=1)
    mime_type: str = "image/png"
    language_hint: str | None = None
    regions: list[BBox] = Field(default_factory=list)


class OcrLine(FrozenModel):
    text: str
    #: Line confidence when the backend reports one. Native transcription
    #: backends (e.g. PaddleOCR-VL served as plain text) report NONE — it is
    #: never invented. Canonical OCR-derived Evidence still requires
    #: page-level ocr_confidence (schemas/evidence.py), so confidence-less
    #: backends cannot source canonical evidence on their own.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    bbox: BBox | None = None


class OcrPageResult(FrozenModel):
    lines: list[OcrLine] = Field(default_factory=list)
    full_text: str = ""
    mean_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: int = Field(default=0, ge=0)
    provider_id: str = ""


class OCRProvider(Provider):
    """Frozen provider family: OCRProvider (Paddle OCR HTTP in production)."""

    family = ProviderFamily.OCR

    @abstractmethod
    async def recognize_page(self, image: bytes, request: OcrPageRequest) -> OcrPageResult:
        """OCR one rendered page image."""


# ---------------------------------------------------------------------------
# Embedding / metadata / external search / storage
# ---------------------------------------------------------------------------


class EmbeddingResult(FrozenModel):
    vectors: list[list[float]] = Field(default_factory=list)
    model_id: str = ""
    dimensions: int | None = Field(default=None, ge=1)
    latency_ms: int = Field(default=0, ge=0)


class EmbeddingProvider(Provider):
    """Frozen provider family: EmbeddingProvider."""

    family = ProviderFamily.EMBEDDING

    @abstractmethod
    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a batch of texts; empty input must raise, not silently
        return an empty result."""


@dataclass(slots=True)
class MetadataRecord:
    """External metadata with mandatory provenance (spec doc 01 §14).

    The system must continue to function if external metadata sources are
    unavailable: adapters raise retryable DomainErrors; callers degrade with
    structured warnings instead of failing the pipeline.
    """

    provider: str
    identifier: str
    data: dict[str, Any]
    retrieved_at: datetime
    source_url: str | None = None
    content_hash: str | None = None


class MetadataProvider(Provider):
    """Frozen provider family: MetadataProvider (DOI/venue/author/citation)."""

    family = ProviderFamily.METADATA

    @abstractmethod
    async def fetch_by_doi(self, doi: str) -> MetadataRecord | None: ...

    @abstractmethod
    async def search_by_title(self, title: str, limit: int = 5) -> list[MetadataRecord]: ...


@dataclass(slots=True)
class ExternalSearchHit:
    provider: str
    identifier: str
    title: str
    url: str | None
    snippet: str
    retrieved_at: datetime
    extra: dict[str, Any] = field(default_factory=dict)


class ExternalSearchProvider(Provider):
    """Frozen provider family: ExternalSearchProvider (external novelty etc.).

    Every returned fact records provenance and retrieval time.
    """

    family = ProviderFamily.EXTERNAL_SEARCH

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[ExternalSearchHit]: ...


class StorageProvider(Provider):
    """Frozen provider family: StorageProvider.

    v1.0 canonical implementation is the local content-addressed object store
    (P01); the abstraction keeps future backends replaceable.
    """

    family = ProviderFamily.STORAGE

    @abstractmethod
    def put(self, data: bytes, *, kind: str) -> tuple[str, str]:
        """Store bytes; returns (sha256_hex, storage_key)."""

    @abstractmethod
    def get(self, storage_key: str) -> bytes: ...

    @abstractmethod
    def exists(self, storage_key: str) -> bool: ...

    @abstractmethod
    def delete(self, storage_key: str) -> bool:
        """Delete one object; returns True when a stored object was removed."""


__all__ = [
    "ChatMessage",
    "EmbeddingProvider",
    "EmbeddingResult",
    "ExternalSearchHit",
    "ExternalSearchProvider",
    "LLMProvider",
    "LlmRequest",
    "LlmResponse",
    "LlmUsage",
    "MetadataProvider",
    "MetadataRecord",
    "OCRProvider",
    "OcrLine",
    "OcrPageRequest",
    "OcrPageResult",
    "Provider",
    "StorageProvider",
]

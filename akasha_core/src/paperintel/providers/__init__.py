"""Provider abstraction layer (frozen module namespace: providers)."""

from paperintel.providers.base import (
    ChatMessage,
    EmbeddingProvider,
    EmbeddingResult,
    ExternalSearchHit,
    ExternalSearchProvider,
    LLMProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
    MetadataProvider,
    MetadataRecord,
    OcrLine,
    OcrPageRequest,
    OcrPageResult,
    OCRProvider,
    Provider,
    StorageProvider,
)
from paperintel.providers.registry import ProviderRegistry

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
    "ProviderRegistry",
    "StorageProvider",
]

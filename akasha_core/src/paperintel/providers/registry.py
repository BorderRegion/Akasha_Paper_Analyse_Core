"""Provider registry (spec doc 02 §5, spec doc 05 P02).

Business code resolves providers by logical name (e.g. ``llm_primary``) or by
family; it never constructs provider SDK clients directly. Full lifecycle
(circuit breakers, concurrency budgets, canaries) is implemented in P02 —
this registry establishes the frozen resolution surface in P00.
"""

from __future__ import annotations

from paperintel.errors import DomainError
from paperintel.providers.base import (
    EmbeddingProvider,
    ExternalSearchProvider,
    LLMProvider,
    MetadataProvider,
    OCRProvider,
    Provider,
    StorageProvider,
)
from paperintel.schemas.enums import ProviderFamily

_FAMILY_TYPES: dict[ProviderFamily, type[Provider]] = {
    ProviderFamily.LLM: LLMProvider,
    ProviderFamily.OCR: OCRProvider,
    ProviderFamily.EMBEDDING: EmbeddingProvider,
    ProviderFamily.METADATA: MetadataProvider,
    ProviderFamily.EXTERNAL_SEARCH: ExternalSearchProvider,
    ProviderFamily.STORAGE: StorageProvider,
}


class ProviderRegistry:
    """Name-keyed registry of provider instances."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, name: str, provider: Provider) -> None:
        if not name or not name.strip():
            raise ValueError("provider name must be non-empty")
        expected = _FAMILY_TYPES.get(provider.family)
        if expected is not None and not isinstance(provider, expected):
            raise DomainError(
                "PROVIDER_001",
                message=(
                    f"Provider {name!r} of family {provider.family.value} must be an "
                    f"instance of {expected.__name__}."
                ),
                details={"provider": name, "family": str(provider.family)},
            )
        if name in self._providers:
            raise DomainError(
                "CFG_002",
                message=f"Provider {name!r} is already registered.",
                details={"provider": name},
            )
        self._providers[name] = provider

    def get(self, name: str) -> Provider:
        try:
            return self._providers[name]
        except KeyError:
            raise DomainError(
                "PROVIDER_001",
                message=f"Provider {name!r} is not registered.",
                details={"provider": name, "registered": sorted(self._providers)},
            ) from None

    def get_llm(self, name: str) -> LLMProvider:
        provider = self.get(name)
        if not isinstance(provider, LLMProvider):
            raise DomainError(
                "PROVIDER_001",
                message=f"Provider {name!r} is not an LLMProvider.",
                details={"provider": name, "family": provider.family.value},
            )
        return provider

    def get_ocr(self, name: str) -> OCRProvider:
        provider = self.get(name)
        if not isinstance(provider, OCRProvider):
            raise DomainError(
                "PROVIDER_001",
                message=f"Provider {name!r} is not an OCRProvider.",
                details={"provider": name, "family": provider.family.value},
            )
        return provider

    def by_family(self, family: ProviderFamily) -> dict[str, Provider]:
        return {
            name: provider
            for name, provider in self._providers.items()
            if provider.family is family
        }

    def names(self) -> list[str]:
        return sorted(self._providers)


__all__ = ["ProviderRegistry"]

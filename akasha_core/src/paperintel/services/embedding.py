"""services.embedding — configured embedding provider access (P12).

One place resolves the embedding provider from configuration so REST, MCP
and the CLI behave identically (no process-local state, no per-surface
logic).
"""

from __future__ import annotations

from paperintel.config.settings import AppConfig, get_settings
from paperintel.errors import DomainError


def configured_embedder(settings: AppConfig | None = None):
    """The configured EMBEDDING-family provider, or None when absent.

    Absence is reported to the caller (search degrades to FTS and records
    that explicitly) — never a silent fake embedder.
    """
    settings = settings or get_settings()
    if settings.providers_file is None:
        return None
    from paperintel.config.provider_config import load_provider_config
    from paperintel.providers.factory import build_registry
    from paperintel.schemas.enums import ProviderFamily

    config = load_provider_config(settings.providers_file)
    registry = build_registry(config)
    providers = registry.by_family(ProviderFamily.EMBEDDING)
    if not providers:
        return None
    return registry.get(sorted(providers)[0])


def configured_llm(settings: AppConfig | None = None):
    """The configured LLM provider (analyst role). Raises CFG_001 when none
    is configured — a missing provider is a configuration error, never a
    silent fallback."""
    settings = settings or get_settings()
    if settings.providers_file is None:
        raise DomainError(
            "CFG_001",
            message="No providers file configured — an LLM provider is required.",
            details={"providers_file": None},
        )
    from paperintel.config.provider_config import load_provider_config
    from paperintel.providers.factory import build_registry
    from paperintel.schemas.enums import ProviderFamily

    config = load_provider_config(settings.providers_file)
    registry = build_registry(config)
    providers = registry.by_family(ProviderFamily.LLM)
    if not providers:
        raise DomainError(
            "CFG_001",
            message="No LLM provider configured.",
            details={"providers_file": str(settings.providers_file)},
        )
    return registry.get_llm(sorted(providers)[0])

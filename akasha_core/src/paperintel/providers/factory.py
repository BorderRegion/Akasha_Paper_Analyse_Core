"""Provider factory: build concrete providers from providers.yaml.

Wiring rules (spec doc 01 §22, doc 02 §5):
- API keys are resolved from the environment ONLY (``api_key_env`` names the
  variable); a declared-but-missing key is a hard CFG_001 failure at build
  time — never a silent anonymous fallback;
- business code receives providers through the frozen contracts in
  ``paperintel.providers.base`` via ``ProviderRegistry``;
- unimplemented adapter kinds fail loudly naming the phase that delivers them
  (no silent fallbacks).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from paperintel.config.provider_config import resolve_api_key
from paperintel.errors import DomainError
from paperintel.providers.base import Provider
from paperintel.providers.http_embeddings import OpenAICompatibleEmbeddingProvider
from paperintel.providers.http_llm import OpenAICompatibleLLMProvider
from paperintel.providers.mocks.llm import MockLLMProvider
from paperintel.providers.mocks.ocr import MockOCRProvider
from paperintel.providers.paddle_ocr import RESPONSE_MODES, PaddleOCRHttpProvider
from paperintel.providers.registry import ProviderRegistry
from paperintel.providers.resilience import CircuitBreaker, RetryPolicy
from paperintel.schemas.enums import LlmMockMode, OcrMockMode, ProviderKind
from paperintel.schemas.operations import ProviderConfigFile, ProviderEntry
from paperintel.storage.object_store import LocalObjectStore

#: Adapters delivered by later phases; referenced configs fail fast until then.
_PHASE_DELIVERED: dict[ProviderKind, str] = {
    ProviderKind.CROSSREF: "P08",
    ProviderKind.OPENALEX: "P08",
    ProviderKind.SEMANTIC_SCHOLAR: "P08",
}


def _option(entry: ProviderEntry, key: str, default: Any) -> Any:
    value = entry.options.get(key, default)
    return value


def _typed_option(entry: ProviderEntry, key: str, default: Any, caster: Any) -> Any:
    raw = entry.options.get(key, default)
    try:
        return caster(raw)
    except (TypeError, ValueError) as exc:
        raise DomainError(
            "CFG_002",
            message=f"Provider option {key!r} has an invalid value.",
            details={"option": key, "reason": type(exc).__name__},
        ) from exc


def _retry_from(entry: ProviderEntry) -> RetryPolicy:
    try:
        return RetryPolicy(
            max_attempts=_typed_option(entry, "retry_max_attempts", 3, int),
            base_delay_s=_typed_option(entry, "retry_base_delay_s", 1.0, float),
            multiplier=_typed_option(entry, "retry_multiplier", 2.0, float),
            max_delay_s=_typed_option(entry, "retry_max_delay_s", 30.0, float),
            jitter_ratio=_typed_option(entry, "retry_jitter_ratio", 0.1, float),
        )
    except ValueError as exc:
        raise DomainError(
            "CFG_002",
            message=f"Invalid retry options: {exc}",
            details={},
        ) from exc


def _breaker_from(entry: ProviderEntry, clock: Any) -> CircuitBreaker:
    try:
        return CircuitBreaker(
            failure_threshold=_typed_option(entry, "breaker_failure_threshold", 5, int),
            recovery_timeout_s=_typed_option(entry, "breaker_recovery_s", 60.0, float),
            half_open_max_calls=_typed_option(entry, "breaker_half_open_max_calls", 1, int),
            clock=clock,
        )
    except ValueError as exc:
        raise DomainError(
            "CFG_002",
            message=f"Invalid circuit breaker options: {exc}",
            details={},
        ) from exc


def _resolve_key(entry: ProviderEntry, env: dict[str, str] | None) -> str | None:
    """Env-only key resolution; declared-but-missing keys fail fast."""
    key = resolve_api_key(entry, env)
    if entry.api_key_env is not None and key is None:
        raise DomainError(
            "CFG_001",
            message=(
                f"Provider requires API key from environment variable "
                f"{entry.api_key_env!r}, but it is unset or empty."
            ),
            details={"env_var": entry.api_key_env},
        )
    return key


def _single_model(entry: ProviderEntry, role_hint: str) -> str:
    """Resolve one model name for single-model provider kinds."""
    model = entry.options.get("model")
    if isinstance(model, str) and model:
        return model
    if role_hint in entry.models:
        return entry.models[role_hint].model
    if len(entry.models) == 1:
        return next(iter(entry.models.values())).model
    raise DomainError(
        "CFG_002",
        message=f"Provider needs exactly one model (options.model or models.{role_hint}).",
        details={"models": sorted(entry.models)},
    )


def _headers(entry: ProviderEntry) -> dict[str, str] | None:
    raw = entry.options.get("headers")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise DomainError(
            "CFG_002",
            message="Provider option 'headers' must be a string mapping.",
            details={},
        )
    return dict(raw)


def build_provider(
    name: str,
    entry: ProviderEntry,
    *,
    env: dict[str, str] | None = None,
    clock: Any = None,
    sleep: Any = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> Provider:
    """Construct one provider from its config entry.

    Raises:
        DomainError: CFG_001 (missing declared API key), CFG_002 (invalid
            options), PROVIDER_001 (adapter kind not yet delivered).
    """
    import time as _time  # noqa: PLC0415
    from asyncio import sleep as _async_sleep  # noqa: PLC0415

    clock = clock or _time.monotonic
    sleep = sleep or _async_sleep
    provider_id = f"prv_{name}"

    match entry.kind:
        case ProviderKind.MOCK_LLM:
            try:
                mode = LlmMockMode(str(_option(entry, "mode", "VALID")))
            except ValueError as exc:
                raise DomainError(
                    "CFG_002",
                    message=f"Unknown LLM mock mode: {entry.options.get('mode')!r}.",
                    details={},
                ) from exc
            return MockLLMProvider(
                provider_id,
                mode=mode,
                seed=_typed_option(entry, "seed", 1000, int),
            )
        case ProviderKind.MOCK_OCR:
            try:
                mode_ocr = OcrMockMode(str(_option(entry, "mode", "VALID")))
            except ValueError as exc:
                raise DomainError(
                    "CFG_002",
                    message=f"Unknown OCR mock mode: {entry.options.get('mode')!r}.",
                    details={},
                ) from exc
            return MockOCRProvider(
                provider_id,
                mode=mode_ocr,
                seed=_typed_option(entry, "seed", 2000, int),
            )
        case ProviderKind.MOCK_EMBEDDING:
            from paperintel.providers.mocks.embedding import (  # noqa: PLC0415
                MockEmbeddingProvider,
            )

            return MockEmbeddingProvider(provider_id)
        case ProviderKind.MOCK_METADATA:
            from paperintel.providers.mocks.metadata import MockMetadataProvider  # noqa: PLC0415

            return MockMetadataProvider(provider_id)
        case ProviderKind.CROSSREF | ProviderKind.OPENALEX | ProviderKind.SEMANTIC_SCHOLAR:
            from paperintel.providers.http_metadata import HTTPMetadataProvider

            return HTTPMetadataProvider(
                provider_id,
                kind=entry.kind.value,
                base_url=entry.base_url,
                api_key=_resolve_key(entry, env),
                timeout_seconds=float(entry.timeout_seconds),
                max_concurrency=entry.max_concurrency,
                retry=_retry_from(entry),
            )
        case ProviderKind.OPENAI_COMPATIBLE:
            if not entry.models:
                raise DomainError(
                    "CFG_002",
                    message="openai_compatible provider requires at least one model role.",
                    details={"provider": name},
                )
            return OpenAICompatibleLLMProvider(
                provider_id,
                base_url=entry.base_url or "",
                models={role: rc.model for role, rc in entry.models.items()},
                temperatures={role: rc.temperature for role, rc in entry.models.items()},
                api_key=_resolve_key(entry, env),
                timeout_seconds=float(entry.timeout_seconds),
                max_concurrency=entry.max_concurrency,
                retry=_retry_from(entry),
                breaker=_breaker_from(entry, clock),
                support_response_format=bool(_option(entry, "support_response_format", True)),
                extra_headers=_headers(entry),
                clock=clock,
                sleep=sleep,
            )
        case ProviderKind.PADDLE_HTTP:
            mode = entry.options.get("response_mode", "json_lines")
            if mode not in RESPONSE_MODES:
                raise DomainError(
                    "CFG_002",
                    message=(
                        f"Provider option response_mode={mode!r} is invalid; "
                        f"expected one of {sorted(RESPONSE_MODES)}."
                    ),
                    details={"option": "response_mode"},
                )
            return PaddleOCRHttpProvider(
                provider_id,
                base_url=entry.base_url or "",
                model=_single_model(entry, "ocr"),
                api_key=_resolve_key(entry, env),
                timeout_seconds=float(entry.timeout_seconds),
                max_concurrency=entry.max_concurrency,
                retry=_retry_from(entry),
                breaker=_breaker_from(entry, clock),
                low_confidence_threshold=_typed_option(
                    entry, "low_confidence_threshold", 0.6, float
                ),
                response_mode=str(mode),
                max_output_tokens=_typed_option(entry, "max_output_tokens", 4096, int),
                extra_headers=_headers(entry),
                clock=clock,
                sleep=sleep,
            )
        case ProviderKind.HTTP_EMBEDDING:
            expected = entry.options.get("expected_dimensions")
            return OpenAICompatibleEmbeddingProvider(
                provider_id,
                base_url=entry.base_url or "",
                model=_single_model(entry, "embedding"),
                api_key=_resolve_key(entry, env),
                expected_dimensions=int(expected) if expected is not None else None,
                timeout_seconds=float(entry.timeout_seconds),
                max_concurrency=entry.max_concurrency,
                retry=_retry_from(entry),
                breaker=_breaker_from(entry, clock),
                extra_headers=_headers(entry),
                clock=clock,
                sleep=sleep,
            )
        case ProviderKind.LOCAL_OBJECT_STORE:
            root = entry.options.get("root")
            if root is None:
                base = Path(data_dir) if data_dir else Path("data")
                root = base / "objects"
            return LocalObjectStore(Path(root))
        case _:
            phase = _PHASE_DELIVERED.get(entry.kind)
            raise DomainError(
                "PROVIDER_001",
                message=(
                    f"Provider kind {entry.kind.value!r} adapter is delivered in phase "
                    f"{phase or 'a later phase'}; it cannot be built yet."
                ),
                details={"provider": name, "kind": entry.kind.value},
            )


def build_registry(
    config: ProviderConfigFile,
    *,
    env: dict[str, str] | None = None,
    clock: Any = None,
    sleep: Any = None,
    data_dir: str | os.PathLike[str] | None = None,
) -> ProviderRegistry:
    """Build and register every configured provider.

    Fails fast on the first invalid entry (no partial silent registry).
    """
    registry = ProviderRegistry()
    from paperintel.config.settings import get_settings
    from paperintel.providers.runtime import instrument

    for name, entry in config.providers.items():
        registry.register(
            name,
            instrument(
                build_provider(name, entry, env=env, clock=clock, sleep=sleep, data_dir=data_dir),
                get_settings(),
                data_dir=data_dir,
            ),
        )
    return registry


__all__ = ["build_provider", "build_registry"]

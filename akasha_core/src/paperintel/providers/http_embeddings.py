"""OpenAI-compatible embedding provider adapter (spec doc 05 P02 interface;
used by hybrid search in P09).

POST ``/embeddings`` with ``{"model": ..., "input": [...]}``; strict
validation: every vector must be a non-empty list of floats, all vectors in
one batch must share dimensions, and ``expected_dimensions`` (when
configured) must match — dimension drift would silently poison the vector
index, so it is a hard EMBED_001 failure instead.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from paperintel.errors import DomainError
from paperintel.providers.base import EmbeddingProvider, EmbeddingResult
from paperintel.providers.http_common import (
    build_client,
    classify_http_failure,
    parse_json_body,
    safe_body_excerpt,
)
from paperintel.providers.resilience import (
    CanaryRecord,
    CircuitBreaker,
    ConcurrencyLimiter,
    FailureClassification,
    ProviderStats,
    RetryPolicy,
    availability_from,
    run_with_resilience,
)
from paperintel.schemas.enums import CanaryState
from paperintel.schemas.health import ModuleHealthRecord

_EMBEDDINGS_PATH = "/embeddings"


class OpenAICompatibleEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        expected_dimensions: int | None = None,
        timeout_seconds: float = 60.0,
        max_concurrency: int = 4,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        extra_headers: dict[str, str] | None = None,
        canary_ttl_s: float = 86400.0,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        super().__init__(provider_id)
        self.base_url = base_url
        self.model = model
        self._api_key = api_key  # never rendered by __repr__/status/logs
        self.expected_dimensions = expected_dimensions
        self.timeout_seconds = timeout_seconds
        self.retry = retry or RetryPolicy(max_attempts=2)
        self.breaker = breaker or CircuitBreaker()
        self.limiter = ConcurrencyLimiter(max_concurrency)
        self.stats = ProviderStats()
        self.canary = CanaryRecord()
        self.canary_ttl_s = canary_ttl_s
        self._clock = clock
        self._sleep = sleep
        self._client = build_client(
            base_url=base_url,
            timeout_s=timeout_seconds,
            api_key=api_key,
            transport=transport,
            extra_headers=extra_headers,
        )

    def __repr__(self) -> str:
        return (
            f"<OpenAICompatibleEmbeddingProvider {self.provider_id} "
            f"base_url={self.base_url!r} model={self.model!r} api_key=[REDACTED]>"
        )

    def _classify(self, exc: Exception) -> FailureClassification:
        return classify_http_failure(
            exc,
            timeout_code="EMBED_001",
            rate_limit_code="PROVIDER_002",
            provider_label="Embedding provider",
        )

    async def _post_embeddings(self, texts: list[str]) -> EmbeddingResult:
        response = await self._client.post(
            _EMBEDDINGS_PATH,
            json={"model": self.model, "input": texts},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = parse_json_body(
            response, invalid_code="EMBED_001", label="Embedding provider envelope"
        )
        return self._parse(body, response, count=len(texts))

    def _parse(self, body: Any, response: httpx.Response, *, count: int) -> EmbeddingResult:
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise DomainError(
                "EMBED_001",
                message="Embedding response envelope malformed.",
                details={"body_excerpt": safe_body_excerpt(response)},
            )
        data = body["data"]
        if len(data) != count:
            raise DomainError(
                "EMBED_001",
                message=f"Expected {count} embeddings, got {len(data)}.",
                details={},
            )
        vectors: list[list[float]] = []
        dimensions: int | None = None
        for index, item in enumerate(data):
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if (
                not isinstance(embedding, list)
                or not embedding
                or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in embedding
                )
            ):
                raise DomainError(
                    "EMBED_001",
                    message=f"Embedding {index} is not a non-empty numeric vector.",
                    details={"index": index},
                )
            vector = [float(v) for v in embedding]
            if dimensions is None:
                dimensions = len(vector)
                if self.expected_dimensions is not None and dimensions != self.expected_dimensions:
                    raise DomainError(
                        "EMBED_001",
                        message=(
                            f"Embedding dimension {dimensions} does not match configured "
                            f"{self.expected_dimensions}; refusing to poison the vector index."
                        ),
                        details={"model": self.model},
                    )
            elif len(vector) != dimensions:
                raise DomainError(
                    "EMBED_001",
                    message=f"Embedding {index} dimension {len(vector)} != {dimensions}.",
                    details={},
                )
            vectors.append(vector)
        self.stats.record_response_validation(True)
        return EmbeddingResult(
            vectors=vectors,
            model_id=str(body.get("model") or self.model),
            dimensions=dimensions,
        )

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            raise DomainError(
                "EMBED_001",
                message="Refusing to embed an empty batch.",
                details={},
            )
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise DomainError(
                "EMBED_001",
                message="Every embedding input must be a non-empty string.",
                details={},
            )
        started = time.monotonic()
        result = await run_with_resilience(
            lambda: self._post_embeddings(texts),
            retry=self.retry,
            breaker=self.breaker,
            limiter=self.limiter,
            classify=self._classify,
            stats=self.stats,
            sleep=self._sleep,
        )
        return result.model_copy(update={"latency_ms": int((time.monotonic() - started) * 1000)})

    async def run_canary(self) -> CanaryRecord:
        started = time.monotonic()
        try:
            result = await self.embed(["PaperIntel embedding canary"])
            if not result.vectors or not result.dimensions:
                raise ValueError("empty canary vector")
            self.canary = CanaryRecord(
                state=CanaryState.OK,
                detail=f"dim={result.dimensions}",
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=self._clock(),
            )
        except (DomainError, ValueError) as exc:
            code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
            self.canary = CanaryRecord(
                state=CanaryState.FAILED,
                detail=f"canary failed: {code}",
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=self._clock(),
            )
        return self.canary

    async def health(self) -> ModuleHealthRecord:
        availability = availability_from(
            self.breaker.state, self.stats.transport_error_rate, self.stats.total_calls > 0
        )
        return ModuleHealthRecord(
            module_id=f"providers.{self.provider_id}",
            state=availability,
            checks={"configured": "PASS"},
            metrics={
                "total_calls": self.stats.total_calls,
                "latency_mean": self.stats.latency_mean,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["OpenAICompatibleEmbeddingProvider"]

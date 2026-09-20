"""OpenAI-compatible LLM provider adapter (spec doc 05 P02).

Works against any ``/chat/completions`` endpoint speaking the OpenAI wire
format (DeepSeek official API, the DLUT newapi gateway, local servers).

This adapter owns ONLY the transport layer of the schema firewall (spec doc
02 §6): HTTP → transport JSON parse → ``LlmResponse`` with unvalidated
content. Semantic validation happens downstream; the provider never invents
fallback content and never swallows failures.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from paperintel.errors import DomainError
from paperintel.providers.base import (
    ChatMessage,
    LLMProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)
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
from paperintel.schemas.enums import CanaryState, CircuitState, ModelRole
from paperintel.schemas.health import LlmProviderStatus, ModuleHealthRecord

_CHAT_PATH = "/chat/completions"
_CANARY_PROMPT = (
    "Evidence evd_canary: The dataset contains 1,000 samples. "
    "Method A reaches 82.5%% accuracy. "
    "Extract dataset_size and accuracy as JSON numbers, and evidence_id as a string. "
    'Also include these request markers: {"canary": "ok", "echo": "%s"}. '
    "Return only the JSON object."
)


class OpenAICompatibleLLMProvider(LLMProvider):
    """LLMProvider for OpenAI-compatible chat-completion servers."""

    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str,
        models: dict[ModelRole | str, str],
        temperatures: dict[str, float | None] | None = None,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_concurrency: int = 8,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        default_temperature: float | None = None,
        support_response_format: bool = True,
        extra_headers: dict[str, str] | None = None,
        canary_ttl_s: float = 86400.0,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        super().__init__(provider_id)
        if not models:
            raise ValueError("at least one model must be configured")
        self.base_url = base_url
        # Normalize role keys: accept ModelRole or its string value.
        self.models: dict[str, str] = {
            (role.value if isinstance(role, ModelRole) else str(role)): model
            for role, model in models.items()
        }
        self.temperatures: dict[str, float | None] = dict(temperatures or {})
        self._api_key = api_key  # never rendered by __repr__/status/logs
        self.timeout_seconds = timeout_seconds
        self.default_temperature = default_temperature
        self.support_response_format = support_response_format
        self.retry = retry or RetryPolicy()
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
            f"<OpenAICompatibleLLMProvider {self.provider_id} "
            f"base_url={self.base_url!r} models={sorted(self.models.values())} "
            "api_key=[REDACTED]>"
        )

    # -- model resolution -----------------------------------------------------

    def model_for(self, role: ModelRole | str) -> str:
        key = role.value if isinstance(role, ModelRole) else str(role)
        if key in self.models:
            return self.models[key]
        if len(self.models) == 1:
            return next(iter(self.models.values()))
        raise DomainError(
            "CFG_002",
            message=f"No model configured for role {key!r}.",
            details={"provider_id": self.provider_id, "role": key},
        )

    def temperature_for(self, role: ModelRole | str) -> float | None:
        key = role.value if isinstance(role, ModelRole) else str(role)
        if key in self.temperatures:
            return self.temperatures[key]
        return self.default_temperature

    # -- transport --------------------------------------------------------------

    def _payload(self, request: LlmRequest, nonce: str | None = None) -> dict[str, Any]:
        model = self.model_for(request.model_role)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        temperature = (
            request.temperature
            if request.temperature is not None
            else self.temperature_for(request.model_role)
        )
        if temperature is not None:
            payload["temperature"] = temperature
        if request.max_output_tokens is not None:
            payload["max_tokens"] = request.max_output_tokens
        if request.response_json and self.support_response_format:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _classify(self, exc: Exception) -> FailureClassification:
        return classify_http_failure(
            exc,
            timeout_code="LLM_001",
            rate_limit_code="LLM_002",
            provider_label="LLM provider",
        )

    async def _post_chat(self, request: LlmRequest) -> LlmResponse:
        payload = self._payload(request)
        timeout = request.timeout_seconds or self.timeout_seconds
        response = await self._client.post(_CHAT_PATH, json=payload, timeout=timeout)
        response.raise_for_status()
        body = parse_json_body(response, invalid_code="PROVIDER_001", label="LLM provider envelope")
        return self._to_llm_response(body, response)

    def _to_llm_response(self, body: Any, response: httpx.Response) -> LlmResponse:
        if not isinstance(body, dict):
            raise DomainError(
                "PROVIDER_001",
                message="LLM provider envelope is not a JSON object.",
                details={"body_excerpt": safe_body_excerpt(response)},
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DomainError(
                "PROVIDER_001",
                message="LLM provider response contains no choices.",
                details={"body_excerpt": safe_body_excerpt(response)},
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise DomainError(
                "PROVIDER_001",
                message="LLM provider choice has no message object.",
                details={},
            )
        content = message.get("content")
        reasoning_present = bool(message.get("reasoning_content"))
        if content is None:
            # No silent fallback: empty content is reported as-is (e.g. a
            # reasoning model that exhausted max_tokens before answering);
            # callers see finish_reason and the reasoning flag.
            content = ""
        usage = body.get("usage") or {}
        latency_header = None  # latency is measured locally by resilience
        result = LlmResponse(
            content=content if isinstance(content, str) else json.dumps(content),
            finish_reason=choices[0].get("finish_reason"),
            usage=LlmUsage(
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            ),
            latency_ms=int(latency_header or 0),
            model_id=str(body.get("model") or ""),
            provider_id=self.provider_id,
            raw_metadata={
                "response_id": body.get("id"),
                "reasoning_content_present": reasoning_present,
            },
        )
        return result

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """One resilient completion call (semaphore → breaker → bounded retry).

        Raises:
            DomainError: LLM_001 (timeout), LLM_002 (429), LLM_003 (body not
                JSON), PROVIDER_001 (5xx/4xx/transport/envelope),
                PROVIDER_003 (circuit open).
        """
        started = time.monotonic()
        response: LlmResponse = await run_with_resilience(
            lambda: self._post_chat(request),
            retry=self.retry,
            breaker=self.breaker,
            limiter=self.limiter,
            classify=self._classify,
            stats=self.stats,
            sleep=self._sleep,
        )
        # Transport JSON parsed; when JSON output was requested, cheaply probe
        # whether the model CONTENT is valid JSON (diagnostic rate only — the
        # schema firewall stays authoritative downstream).
        if request.response_json:
            try:
                json.loads(response.content)
                self.stats.record_json_parse(valid=True)
            except (json.JSONDecodeError, ValueError):
                self.stats.record_json_parse(valid=False)
        filled = response.model_copy(
            update={
                "latency_ms": int((time.monotonic() - started) * 1000),
                "provider_id": self.provider_id,
            }
        )
        return filled

    # -- canary -------------------------------------------------------------------

    async def run_canary(self, nonce: str = "paperintel") -> CanaryRecord:
        """Active health probe: strict echo JSON round-trip (doc 04 §6)."""
        prompt = _CANARY_PROMPT % nonce
        request = LlmRequest(
            messages=[ChatMessage(role="user", content=prompt)],
            model_role=ModelRole.ANALYST,
            response_json=True,
            temperature=0.0,
            # Reasoning models (e.g. DeepSeek-V4) spend output tokens on
            # reasoning_content before answering; a tight budget would make
            # the canary fail on healthy providers.
            max_output_tokens=2048,
        )
        started = time.monotonic()
        try:
            response = await self.complete(request)
            payload = json.loads(response.content)
            if (
                not isinstance(payload, dict)
                or payload.get("echo") != nonce
                or payload.get("dataset_size") != 1000
                or payload.get("accuracy") != 82.5
                or payload.get("evidence_id") != "evd_canary"
            ):
                raise ValueError("unexpected canary payload keys")
            self.canary = CanaryRecord(
                state=CanaryState.OK,
                detail="fixed evidence, numeric anchors and evidence reference verified",
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=self._clock(),
            )
        except (DomainError, ValueError, json.JSONDecodeError) as exc:
            code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
            self.canary = CanaryRecord(
                state=CanaryState.FAILED,
                detail=f"canary failed: {code}",
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=self._clock(),
            )
        return self.canary

    # -- status / health -------------------------------------------------------------

    def status(self) -> LlmProviderStatus:
        availability = availability_from(
            self.breaker.state,
            self.stats.transport_error_rate,
            self.stats.total_calls > 0,
            self.canary.effective_state(self._clock, self.canary_ttl_s),
        )
        canary_state = self.canary.effective_state(self._clock, self.canary_ttl_s)
        return LlmProviderStatus(
            provider_id=self.provider_id,
            availability=availability,
            configured_concurrency=self.limiter.configured,
            active_concurrency=self.limiter.active,
            latency_p50=self.stats.latency_p50,
            latency_p90=self.stats.latency_p90,
            rate_limit_events=self.stats.rate_limit_events,
            transport_error_rate=self.stats.transport_error_rate,
            valid_json_rate=self.stats.valid_json_rate,
            schema_pass_rate=self.stats.schema_pass_rate,
            citation_pass_rate=self.stats.citation_pass_rate,
            unsupported_claim_rate=self.stats.unsupported_claim_rate,
            canary_state=canary_state,
            circuit_breaker_state=self.breaker.state,
        )

    async def health(self) -> ModuleHealthRecord:
        status = self.status()
        checks = {
            "configured": "PASS",
            "circuit_breaker": "PASS"
            if status.circuit_breaker_state is not CircuitState.OPEN
            else "FAIL",
        }
        return ModuleHealthRecord(
            module_id=f"providers.{self.provider_id}",
            state=status.availability,
            checks=checks,
            metrics={
                "total_calls": self.stats.total_calls,
                "latency_p50": status.latency_p50,
                "rate_limit_events": status.rate_limit_events,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["OpenAICompatibleLLMProvider"]

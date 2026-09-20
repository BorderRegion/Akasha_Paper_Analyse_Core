"""OpenAI-compatible LLM provider tests (offline, httpx.MockTransport).

Covers the P02 spec test list: timeout, 429, 5xx, malformed JSON, provider
recovery, concurrency ceiling, circuit breaker, key redaction.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from tests.unit.provider_helpers import (
    FakeClock,
    RecordingSleeper,
    SequenceHandler,
    chat_response,
    json_response,
    make_transport,
    raw_response,
)

from paperintel.errors import DomainError
from paperintel.providers.base import ChatMessage, LlmRequest
from paperintel.providers.http_llm import OpenAICompatibleLLMProvider
from paperintel.providers.resilience import CircuitBreaker, RetryPolicy
from paperintel.schemas.enums import CanaryState, CircuitState, ModelRole, ModuleHealthState

API_KEY = "sk-test-0000000000000000secret"


def make_provider(handler, **kwargs) -> OpenAICompatibleLLMProvider:
    clock = kwargs.pop("clock", FakeClock())
    sleep = kwargs.pop("sleep", RecordingSleeper(clock))
    defaults = {
        "base_url": "https://llm.example.invalid/v1",
        "models": {ModelRole.ANALYST: "test-model"},
        "api_key": API_KEY,
        "timeout_seconds": 5.0,
        "max_concurrency": 4,
        "retry": RetryPolicy(max_attempts=3, base_delay_s=0.01, jitter_ratio=0.0),
        "transport": make_transport(handler),
        "clock": clock,
        "sleep": sleep,
    }
    defaults.update(kwargs)
    return OpenAICompatibleLLMProvider("prv_test_llm", **defaults)


def simple_request() -> LlmRequest:
    return LlmRequest(
        messages=[ChatMessage(role="user", content="hello")],
        model_role=ModelRole.ANALYST,
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# happy path + payload shape
# ---------------------------------------------------------------------------


def test_successful_completion_parses_response() -> None:
    handler = SequenceHandler(chat_response('{"answer": 42}', model="test-model"))
    provider = make_provider(handler)
    response = run(provider.complete(simple_request()))
    assert response.content == '{"answer": 42}'
    assert response.finish_reason == "stop"
    assert response.usage.input_tokens == 10
    assert response.usage.output_tokens == 5
    assert response.model_id == "test-model"
    assert response.provider_id == "prv_test_llm"
    assert response.latency_ms >= 0
    assert provider.stats.valid_json_rate == 1.0
    # Request payload shape
    sent = json.loads(handler.requests[0].content)
    assert sent["model"] == "test-model"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"] == [{"role": "user", "content": "hello"}]
    assert handler.requests[0].headers["authorization"] == f"Bearer {API_KEY}"


def test_response_format_disabled_via_option() -> None:
    handler = SequenceHandler(chat_response("{}"))
    provider = make_provider(handler, support_response_format=False)
    run(provider.complete(simple_request()))
    sent = json.loads(handler.requests[0].content)
    assert "response_format" not in sent


def test_role_model_and_temperature_resolution() -> None:
    handler = SequenceHandler(chat_response("{}"))
    provider = make_provider(
        handler,
        models={ModelRole.ANALYST: "analyst-model", ModelRole.VERIFIER: "verifier-model"},
        temperatures={"verifier": 0.2},
    )
    request = LlmRequest(
        messages=[ChatMessage(role="user", content="v")], model_role=ModelRole.VERIFIER
    )
    run(provider.complete(request))
    sent = json.loads(handler.requests[0].content)
    assert sent["model"] == "verifier-model"
    assert sent["temperature"] == 0.2
    # Unknown role with multiple models configured → CFG_002, never a guess.
    with pytest.raises(DomainError) as excinfo:
        provider.model_for(ModelRole.SYNTHESIZER)
    assert excinfo.value.code == "CFG_002"


def test_reasoning_model_empty_content_not_fabricated() -> None:
    """DeepSeek-style reasoning response: content may be empty when tokens
    went into reasoning_content. No silent fallback substitutes reasoning
    text; the flag is exposed instead."""
    handler = SequenceHandler(
        chat_response(None, reasoning_content="long thinking...", finish_reason="length")
    )
    provider = make_provider(handler)
    response = run(provider.complete(simple_request()))
    assert response.content == ""
    assert response.finish_reason == "length"
    assert response.raw_metadata["reasoning_content_present"] is True
    assert "long thinking" not in response.content


# ---------------------------------------------------------------------------
# timeout / 429 / 5xx / malformed
# ---------------------------------------------------------------------------


def test_timeout_maps_to_llm001_after_bounded_retries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    provider = make_provider(handler, clock=clock, sleep=sleeper)
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    assert excinfo.value.code == "LLM_001"
    assert excinfo.value.retryable is True
    assert provider.stats.transport_errors == 3  # max_attempts=3
    assert len(sleeper.delays) == 2  # slept between attempts, not after the last


def test_429_maps_to_llm002_and_honors_retry_after() -> None:
    responses = [
        httpx.Response(429, headers={"Retry-After": "0.05"}, content=b"slow down"),
        chat_response('{"ok": true}'),
    ]
    seq = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(seq)

    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    provider = make_provider(handler, clock=clock, sleep=sleeper)
    response = run(provider.complete(simple_request()))
    assert response.content == '{"ok": true}'
    assert sleeper.delays == [0.05]  # server-provided Retry-After honored
    assert provider.stats.rate_limit_events == 1


def test_429_persistent_raises_llm002() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=b"nope")

    provider = make_provider(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    assert excinfo.value.code == "LLM_002"
    assert provider.stats.rate_limit_events == 3


def test_5xx_then_success_is_provider_recovery() -> None:
    handler = SequenceHandler(
        httpx.Response(503, content=b"unavailable"),
        httpx.Response(500, content=b"boom"),
        chat_response('{"recovered": true}'),
    )
    provider = make_provider(handler)
    response = run(provider.complete(simple_request()))
    assert json.loads(response.content) == {"recovered": True}
    assert provider.stats.transport_errors == 2
    assert provider.breaker.state is CircuitState.CLOSED


def test_5xx_persistent_raises_provider001() -> None:
    handler = SequenceHandler(httpx.Response(502, content=b"bad gateway"))
    provider = make_provider(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    assert excinfo.value.code == "PROVIDER_001"
    assert excinfo.value.details["http_status"] == 502


def test_malformed_http_body_is_provider001() -> None:
    handler = SequenceHandler(raw_response("<html>not json at all</html>"))
    provider = make_provider(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    # Transport envelope garbled → provider-level failure (bounded retries).
    assert excinfo.value.code == "PROVIDER_001"
    assert provider.stats.valid_json_rate is None  # content probe never reached


def test_missing_choices_is_provider001() -> None:
    handler = SequenceHandler(json_response({"id": "x", "data": []}))
    provider = make_provider(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    assert excinfo.value.code == "PROVIDER_001"


def test_non_json_model_content_flagged_in_stats_but_returned() -> None:
    """The provider does NOT repair or reject model content (schema firewall
    owns that downstream, LLM_003); it records the diagnostic rate."""
    handler = SequenceHandler(chat_response("I cannot answer that. ##"))
    provider = make_provider(handler)
    response = run(provider.complete(simple_request()))
    assert response.content == "I cannot answer that. ##"
    assert provider.stats.valid_json_rate == 0.0


def test_auth_failure_not_retried_and_secret_safe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b'{"error": "invalid authentication"}')

    provider = make_provider(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    envelope = excinfo.value.to_envelope()
    dumped = json.dumps(envelope, default=str)
    assert "401" in dumped
    assert API_KEY not in dumped
    assert provider.stats.total_calls == 1  # never retried


# ---------------------------------------------------------------------------
# concurrency ceiling + circuit breaker
# ---------------------------------------------------------------------------


def test_concurrency_ceiling_enforced_over_http() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return chat_response("{}")

    provider = make_provider(handler, max_concurrency=2)

    async def main() -> None:
        await asyncio.gather(*(provider.complete(simple_request()) for _ in range(8)))

    run(main())
    assert peak <= 2
    assert provider.limiter.active == 0


def test_circuit_breaker_opens_and_recovers() -> None:
    calls = {"n": 0}
    healthy = {"flag": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if not healthy["flag"]:
            return httpx.Response(500, content=b"down")
        return chat_response('{"ok": true}')

    clock = FakeClock()
    sleeper = RecordingSleeper(clock)
    breaker = CircuitBreaker(
        failure_threshold=3, recovery_timeout_s=30.0, half_open_max_calls=1, clock=clock
    )
    provider = make_provider(
        handler,
        breaker=breaker,
        clock=clock,
        sleep=sleeper,
        retry=RetryPolicy(max_attempts=1),
    )

    # Three failed calls trip the breaker.
    for _ in range(3):
        with pytest.raises(DomainError):
            run(provider.complete(simple_request()))
    assert breaker.state is CircuitState.OPEN

    # Fast rejection: no new HTTP calls while open.
    http_calls_at_open = calls["n"]
    with pytest.raises(DomainError) as excinfo:
        run(provider.complete(simple_request()))
    assert excinfo.value.code == "PROVIDER_003"
    assert calls["n"] == http_calls_at_open
    assert provider.status().availability is ModuleHealthState.UNAVAILABLE

    # After the recovery window: half-open probe succeeds → CLOSED.
    healthy["flag"] = True
    clock.advance(31.0)
    assert breaker.state is CircuitState.HALF_OPEN
    response = run(provider.complete(simple_request()))
    assert json.loads(response.content) == {"ok": True}
    assert breaker.state is CircuitState.CLOSED
    assert provider.status().availability in {
        ModuleHealthState.HEALTHY,
        ModuleHealthState.DEGRADED,  # error rate from the incident still recent
    }


# ---------------------------------------------------------------------------
# key redaction surfaces
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_serializable_surfaces() -> None:
    handler = SequenceHandler(chat_response("{}"))
    provider = make_provider(handler)
    run(provider.complete(simple_request()))

    surfaces = [
        repr(provider),
        str(provider),
        provider.status().model_dump_json(),
        json.dumps(run(provider.health()).model_dump(mode="json"), default=str),
    ]

    # And an error surface:
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=f"upstream said {API_KEY}".encode())

    failing_provider = make_provider(SequenceHandler(failing))
    with pytest.raises(DomainError) as excinfo:
        run(failing_provider.complete(simple_request()))
    surfaces.append(json.dumps(excinfo.value.to_envelope(), default=str))

    for surface in surfaces:
        assert API_KEY not in surface
        assert "0000000000000000secret" not in surface
    assert "[REDACTED]" in repr(provider)


# ---------------------------------------------------------------------------
# canary + status exposures
# ---------------------------------------------------------------------------


def test_canary_ok_and_failed_and_stale() -> None:
    nonce_seen = {}

    def good_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        prompt = payload["messages"][-1]["content"]
        nonce_seen["prompt"] = prompt
        # The canary prompt embeds the nonce as: {"canary": "ok", "echo": "<nonce>"}
        nonce = prompt.split('"echo": "', 1)[1].split('"', 1)[0]
        return chat_response(
            json.dumps(
                {
                    "canary": "ok",
                    "echo": nonce,
                    "dataset_size": 1000,
                    "accuracy": 82.5,
                    "evidence_id": "evd_canary",
                }
            )
        )

    clock = FakeClock()
    provider = make_provider(good_handler, clock=clock)
    record = run(provider.run_canary(nonce="n-123"))
    assert record.state is CanaryState.OK
    assert "n-123" in nonce_seen["prompt"]

    # STALE after the TTL with no re-run.
    clock.advance(86401.0)
    assert provider.status().canary_state is CanaryState.STALE

    # FAILED when the echo does not match.
    bad = make_provider(SequenceHandler(chat_response('{"canary": "ok", "echo": "wrong"}')))
    record = run(bad.run_canary(nonce="expected"))
    assert record.state is CanaryState.FAILED

    # FAILED with the catalog code on transport death.
    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    dead_provider = make_provider(dead)
    record = run(dead_provider.run_canary())
    assert record.state is CanaryState.FAILED
    assert "PROVIDER_001" in record.detail


def test_status_exposure_fields_match_doc04() -> None:
    handler = SequenceHandler(chat_response('{"a": 1}'))
    provider = make_provider(handler)
    run(provider.complete(simple_request()))
    provider.stats.record_schema(True)
    provider.stats.record_schema(False)
    provider.stats.record_citation(True)
    provider.stats.record_unsupported_claim()

    status = provider.status()
    dumped = status.model_dump()
    for field_name in (
        "availability",
        "configured_concurrency",
        "active_concurrency",
        "latency_p50",
        "latency_p90",
        "rate_limit_events",
        "transport_error_rate",
        "valid_json_rate",
        "schema_pass_rate",
        "citation_pass_rate",
        "unsupported_claim_rate",
        "canary_state",
        "circuit_breaker_state",
    ):
        assert field_name in dumped
    assert status.configured_concurrency == 4
    assert status.valid_json_rate == 1.0
    assert status.schema_pass_rate == 0.5
    assert status.circuit_breaker_state is CircuitState.CLOSED

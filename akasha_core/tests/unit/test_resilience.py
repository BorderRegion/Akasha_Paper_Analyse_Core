"""Resilience primitive tests: bounded retry, circuit breaker, semaphore,
stats, classification (spec doc 05 P02)."""

from __future__ import annotations

import asyncio
import random

import httpx
import pytest
from tests.unit.provider_helpers import FakeClock, RecordingSleeper

from paperintel.errors import DomainError
from paperintel.providers.http_common import MAX_RETRY_AFTER_S, classify_http_failure
from paperintel.providers.resilience import (
    CircuitBreaker,
    ConcurrencyLimiter,
    FailureClassification,
    ProviderStats,
    RetryPolicy,
    availability_from,
    classify_domain_error,
    run_with_resilience,
)
from paperintel.schemas.enums import CircuitState, ModuleHealthState

# ---------------------------------------------------------------------------
# RetryPolicy
# ---------------------------------------------------------------------------


def test_retry_policy_delays_are_bounded_exponential() -> None:
    policy = RetryPolicy(
        max_attempts=5, base_delay_s=1.0, multiplier=2.0, max_delay_s=10.0, jitter_ratio=0.0
    )
    delays = [policy.delay_for(n) for n in range(1, 6)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 10.0]  # capped at max_delay_s


def test_retry_policy_jitter_stays_within_bounds() -> None:
    policy = RetryPolicy(base_delay_s=2.0, jitter_ratio=0.25)
    rng = random.Random(42)
    for _ in range(50):
        delay = policy.delay_for(1, rng)
        assert 1.5 <= delay <= 2.5


def test_retry_policy_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(multiplier=0.5)
    with pytest.raises(ValueError):
        RetryPolicy(jitter_ratio=1.5)
    with pytest.raises(ValueError):
        RetryPolicy(base_delay_s=-1.0)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


def test_breaker_opens_after_threshold_and_rejects_fast() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_timeout_s=30.0, clock=clock)
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(DomainError) as excinfo:
        asyncio.run(breaker.acquire())
    assert excinfo.value.code == "PROVIDER_003"
    assert excinfo.value.retryable is True  # catalog semantics preserved


def test_breaker_success_resets_consecutive_failures() -> None:
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED


def test_breaker_half_open_after_recovery_timeout() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout_s=30.0, half_open_max_calls=1, clock=clock
    )
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    clock.advance(29.9)
    assert breaker.state is CircuitState.OPEN
    clock.advance(0.2)
    assert breaker.state is CircuitState.HALF_OPEN
    # One probe may pass; the second is rejected while the probe is in flight.
    asyncio.run(breaker.acquire())
    with pytest.raises(DomainError) as excinfo:
        asyncio.run(breaker.acquire())
    assert excinfo.value.code == "PROVIDER_003"
    # Probe failure re-opens the circuit.
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    # After the next window a successful probe closes it.
    clock.advance(31.0)
    assert breaker.state is CircuitState.HALF_OPEN
    asyncio.run(breaker.acquire())
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED


def test_breaker_validation() -> None:
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError):
        CircuitBreaker(recovery_timeout_s=0)
    with pytest.raises(ValueError):
        CircuitBreaker(half_open_max_calls=0)


# ---------------------------------------------------------------------------
# Concurrency ceiling
# ---------------------------------------------------------------------------


def test_concurrency_ceiling_enforced() -> None:
    limiter = ConcurrencyLimiter(3)
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(peak, active)
            assert limiter.active == active
            await asyncio.sleep(0.01)
            active -= 1

    async def main() -> None:
        await asyncio.gather(*(worker() for _ in range(12)))

    asyncio.run(main())
    assert peak <= 3
    assert limiter.active == 0
    assert limiter.configured == 3


# ---------------------------------------------------------------------------
# run_with_resilience
# ---------------------------------------------------------------------------


def _rig(**overrides):
    params = {
        "retry": RetryPolicy(max_attempts=3, base_delay_s=0.01, jitter_ratio=0.0),
        "breaker": CircuitBreaker(failure_threshold=100),
        "limiter": ConcurrencyLimiter(4),
        "stats": ProviderStats(),
        "sleep": RecordingSleeper(),
    }
    params.update(overrides)
    return params


def test_retry_recovers_from_transient_failures() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise DomainError("PROVIDER_001", message="transient")
        return "ok"

    rig = _rig()
    result = asyncio.run(run_with_resilience(flaky, **rig))
    assert result == "ok"
    assert calls["n"] == 3
    assert rig["sleep"].delays == [0.01, 0.02]  # bounded exponential
    # Per-attempt semantics: 3 attempts = 2 transport errors + 1 success.
    assert rig["stats"].total_calls == 3
    assert rig["stats"].successful_calls == 1
    assert rig["stats"].transport_errors == 2


def test_retry_exhaustion_raises_last_error() -> None:
    async def always_fail() -> None:
        raise DomainError("PROVIDER_001", message="still down")

    rig = _rig()
    with pytest.raises(DomainError) as excinfo:
        asyncio.run(run_with_resilience(always_fail, **rig))
    assert excinfo.value.code == "PROVIDER_001"
    assert rig["stats"].total_calls == 3
    assert rig["stats"].successful_calls == 0
    assert rig["breaker"]._consecutive_failures == 3


def test_non_retryable_fails_immediately() -> None:
    calls = {"n": 0}

    async def semantic_reject() -> None:
        calls["n"] += 1
        # LLM_005 is retryable=False in the frozen catalog.
        raise DomainError("LLM_005", message="semantic validation failed")

    rig = _rig()
    with pytest.raises(DomainError):
        asyncio.run(run_with_resilience(semantic_reject, **rig))
    assert calls["n"] == 1
    assert rig["sleep"].delays == []


def test_retry_after_override_is_used() -> None:
    async def rate_limited() -> None:
        raise DomainError("LLM_002", message="slow down")

    def classify(exc: Exception) -> FailureClassification:
        return FailureClassification(
            error=exc, retry=True, retry_after_s=7.5, stats_hook="rate_limit"
        )

    rig = _rig(classify=classify)
    with pytest.raises(DomainError):
        asyncio.run(run_with_resilience(rate_limited, **rig))
    assert rig["sleep"].delays == [7.5, 7.5]
    assert rig["stats"].rate_limit_events == 3


def test_open_breaker_short_circuits_before_operation() -> None:
    calls = {"n": 0}

    async def op() -> str:
        calls["n"] += 1
        return "ok"

    breaker = CircuitBreaker(failure_threshold=1)
    breaker.record_failure()  # trips open
    rig = _rig(breaker=breaker)
    with pytest.raises(DomainError) as excinfo:
        asyncio.run(run_with_resilience(op, **rig))
    assert excinfo.value.code == "PROVIDER_003"
    assert calls["n"] == 0  # never reached the operation


def test_content_codes_not_retried_at_transport_level() -> None:
    for code in ("LLM_003", "LLM_004", "OCR_002"):
        classification = classify_domain_error(DomainError(code, message="x"))
        assert classification.retry is False


# ---------------------------------------------------------------------------
# HTTP classification
# ---------------------------------------------------------------------------


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://example.invalid/chat/completions")


def test_classify_timeout_maps_llm001() -> None:
    exc = httpx.ReadTimeout("timed out", request=_request())
    classification = classify_http_failure(
        exc, timeout_code="LLM_001", rate_limit_code="LLM_002", provider_label="LLM provider"
    )
    assert classification.error.code == "LLM_001"
    assert classification.retry is True
    assert classification.stats_hook == "transport"


def test_classify_429_honors_capped_retry_after() -> None:
    response = httpx.Response(429, headers={"Retry-After": "9999"}, request=_request())
    exc = httpx.HTTPStatusError("429", request=_request(), response=response)
    classification = classify_http_failure(
        exc, timeout_code="LLM_001", rate_limit_code="LLM_002", provider_label="LLM provider"
    )
    assert classification.error.code == "LLM_002"
    assert classification.retry_after_s == MAX_RETRY_AFTER_S  # hostile values capped
    assert classification.stats_hook == "rate_limit"


def test_classify_5xx_retryable_4xx_not() -> None:
    for status, expect_retry in ((500, True), (502, True), (503, True), (400, False), (404, False)):
        response = httpx.Response(status, request=_request(), content=b"oops")
        exc = httpx.HTTPStatusError(str(status), request=_request(), response=response)
        classification = classify_http_failure(
            exc, timeout_code="OCR_001", rate_limit_code="PROVIDER_002", provider_label="OCR"
        )
        assert classification.error.code == "PROVIDER_001"
        assert classification.retry is expect_retry


def test_classify_auth_failure_excludes_secret_material() -> None:
    response = httpx.Response(
        401, request=_request(), content=b'{"error":"invalid api key sk-SECRETVALUE"}'
    )
    exc = httpx.HTTPStatusError("401", request=_request(), response=response)
    classification = classify_http_failure(
        exc, timeout_code="LLM_001", rate_limit_code="LLM_002", provider_label="LLM provider"
    )
    assert classification.retry is False
    envelope = classification.error.to_envelope()
    dumped = str(envelope)
    assert "sk-SECRETVALUE" not in dumped  # body excerpt passes the redactor


# ---------------------------------------------------------------------------
# Stats + availability mapping
# ---------------------------------------------------------------------------


def test_stats_percentiles_and_rates() -> None:
    stats = ProviderStats()
    assert stats.latency_p50 is None
    assert stats.transport_error_rate is None
    for ms in range(1, 101):
        stats.record_latency(float(ms))
    assert stats.latency_p50 == pytest.approx(50.5, abs=1.0)
    assert stats.latency_p90 == pytest.approx(90.5, abs=1.0)
    stats.record_json_parse(True)
    stats.record_json_parse(False)
    stats.record_json_parse(True)
    assert stats.valid_json_rate == pytest.approx(2 / 3, abs=1e-3)
    stats.record_schema(True)
    stats.record_schema(False)
    assert stats.schema_pass_rate == 0.5
    stats.record_response_validation(True)
    assert stats.response_validation_rate == 1.0


def test_availability_mapping() -> None:
    assert availability_from(CircuitState.CLOSED, None, False) is ModuleHealthState.UNKNOWN
    assert availability_from(CircuitState.CLOSED, 0.0, True) is ModuleHealthState.HEALTHY
    assert availability_from(CircuitState.CLOSED, 0.75, True) is ModuleHealthState.DEGRADED
    assert availability_from(CircuitState.HALF_OPEN, 0.1, True) is ModuleHealthState.DEGRADED
    assert availability_from(CircuitState.OPEN, 0.1, True) is ModuleHealthState.UNAVAILABLE

"""Provider resilience primitives (spec doc 05 P02).

- semaphore/concurrency ceiling;
- retry with BOUNDED exponential backoff (+ optional Retry-After from 429);
- 429 handling;
- circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED);
- provider statistics feeding the frozen status exposures (doc 04 §6).

Rules honored:
- never retry forever: attempts are bounded by RetryPolicy.max_attempts
  (spec doc 02: forbidden behavior "retry forever");
- no silent fallbacks: every failure surfaces as a DomainError with a
  catalog code; classification decides retryability explicitly;
- secrets never enter error details, stats, or logs (keys are held by the
  provider objects and excluded from every serializable surface).
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakKeyDictionary

from paperintel.errors import DomainError
from paperintel.schemas.enums import CanaryState, CircuitState, ModuleHealthState

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

#: Content-level failures are NOT retried at transport level: the agent-layer
#: repair loop (P06) owns them, and retrying garbage burns tokens for nothing.
NO_TRANSPORT_RETRY_CODES = frozenset({"LLM_003", "LLM_004", "OCR_002"})


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded exponential backoff.

    ``max_attempts`` counts TOTAL attempts (1 = no retry). Delay for attempt
    ``n`` (1-based, the delay BEFORE attempt n+1) is::

        min(max_delay_s, base_delay_s * multiplier ** (n - 1)) * (1 ± jitter)
    """

    max_attempts: int = 3
    base_delay_s: float = 1.0
    multiplier: float = 2.0
    max_delay_s: float = 30.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("delays must be >= 0")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be in [0, 1]")

    def delay_for(self, attempt: int, rng: random.Random | None = None) -> float:
        delay = min(self.max_delay_s, self.base_delay_s * self.multiplier ** (attempt - 1))
        if self.jitter_ratio and rng is not None:
            jitter = delay * self.jitter_ratio
            delay += rng.uniform(-jitter, jitter)
        return max(0.0, delay)


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Failure-count breaker with recovery window.

    - CLOSED: calls flow; consecutive classified failures open the circuit at
      ``failure_threshold``;
    - OPEN: calls are rejected fast with PROVIDER_003 (retryable per catalog,
      but NOT retried internally — waiting out the recovery window inside a
      call would stall the pipeline);
    - HALF_OPEN: after ``recovery_timeout_s``, up to ``half_open_max_calls``
      probe calls may pass; success closes the circuit, failure re-opens it.
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout_s: float = 60.0,
        half_open_max_calls: int = 1,
        clock: Clock = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout_s <= 0:
            raise ValueError("recovery_timeout_s must be > 0")
        if half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_s = recovery_timeout_s
        self.half_open_max_calls = half_open_max_calls
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._half_open_inflight = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.recovery_timeout_s:
                self._state = CircuitState.HALF_OPEN
                self._half_open_inflight = 0
        return self._state

    async def acquire(self) -> None:
        """Reserve permission for one call.

        Raises:
            DomainError: PROVIDER_003 when the circuit is open or the
                half-open probe budget is exhausted.
        """
        async with self._lock:
            state = self.state
            if state is CircuitState.OPEN:
                raise DomainError(
                    "PROVIDER_003",
                    message="Provider circuit breaker is open; calls are rejected until recovery.",
                    details={
                        "recovery_in_s": round(
                            max(
                                0.0,
                                self.recovery_timeout_s
                                - (self._clock() - (self._opened_at or 0.0)),
                            ),
                            3,
                        ),
                    },
                )
            if state is CircuitState.HALF_OPEN:
                if self._half_open_inflight >= self.half_open_max_calls:
                    raise DomainError(
                        "PROVIDER_003",
                        message="Circuit half-open probe budget exhausted.",
                        details={},
                    )
                self._half_open_inflight += 1

    def release(self) -> None:
        """Return an unused probe without treating cancellation as success."""
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_inflight = max(0, self._half_open_inflight - 1)

    def record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_inflight = max(0, self._half_open_inflight - 1)
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            self._half_open_inflight = max(0, self._half_open_inflight - 1)
            self._trip()
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._consecutive_failures = 0


# ---------------------------------------------------------------------------
# Concurrency ceiling
# ---------------------------------------------------------------------------


class ConcurrencyLimiter:
    """asyncio semaphore wrapper exposing configured/active concurrency."""

    def __init__(self, max_concurrent: int) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.configured = max_concurrent
        self._semaphores: WeakKeyDictionary = WeakKeyDictionary()
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    class _Slot:
        def __init__(self, limiter: ConcurrencyLimiter) -> None:
            self._limiter = limiter

        async def __aenter__(self) -> None:
            loop = asyncio.get_running_loop()
            self._semaphore = self._limiter._semaphores.setdefault(
                loop, asyncio.Semaphore(self._limiter.configured)
            )
            await self._semaphore.acquire()
            self._limiter._active += 1

        async def __aexit__(self, *exc_info: Any) -> None:
            self._limiter._active -= 1
            self._semaphore.release()

    def slot(self) -> _Slot:
        return ConcurrencyLimiter._Slot(self)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ProviderStats:
    """Counters/latencies feeding doc 04 §6 provider status exposures.

    Counter semantics are PER ATTEMPT: one logical call that fails twice and
    then succeeds records total_calls=3, transport_errors=2,
    successful_calls=1. transport_error_rate is therefore attempt-based.
    """

    latency_window: int = 200
    observer: Any = None

    def observe(self, kind, **values):
        if self.observer is not None:
            self.observer(kind, **values)

    _latencies: deque[float] = field(default_factory=lambda: deque(maxlen=200), init=False)
    total_calls: int = 0
    successful_calls: int = 0
    transport_errors: int = 0
    rate_limit_events: int = 0
    json_valid: int = 0
    json_invalid: int = 0
    schema_pass: int = 0
    schema_fail: int = 0
    citation_pass: int = 0
    citation_fail: int = 0
    unsupported_claims: int = 0
    validated_responses: int = 0
    invalid_responses: int = 0

    def __post_init__(self) -> None:
        self._latencies = deque(maxlen=self.latency_window)

    def record_latency(self, latency_ms: float) -> None:
        self._latencies.append(latency_ms)
        self.observe("latency", value=latency_ms)

    def record_success(self) -> None:
        self.observe("attempt", status="OK")
        self.total_calls += 1
        self.successful_calls += 1

    def record_failure(self, classification: FailureClassification) -> None:
        self.observe("attempt", status=classification.error.code)
        self.total_calls += 1
        if classification.stats_hook == "rate_limit":
            self.rate_limit_events += 1
        if classification.stats_hook in {"transport", "rate_limit"}:
            self.transport_errors += 1

    def record_json_parse(self, valid: bool) -> None:
        if valid:
            self.json_valid += 1
        else:
            self.json_invalid += 1

    def record_schema(self, passed: bool) -> None:
        self.observe("schema", success=passed)
        if passed:
            self.schema_pass += 1
        else:
            self.schema_fail += 1

    def record_citation(self, passed: bool) -> None:
        self.observe("citation", success=passed)
        if passed:
            self.citation_pass += 1
        else:
            self.citation_fail += 1

    def record_unsupported_claim(self) -> None:
        self.observe("unsupported", value=1)
        self.unsupported_claims += 1

    def record_response_validation(self, valid: bool) -> None:
        if valid:
            self.validated_responses += 1
        else:
            self.invalid_responses += 1

    def _percentile(self, q: float) -> float | None:
        if not self._latencies:
            return None
        ordered = sorted(self._latencies)
        idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
        return round(ordered[idx], 3)

    @property
    def latency_p50(self) -> float | None:
        return self._percentile(0.5)

    @property
    def latency_p90(self) -> float | None:
        return self._percentile(0.9)

    @property
    def latency_mean(self) -> float | None:
        if not self._latencies:
            return None
        return round(statistics.fmean(self._latencies), 3)

    @property
    def transport_error_rate(self) -> float | None:
        if self.total_calls == 0:
            return None
        return round(self.transport_errors / self.total_calls, 4)

    @property
    def valid_json_rate(self) -> float | None:
        total = self.json_valid + self.json_invalid
        if total == 0:
            return None
        return round(self.json_valid / total, 4)

    @property
    def schema_pass_rate(self) -> float | None:
        total = self.schema_pass + self.schema_fail
        if total == 0:
            return None
        return round(self.schema_pass / total, 4)

    @property
    def citation_pass_rate(self) -> float | None:
        total = self.citation_pass + self.citation_fail
        if total == 0:
            return None
        return round(self.citation_pass / total, 4)

    @property
    def unsupported_claim_rate(self) -> float | None:
        total = self.schema_pass + self.schema_fail
        if total == 0:
            return None
        return round(min(1.0, self.unsupported_claims / max(1, total)), 4)

    @property
    def response_validation_rate(self) -> float | None:
        total = self.validated_responses + self.invalid_responses
        if total == 0:
            return None
        return round(self.validated_responses / total, 4)


# ---------------------------------------------------------------------------
# Failure classification + resilient execution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureClassification:
    """How one exception maps onto the error catalog and the retry loop."""

    error: DomainError
    retry: bool
    retry_after_s: float | None = None
    counts_toward_breaker: bool = True
    #: "rate_limit" | "transport" | None — which stats counter to bump.
    stats_hook: str | None = None


Classifier = Callable[[Exception], FailureClassification]


def classify_domain_error(exc: Exception) -> FailureClassification:
    """Default classifier: DomainErrors keep their catalog retryability
    except content-level codes, which the agent repair loop owns."""
    if isinstance(exc, DomainError):
        code = exc.code
        if code in NO_TRANSPORT_RETRY_CODES:
            return FailureClassification(error=exc, retry=False)
        hook = "rate_limit" if code in {"LLM_002", "PROVIDER_002"} else "transport"
        return FailureClassification(error=exc, retry=exc.retryable, stats_hook=hook)
    return FailureClassification(
        error=DomainError(
            "PROVIDER_001",
            message="Unexpected provider failure.",
            details={"exception_type": type(exc).__name__},
        ),
        retry=False,
        counts_toward_breaker=True,
        stats_hook="transport",
    )


async def run_with_resilience(
    operation: Callable[[], Awaitable[Any]],
    *,
    retry: RetryPolicy,
    breaker: CircuitBreaker,
    limiter: ConcurrencyLimiter,
    classify: Classifier = classify_domain_error,
    stats: ProviderStats,
    sleep: Sleeper = asyncio.sleep,
    rng: random.Random | None = None,
) -> Any:
    """Execute one provider operation under semaphore + breaker + bounded retry.

    Raises the classified DomainError when retries are exhausted or the
    failure is non-retryable; PROVIDER_003 immediately when the breaker is
    open. Exceptions are never swallowed.
    """
    rng = rng or random.Random()
    last_error: DomainError | None = None
    for attempt in range(1, retry.max_attempts + 1):
        await breaker.acquire()
        started = time.monotonic()
        try:
            async with limiter.slot():
                result = await operation()
        except asyncio.CancelledError:
            breaker.release()
            raise
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            failure = classify(exc)
            stats.record_latency((time.monotonic() - started) * 1000.0)
            stats.record_failure(failure)
            if failure.counts_toward_breaker:
                breaker.record_failure()
            else:
                breaker.release()
            last_error = failure.error
            if not failure.retry or attempt >= retry.max_attempts:
                raise failure.error from exc
            delay = (
                failure.retry_after_s
                if failure.retry_after_s is not None
                else retry.delay_for(attempt, rng)
            )
            await sleep(delay)
            continue
        breaker.record_success()
        stats.record_latency((time.monotonic() - started) * 1000.0)
        stats.record_success()
        return result
    # Unreachable: the loop either returns or raises.
    raise last_error or DomainError("PROVIDER_001", message="Retry loop exited unexpectedly.")


def availability_from(
    breaker_state: CircuitState,
    transport_error_rate: float | None,
    has_calls: bool,
    canary_state: CanaryState = CanaryState.UNKNOWN,
) -> ModuleHealthState:
    """Map breaker/error stats onto the frozen availability states.

    A reachable provider is not automatically healthy (doc 00 §7.14): an open
    breaker means UNAVAILABLE, half-open or a high recent transport error
    rate means DEGRADED.
    """
    if breaker_state is CircuitState.OPEN:
        return ModuleHealthState.UNAVAILABLE
    if canary_state in (CanaryState.FAILED, CanaryState.STALE):
        return ModuleHealthState.DEGRADED
    if not has_calls:
        return ModuleHealthState.UNKNOWN
    if breaker_state is CircuitState.HALF_OPEN:
        return ModuleHealthState.DEGRADED
    if transport_error_rate is not None and transport_error_rate >= 0.5:
        return ModuleHealthState.DEGRADED
    return ModuleHealthState.HEALTHY


@dataclass(slots=True)
class CanaryRecord:
    """Last canary outcome per provider (doc 04 §6 canary_state)."""

    state: CanaryState = CanaryState.UNKNOWN
    detail: str = ""
    latency_ms: int = 0
    checked_at: float | None = None  # clock() timestamp

    def effective_state(self, clock: Clock, ttl_s: float) -> CanaryState:
        if self.state is CanaryState.OK and self.checked_at is not None:
            if clock() - self.checked_at > ttl_s:
                return CanaryState.STALE
        return self.state


__all__ = [
    "NO_TRANSPORT_RETRY_CODES",
    "CanaryRecord",
    "CircuitBreaker",
    "ConcurrencyLimiter",
    "FailureClassification",
    "ProviderStats",
    "RetryPolicy",
    "availability_from",
    "classify_domain_error",
    "run_with_resilience",
]

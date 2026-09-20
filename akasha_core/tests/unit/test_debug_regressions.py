"""Behavioral regressions discovered while checking the delivery review."""

import asyncio

import pymupdf
import pytest
from tests.unit.test_agents_firewall import _claim, _payload, _resolve_ok

from paperintel.agents.firewall import semantic_validation
from paperintel.errors import DomainError
from paperintel.extraction.pdf_document import render_clip_png, render_page_png
from paperintel.operations.debug import redact
from paperintel.providers.resilience import (
    CircuitBreaker,
    ConcurrencyLimiter,
    ProviderStats,
    RetryPolicy,
    run_with_resilience,
)
from paperintel.schemas.agent import AgentResult
from paperintel.schemas.enums import CircuitState


@pytest.mark.parametrize("claim_type", ["INFERENCE", "CRITIQUE", "EXTERNAL"])
def test_unanchored_claim_dropped_without_losing_valid_claim(claim_type):
    result = AgentResult.model_validate(_payload(claims=[
        _claim(), _claim(statement="No cited evidence for this candidate.", claim_type=claim_type, evidence=[]),
    ]))
    kept, warnings = semantic_validation(result, resolve_evidence=_resolve_ok)
    assert len(kept.claims) == 1
    assert kept.claims[0].statement == "The dataset contains 1,000 samples."
    assert any(w.startswith("CLAIM_001") for w in warnings)


def test_fact_without_support_is_already_rejected_by_schema():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="SUPPORT evidence"):
        AgentResult.model_validate(_payload(claims=[_claim(evidence=[])]))


def test_cancelled_half_open_probe_returns_capacity():
    async def scenario():
        now = [0.0]
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout_s=1, clock=lambda: now[0])
        breaker.record_failure()
        now[0] = 2.0
        assert breaker.state is CircuitState.HALF_OPEN

        async def cancelled():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await run_with_resilience(cancelled, retry=RetryPolicy(max_attempts=1), breaker=breaker,
                                      limiter=ConcurrencyLimiter(1), stats=ProviderStats())
        assert breaker.state is CircuitState.HALF_OPEN
        await breaker.acquire()
        breaker.record_success()
        assert breaker.state is CircuitState.CLOSED
    asyncio.run(scenario())


@pytest.mark.parametrize("crop", [False, True])
def test_oversize_raster_rejected_before_allocation(crop):
    with pymupdf.open() as doc:
        doc.new_page(width=10000, height=10000)
        with pytest.raises(DomainError, match="25 megapixel"):
            if crop:
                render_clip_png(doc, 1, (0, 0, 10000, 10000))
            else:
                render_page_png(doc, 1)


def test_composite_credentials_redacted_without_hiding_usage_counts():
    assert redact({"client_secret": "value", "nested": {"openai_api_key": "value"}, "tokens_used": 42}) == {
        "client_secret": "[REDACTED]", "nested": {"openai_api_key": "[REDACTED]"}, "tokens_used": 42,
    }

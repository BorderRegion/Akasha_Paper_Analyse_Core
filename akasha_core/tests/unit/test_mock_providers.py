"""Mock provider framework tests (P00 gate check P00-C15).

Deterministic modes from spec doc 06 §3 must be reproducible and must produce
exactly the failure classes the schema firewall will be tested against.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from pydantic import ValidationError

from paperintel.errors import DomainError
from paperintel.providers.base import ChatMessage, LlmRequest, OcrPageRequest
from paperintel.providers.mocks import (
    CANARY_EVIDENCE_ID,
    MockLLMProvider,
    MockOCRProvider,
)
from paperintel.providers.registry import ProviderRegistry
from paperintel.schemas.agent import AgentResult
from paperintel.schemas.enums import LlmMockMode, OcrMockMode, ProviderFamily

IMAGE = b"deterministic-page-render"
IMAGE_SHA = hashlib.sha256(IMAGE).hexdigest()


def _request() -> LlmRequest:
    return LlmRequest(
        messages=[
            ChatMessage(role="system", content="You are a paper analyst."),
            ChatMessage(role="user", content="Analyze the evidence."),
        ]
    )


def _ocr_request(sha: str = IMAGE_SHA) -> OcrPageRequest:
    return OcrPageRequest(image_sha256=sha, page_number=1)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# LLM mock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(LlmMockMode))
def test_llm_modes_are_deterministic(mode: LlmMockMode) -> None:
    async def call() -> str | None:
        provider = MockLLMProvider(mode=mode, seed=7)
        try:
            response = await provider.complete(_request())
            return response.content
        except DomainError as exc:
            return f"error:{exc.code}"

    first = _run(call())
    second = _run(call())
    assert first == second
    assert first is not None


def test_llm_valid_mode_produces_schema_valid_agent_result() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.VALID)
    response = _run(provider.complete(_request()))
    payload = json.loads(response.content)
    result = AgentResult.model_validate(payload)
    assert result.status.value == "SUCCESS"
    assert len(result.claims) == 2
    assert all(claim.claim_type.value == "FACT" for claim in result.claims)
    assert response.usage.input_tokens is not None and response.usage.input_tokens >= 0
    assert provider.calls[0].error_code is None


def test_llm_non_json_mode_is_not_json() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.NON_JSON)
    response = _run(provider.complete(_request()))
    with pytest.raises(json.JSONDecodeError):
        json.loads(response.content)


def test_llm_missing_required_field_fails_schema() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.MISSING_REQUIRED_FIELD)
    response = _run(provider.complete(_request()))
    payload = json.loads(response.content)  # parses fine...
    with pytest.raises(ValidationError):  # ...but violates the schema
        AgentResult.model_validate(payload)


def test_llm_unknown_and_wrong_paper_evidence_pass_shape_layer() -> None:
    """The schema layer only prefix-checks evidence IDs; existence/scope are
    enforced downstream (spec doc 02 §6). Mocks must exercise that split."""
    for mode in (LlmMockMode.UNKNOWN_EVIDENCE, LlmMockMode.WRONG_PAPER_EVIDENCE):
        provider = MockLLMProvider(mode=mode)
        response = _run(provider.complete(_request()))
        result = AgentResult.model_validate(json.loads(response.content))
        assert result.claims[0].evidence[0].evidence_id != CANARY_EVIDENCE_ID


def test_llm_invented_number_mode_adds_unsupported_fact() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.INVENTED_NUMBER)
    response = _run(provider.complete(_request()))
    result = AgentResult.model_validate(json.loads(response.content))
    statements = [claim.statement for claim in result.claims]
    assert any("97.3%" in statement for statement in statements)
    assert any("82.5%" in statement for statement in statements)


def test_llm_duplicate_claims_mode_repeats_claim() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.DUPLICATE_CLAIMS)
    response = _run(provider.complete(_request()))
    result = AgentResult.model_validate(json.loads(response.content))
    first = result.claims[0].model_dump(mode="json")
    duplicates = sum(1 for claim in result.claims if claim.model_dump(mode="json") == first)
    assert duplicates >= 3


def test_llm_tag_spam_mode_floods_observations() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.TAG_SPAM)
    response = _run(provider.complete(_request()))
    result = AgentResult.model_validate(json.loads(response.content))
    assert len(result.observations) == 500


def test_llm_refusal_mode_is_prose() -> None:
    provider = MockLLMProvider(mode=LlmMockMode.REFUSAL)
    response = _run(provider.complete(_request()))
    assert "cannot assist" in response.content
    with pytest.raises(json.JSONDecodeError):
        json.loads(response.content)


def test_llm_transport_modes_raise_retryable_domain_errors() -> None:
    expectations = {
        LlmMockMode.TIMEOUT: "LLM_001",
        LlmMockMode.RATE_LIMIT: "LLM_002",
        LlmMockMode.SERVER_ERROR: "PROVIDER_001",
    }
    for mode, code in expectations.items():
        provider = MockLLMProvider(mode=mode)
        with pytest.raises(DomainError) as excinfo:
            _run(provider.complete(_request()))
        assert excinfo.value.code == code
        assert excinfo.value.retryable is True
        assert provider.calls[-1].error_code == code


# ---------------------------------------------------------------------------
# OCR mock
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(OcrMockMode))
def test_ocr_modes_are_deterministic(mode: OcrMockMode) -> None:
    async def call() -> str:
        provider = MockOCRProvider(mode=mode, seed=11)
        try:
            result = await provider.recognize_page(IMAGE, _ocr_request())
            return json.dumps(result.model_dump(mode="json"), sort_keys=True, default=str)
        except DomainError as exc:
            return f"error:{exc.code}"

    assert _run(call()) == _run(call())


def test_ocr_valid_mode_returns_truth_lines() -> None:
    provider = MockOCRProvider(mode=OcrMockMode.VALID)
    result = _run(provider.recognize_page(IMAGE, _ocr_request()))
    assert "1,000 samples" in result.full_text
    assert "82.5% accuracy" in result.full_text
    assert "2.1 percentage points" in result.full_text
    assert result.mean_confidence is not None and result.mean_confidence > 0.9
    assert all(line.bbox is not None for line in result.lines)


def test_ocr_low_confidence_mode_warns() -> None:
    provider = MockOCRProvider(mode=OcrMockMode.LOW_CONFIDENCE)
    result = _run(provider.recognize_page(IMAGE, _ocr_request()))
    assert result.mean_confidence is not None and result.mean_confidence < 0.5
    assert any("OCR_003" in warning for warning in result.warnings)


def test_ocr_digit_corruption_mode_corrupts_numbers() -> None:
    provider = MockOCRProvider(mode=OcrMockMode.DIGIT_CORRUPTION)
    result = _run(provider.recognize_page(IMAGE, _ocr_request()))
    assert "82.5%" not in result.full_text
    assert "825%" in result.full_text
    assert "1.000 samples" in result.full_text


def test_ocr_empty_mode() -> None:
    provider = MockOCRProvider(mode=OcrMockMode.EMPTY)
    result = _run(provider.recognize_page(IMAGE, _ocr_request()))
    assert result.lines == []
    assert result.full_text == ""
    assert result.mean_confidence is None


def test_ocr_transport_modes_raise() -> None:
    timeout = MockOCRProvider(mode=OcrMockMode.TIMEOUT)
    with pytest.raises(DomainError) as excinfo:
        _run(timeout.recognize_page(IMAGE, _ocr_request()))
    assert excinfo.value.code == "OCR_001"
    assert excinfo.value.retryable is True

    malformed = MockOCRProvider(mode=OcrMockMode.MALFORMED_RESPONSE)
    with pytest.raises(DomainError) as excinfo:
        _run(malformed.recognize_page(IMAGE, _ocr_request()))
    assert excinfo.value.code == "OCR_002"


def test_ocr_sha_mismatch_rejected_not_silently_accepted() -> None:
    provider = MockOCRProvider(mode=OcrMockMode.VALID)
    with pytest.raises(DomainError) as excinfo:
        _run(provider.recognize_page(IMAGE, _ocr_request(sha="0" * 64)))
    assert excinfo.value.code == "OCR_002"


def test_provider_health_records() -> None:
    llm = MockLLMProvider()
    ocr = MockOCRProvider()
    llm_health = _run(llm.health())
    ocr_health = _run(ocr.health())
    assert llm_health.state.value == "HEALTHY"
    assert ocr_health.state.value == "HEALTHY"
    assert llm_health.module_id.startswith("providers.llm.mock.")
    assert ocr_health.module_id.startswith("providers.ocr.mock.")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_register_and_resolve() -> None:
    registry = ProviderRegistry()
    llm = MockLLMProvider(provider_id="prv_mock01")
    ocr = MockOCRProvider(provider_id="prv_mock02")
    registry.register("llm_primary", llm)
    registry.register("ocr_primary", ocr)
    assert registry.get("llm_primary") is llm
    assert registry.get_ocr("ocr_primary") is ocr
    assert registry.names() == ["llm_primary", "ocr_primary"]
    assert set(registry.by_family(llm.family)) == {"llm_primary"}


def test_registry_duplicate_and_unknown() -> None:
    registry = ProviderRegistry()
    registry.register("llm_primary", MockLLMProvider())
    with pytest.raises(DomainError) as excinfo:
        registry.register("llm_primary", MockLLMProvider())
    assert excinfo.value.code == "CFG_002"
    with pytest.raises(DomainError) as excinfo:
        registry.get("absent")
    assert excinfo.value.code == "PROVIDER_001"


class _WrongFamily(MockLLMProvider):
    """LLM implementation claiming to be an OCR provider."""

    family = ProviderFamily.OCR


def test_registry_family_mismatch_rejected() -> None:
    registry = ProviderRegistry()
    with pytest.raises(DomainError) as excinfo:
        registry.register("bad", _WrongFamily())
    assert excinfo.value.code == "PROVIDER_001"

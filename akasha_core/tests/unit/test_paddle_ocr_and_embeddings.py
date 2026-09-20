"""Paddle OCR HTTP provider + embedding provider tests (offline)."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest
from tests.unit.provider_helpers import (
    FakeClock,
    RecordingSleeper,
    SequenceHandler,
    chat_response,
    embeddings_response,
    make_transport,
)

from paperintel.errors import DomainError
from paperintel.providers.base import OcrPageRequest
from paperintel.providers.http_embeddings import OpenAICompatibleEmbeddingProvider
from paperintel.providers.paddle_ocr import (
    OCR_PROMPT_SHA256,
    PaddleOCRHttpProvider,
    render_canary_image,
)
from paperintel.providers.resilience import RetryPolicy
from paperintel.schemas.enums import CanaryState

IMAGE = b"\x89PNG fake page image bytes"
IMAGE_SHA = "9f2e0d4c8b1a3f6e5d7c9b0a2f4e6d8c1b3a5f7e9d0c2b4a6f8e1d3c5b7a9f0e"


def page_request() -> OcrPageRequest:
    return OcrPageRequest(image_sha256=IMAGE_SHA, page_number=3, mime_type="image/png")


def make_ocr(handler, **kwargs) -> PaddleOCRHttpProvider:
    clock = kwargs.pop("clock", FakeClock())
    sleep = kwargs.pop("sleep", RecordingSleeper(clock))
    defaults = {
        "base_url": "https://ocr.example.invalid/v1",
        "model": "PaddleOCR-VL-TEST",
        "api_key": "sk-ocr-secret-value",
        "timeout_seconds": 5.0,
        "max_concurrency": 2,
        "retry": RetryPolicy(max_attempts=2, base_delay_s=0.01, jitter_ratio=0.0),
        "transport": make_transport(handler),
        "clock": clock,
        "sleep": sleep,
    }
    defaults.update(kwargs)
    return PaddleOCRHttpProvider("prv_test_ocr", **defaults)


def lines_payload(lines: list[dict]) -> httpx.Response:
    return chat_response(json.dumps(lines), model="PaddleOCR-VL-TEST")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# OCR: happy paths
# ---------------------------------------------------------------------------


def test_ocr_parses_lines_confidence_bbox() -> None:
    handler = SequenceHandler(
        lines_payload(
            [
                {
                    "text": "We evaluate on 1,000 samples.",
                    "confidence": 0.97,
                    "bbox": [10.0, 20.0, 300.0, 40.0],
                },
                {"text": "Method A reaches 82.5% accuracy.", "confidence": 0.91},
            ]
        )
    )
    provider = make_ocr(handler)
    result = run(provider.recognize_page(IMAGE, page_request()))
    assert len(result.lines) == 2
    assert result.lines[0].text == "We evaluate on 1,000 samples."
    assert result.lines[0].confidence == 0.97
    assert result.lines[0].bbox is not None
    assert result.lines[1].bbox is None
    assert result.full_text == "We evaluate on 1,000 samples.\nMethod A reaches 82.5% accuracy."
    assert result.mean_confidence == pytest.approx(0.94, abs=0.001)
    assert result.warnings == []
    assert result.provider_id == "prv_test_ocr"
    # The prompt contract is versioned and pinned by hash.
    sent = json.loads(handler.requests[0].content)
    assert sent["model"] == "PaddleOCR-VL-TEST"
    text_part = sent["messages"][0]["content"][0]["text"]
    import hashlib

    assert hashlib.sha256(text_part.encode()).hexdigest() == OCR_PROMPT_SHA256
    image_part = sent["messages"][0]["content"][1]["image_url"]["url"]
    assert image_part.startswith("data:image/png;base64,")
    assert base64.b64decode(image_part.split(",", 1)[1]) == IMAGE


def test_ocr_accepts_markdown_fenced_json_and_lines_object() -> None:
    fenced = "```json\n" + json.dumps({"lines": [{"text": "hello", "confidence": 0.9}]}) + "\n```"
    handler = SequenceHandler(chat_response(fenced))
    provider = make_ocr(handler)
    result = run(provider.recognize_page(IMAGE, page_request()))
    assert result.lines[0].text == "hello"


def test_ocr_confidence_clamped_to_unit_interval() -> None:
    handler = SequenceHandler(
        lines_payload([{"text": "x", "confidence": 1.7}, {"text": "y", "confidence": -0.2}])
    )
    provider = make_ocr(handler)
    result = run(provider.recognize_page(IMAGE, page_request()))
    assert [line.confidence for line in result.lines] == [1.0, 0.0]


def test_ocr_low_confidence_flags_ocr003_warning() -> None:
    handler = SequenceHandler(lines_payload([{"text": "blurry text", "confidence": 0.42}]))
    provider = make_ocr(handler)
    result = run(provider.recognize_page(IMAGE, page_request()))
    assert result.mean_confidence == pytest.approx(0.42)
    assert any(w.startswith("OCR_003") for w in result.warnings)
    # Warning, not exception: degraded data flows on with its quality state.


# ---------------------------------------------------------------------------
# OCR: failure modes
# ---------------------------------------------------------------------------


def test_ocr_malformed_content_maps_ocr002_with_bounded_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return chat_response("Sorry, I cannot OCR this image as JSON.")

    provider = make_ocr(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.recognize_page(IMAGE, page_request()))
    assert excinfo.value.code == "OCR_002"
    assert calls["n"] == 2  # retried once within the bounded budget
    assert provider.stats.invalid_responses == 2


def test_ocr_missing_confidence_never_invented() -> None:
    handler = SequenceHandler(lines_payload([{"text": "no confidence given"}]))
    provider = make_ocr(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.recognize_page(IMAGE, page_request()))
    assert excinfo.value.code == "OCR_002"
    assert "confidence" in excinfo.value.message


def test_ocr_invalid_bbox_maps_ocr002() -> None:
    handler = SequenceHandler(
        lines_payload([{"text": "x", "confidence": 0.9, "bbox": [1, 2, 3]}])  # 3 coords
    )
    provider = make_ocr(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.recognize_page(IMAGE, page_request()))
    assert excinfo.value.code == "OCR_002"


def test_ocr_timeout_maps_ocr001() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow scanner", request=request)

    provider = make_ocr(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.recognize_page(IMAGE, page_request()))
    assert excinfo.value.code == "OCR_001"


def test_ocr_429_maps_provider002() -> None:
    handler = SequenceHandler(httpx.Response(429, content=b"quota"))
    provider = make_ocr(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.recognize_page(IMAGE, page_request()))
    assert excinfo.value.code == "PROVIDER_002"
    assert provider.stats.rate_limit_events == 2


def test_ocr_empty_image_rejected_client_side() -> None:
    handler = SequenceHandler(lines_payload([]))
    provider = make_ocr(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.recognize_page(b"", page_request()))
    assert excinfo.value.code == "OCR_002"
    assert handler.call_count == 0  # never hit the network


def test_ocr_empty_page_is_valid_result() -> None:
    handler = SequenceHandler(lines_payload([]))
    provider = make_ocr(handler)
    result = run(provider.recognize_page(IMAGE, page_request()))
    assert result.lines == []
    assert result.mean_confidence is None
    assert result.warnings == []


# ---------------------------------------------------------------------------
# OCR: canary
# ---------------------------------------------------------------------------


def test_ocr_canary_recognizes_synthetic_image() -> None:
    class Recorder:
        def __init__(self) -> None:
            self.requests: list[httpx.Request] = []

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return lines_payload([{"text": "PaperIntel OCR canary", "confidence": 0.99}])

    handler = Recorder()
    provider = make_ocr(handler)
    record = run(provider.run_canary())
    assert record.state is CanaryState.OK
    # The canary image is a real rendered PNG.
    sent = json.loads(handler.requests[0].content)
    image_part = sent["messages"][0]["content"][1]["image_url"]["url"]
    png = base64.b64decode(image_part.split(",", 1)[1])
    assert png.startswith(b"\x89PNG")


def test_ocr_canary_failed_when_text_missing() -> None:
    handler = SequenceHandler(lines_payload([{"text": "garbage", "confidence": 0.5}]))
    provider = make_ocr(handler)
    record = run(provider.run_canary())
    assert record.state is CanaryState.FAILED


def test_render_canary_image_is_png() -> None:
    png = render_canary_image("hello world")
    assert png.startswith(b"\x89PNG")
    assert len(png) > 500


# ---------------------------------------------------------------------------
# OCR: plain_text response mode (native transcription backends)
# ---------------------------------------------------------------------------


def test_ocr_plain_text_mode_image_only_request() -> None:
    handler = SequenceHandler(chat_response("First line.\n\nSecond line."))
    provider = make_ocr(handler, response_mode="plain_text")
    result = run(provider.recognize_page(IMAGE, page_request()))
    # Payload carries the image ONLY — no chat-style instructions that make
    # native transcription models degenerate.
    sent = json.loads(handler.requests[0].content)
    parts = sent["messages"][0]["content"]
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"
    assert sent["max_tokens"] == provider.max_output_tokens
    # Raw text → one line per non-empty stripped line.
    assert [line.text for line in result.lines] == ["First line.", "Second line."]
    # Confidence is NEVER invented for backends that do not report it.
    assert all(line.confidence is None for line in result.lines)
    assert result.mean_confidence is None
    assert any(w.startswith("OCR_CONFIDENCE_UNAVAILABLE") for w in result.warnings)


def test_ocr_json_lines_payload_sets_max_tokens() -> None:
    handler = SequenceHandler(lines_payload([{"text": "x", "confidence": 0.9}]))
    provider = make_ocr(handler, max_output_tokens=1234)
    run(provider.recognize_page(IMAGE, page_request()))
    sent = json.loads(handler.requests[0].content)
    assert sent["max_tokens"] == 1234


def test_ocr_plain_text_canary() -> None:
    handler = SequenceHandler(chat_response("PaperIntel OCR canary"))
    provider = make_ocr(handler, response_mode="plain_text")
    record = run(provider.run_canary())
    assert record.state is CanaryState.OK


def test_ocr_invalid_response_mode_rejected() -> None:
    with pytest.raises(ValueError, match="response_mode"):
        make_ocr(SequenceHandler(lines_payload([])), response_mode="magic")


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------


def make_embed(handler, **kwargs) -> OpenAICompatibleEmbeddingProvider:
    defaults = {
        "base_url": "https://embed.example.invalid/v1",
        "model": "Qwen3-Embedding-TEST",
        "api_key": "sk-embed-secret",
        "timeout_seconds": 5.0,
        "retry": RetryPolicy(max_attempts=2, base_delay_s=0.01, jitter_ratio=0.0),
        "transport": make_transport(handler),
        "clock": FakeClock(),
        "sleep": RecordingSleeper(),
    }
    defaults.update(kwargs)
    return OpenAICompatibleEmbeddingProvider("prv_test_embed", **defaults)


def test_embeddings_roundtrip() -> None:
    handler = SequenceHandler(embeddings_response([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]))
    provider = make_embed(handler, expected_dimensions=3)
    result = run(provider.embed(["alpha", "beta"]))
    assert result.vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    assert result.dimensions == 3
    assert result.model_id == "embed-model"
    sent = json.loads(handler.requests[0].content)
    assert sent["input"] == ["alpha", "beta"]


def test_embeddings_empty_input_raises() -> None:
    provider = make_embed(SequenceHandler(embeddings_response([])))
    with pytest.raises(DomainError) as excinfo:
        run(provider.embed([]))
    assert excinfo.value.code == "EMBED_001"


def test_embeddings_blank_string_raises() -> None:
    provider = make_embed(SequenceHandler(embeddings_response([[0.1]])))
    with pytest.raises(DomainError):
        run(provider.embed(["   "]))


def test_embeddings_dimension_mismatch_rejected() -> None:
    handler = SequenceHandler(embeddings_response([[0.1, 0.2]]))
    provider = make_embed(handler, expected_dimensions=8)
    with pytest.raises(DomainError) as excinfo:
        run(provider.embed(["x"]))
    assert excinfo.value.code == "EMBED_001"
    assert "poison" in excinfo.value.message  # explicit refusal, not silent


def test_embeddings_inconsistent_batch_dimensions_rejected() -> None:
    handler = SequenceHandler(embeddings_response([[0.1, 0.2], [0.3]]))
    provider = make_embed(handler)
    with pytest.raises(DomainError):
        run(provider.embed(["x", "y"]))


def test_embeddings_count_mismatch_rejected() -> None:
    handler = SequenceHandler(embeddings_response([[0.1]]))
    provider = make_embed(handler)
    with pytest.raises(DomainError) as excinfo:
        run(provider.embed(["x", "y"]))
    assert "Expected 2 embeddings" in excinfo.value.message


def test_embeddings_canary() -> None:
    handler = SequenceHandler(embeddings_response([[0.5] * 4]))
    provider = make_embed(handler)
    record = run(provider.run_canary())
    assert record.state is CanaryState.OK
    assert "dim=4" in record.detail

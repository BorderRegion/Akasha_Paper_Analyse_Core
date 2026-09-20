"""Paddle OCR HTTP provider adapter (spec doc 05 P02).

Production OCR is a Paddle OCR HTTP service; in this deployment the adapter
targets the OpenAI-compatible vision chat endpoint of the gateway
(e.g. ``PaddleOCR-VL-1.5``), which is how PaddleOCR-VL is served here. The
adapter is strict: a response whose lines lack text/confidence is INVALID
(OCR_002) — confidences are never invented (no silent fallbacks).

Prompt contract is versioned; its sha256 is exposed for reproducibility.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from typing import Any

import httpx

from paperintel.errors import DomainError
from paperintel.providers.base import OcrLine, OcrPageRequest, OcrPageResult, OCRProvider
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
from paperintel.schemas.common import BBox
from paperintel.schemas.enums import CanaryState
from paperintel.schemas.health import ModuleHealthRecord, OcrProviderStatus

_CHAT_PATH = "/chat/completions"

OCR_PROMPT_VERSION = "paddle-ocr-json-lines-v1"
OCR_PROMPT = (
    "You are an OCR engine. Transcribe every text line visible in this page image.\n"
    "Respond with ONLY a JSON array, no prose, no markdown fences. Each element:\n"
    '{"text": "<line text>", "confidence": <float 0..1>, '
    '"bbox": [x0, y0, x1, y1]}\n'
    "bbox uses image pixel coordinates, origin top-left. Include every line in "
    "reading order. If the page has no text, respond with []."
)
OCR_PROMPT_SHA256 = hashlib.sha256(OCR_PROMPT.encode()).hexdigest()

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)

#: Page mean confidence below this threshold is flagged (OCR_003 warning),
#: matching the data-quality policy for degraded OCR evidence.
DEFAULT_LOW_CONFIDENCE_THRESHOLD = 0.6

#: Response modes:
#: - ``json_lines``: instruction-following VL models return the contracted
#:   JSON array of {text, confidence, bbox} (full evidence-grade output);
#: - ``plain_text``: native transcription backends (e.g. PaddleOCR-VL served
#:   via chat-completions) return raw text; per-line confidence is UNAVAILABLE
#:   and never invented — such output cannot source canonical OCR evidence on
#:   its own (Evidence requires ocr_confidence) and is flagged accordingly.
RESPONSE_MODES = frozenset({"json_lines", "plain_text"})


class PaddleOCRHttpProvider(OCRProvider):
    """OCRProvider over an HTTP vision endpoint (Paddle OCR service /
    OpenAI-compatible vision gateway)."""

    def __init__(
        self,
        provider_id: str,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 120.0,
        max_concurrency: int = 4,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        low_confidence_threshold: float = DEFAULT_LOW_CONFIDENCE_THRESHOLD,
        response_mode: str = "json_lines",
        max_output_tokens: int = 4096,
        extra_headers: dict[str, str] | None = None,
        canary_ttl_s: float = 86400.0,
        clock: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        super().__init__(provider_id)
        if response_mode not in RESPONSE_MODES:
            raise ValueError(f"response_mode must be one of {sorted(RESPONSE_MODES)}")
        if max_output_tokens < 1:
            raise ValueError("max_output_tokens must be >= 1")
        self.base_url = base_url
        self.model = model
        self.response_mode = response_mode
        self.max_output_tokens = max_output_tokens
        self._api_key = api_key  # never rendered by __repr__/status/logs
        self.timeout_seconds = timeout_seconds
        self.low_confidence_threshold = low_confidence_threshold
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
            f"<PaddleOCRHttpProvider {self.provider_id} "
            f"base_url={self.base_url!r} model={self.model!r} api_key=[REDACTED]>"
        )

    # -- transport ------------------------------------------------------------------

    def _classify(self, exc: Exception) -> FailureClassification:
        classification = classify_http_failure(
            exc,
            timeout_code="OCR_001",
            rate_limit_code="PROVIDER_002",
            provider_label="OCR provider",
        )
        # Unlike LLM content failures (owned by the agent repair loop), a
        # malformed OCR payload is transport-shape territory and vision-model
        # glitches are often transient: retry OCR_002 within the bounded budget.
        if classification.error.code == "OCR_002":
            return FailureClassification(
                error=classification.error,
                retry=True,
                counts_toward_breaker=classification.counts_toward_breaker,
                stats_hook=classification.stats_hook,
            )
        return classification

    def _payload(self, image: bytes, mime_type: str) -> dict[str, Any]:
        encoded = base64.b64encode(image).decode("ascii")
        image_part = {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
        }
        if self.response_mode == "plain_text":
            # Native transcription backends (PaddleOCR-VL served via chat)
            # degenerate when given chat-style instructions: send the image
            # ONLY and take the raw transcription text.
            content_parts: list[dict[str, Any]] = [image_part]
        else:
            content_parts = [{"type": "text", "text": OCR_PROMPT}, image_part]
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": content_parts}],
            "temperature": 0.0,
            "max_tokens": self.max_output_tokens,
        }

    async def _post_ocr(self, image: bytes, request: OcrPageRequest) -> OcrPageResult:
        payload = self._payload(image, request.mime_type)
        response = await self._client.post(_CHAT_PATH, json=payload, timeout=self.timeout_seconds)
        response.raise_for_status()
        body = parse_json_body(response, invalid_code="OCR_002", label="OCR provider envelope")
        content = self._extract_content(body, response)
        return self._parse_ocr_content(content, request)

    def _extract_content(self, body: Any, response: httpx.Response) -> str:
        if not isinstance(body, dict):
            raise DomainError(
                "OCR_002",
                message="OCR provider envelope is not a JSON object.",
                details={"body_excerpt": safe_body_excerpt(response)},
            )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DomainError(
                "OCR_002",
                message="OCR provider response contains no choices.",
                details={"body_excerpt": safe_body_excerpt(response)},
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise DomainError(
                "OCR_002",
                message="OCR provider message content is missing or not text.",
                details={},
            )
        return content

    def _parse_ocr_content(self, content: str, request: OcrPageRequest) -> OcrPageResult:
        """Parse per configured response_mode.

        ``plain_text``: raw transcription → one OcrLine per non-empty line,
        confidence None (backend reports none; never invented), flagged with
        the OCR_CONFIDENCE_UNAVAILABLE warning.

        ``json_lines``: strict JSON array of {text, confidence, bbox?}.
        Markdown fences emitted by chat-style models are stripped; anything
        else that is not the contracted shape raises OCR_002. The provider's
        classifier retries OCR_002 once within the bounded retry budget
        (max_attempts=2 default) before surfacing it.
        """
        if self.response_mode == "plain_text":
            return self._parse_plain_text(content)
        stripped = _FENCE_RE.sub("", content.strip()).strip()
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError) as exc:
            self.stats.record_response_validation(False)
            raise DomainError(
                "OCR_002",
                message="OCR response content is not valid JSON.",
                details={"content_excerpt": stripped[:200]},
            ) from exc
        if isinstance(payload, dict) and isinstance(payload.get("lines"), list):
            payload = payload["lines"]
        if not isinstance(payload, list):
            self.stats.record_response_validation(False)
            raise DomainError(
                "OCR_002",
                message="OCR response content is not a JSON array of lines.",
                details={},
            )
        lines: list[OcrLine] = []
        for index, item in enumerate(payload):
            if not isinstance(item, dict):
                self.stats.record_response_validation(False)
                raise DomainError(
                    "OCR_002",
                    message=f"OCR line {index} is not an object.",
                    details={"index": index},
                )
            text_value = item.get("text")
            confidence = item.get("confidence")
            if not isinstance(text_value, str):
                self.stats.record_response_validation(False)
                raise DomainError(
                    "OCR_002",
                    message=f"OCR line {index} has no text.",
                    details={"index": index},
                )
            if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
                # Confidences are NEVER invented: a missing confidence is an
                # invalid provider response (no silent fallback).
                self.stats.record_response_validation(False)
                raise DomainError(
                    "OCR_002",
                    message=f"OCR line {index} has no numeric confidence.",
                    details={"index": index},
                )
            bbox_value = item.get("bbox")
            bbox = None
            if bbox_value is not None:
                bbox = self._coerce_bbox(bbox_value, index)
            lines.append(
                OcrLine(
                    text=text_value,
                    confidence=max(0.0, min(1.0, float(confidence))),
                    bbox=bbox,
                )
            )
        self.stats.record_response_validation(True)
        mean_confidence = (
            round(sum(line.confidence for line in lines) / len(lines), 4) if lines else None
        )
        warnings: list[str] = []
        if mean_confidence is not None and mean_confidence < self.low_confidence_threshold:
            warnings.append(
                f"OCR_003: mean confidence {mean_confidence:.2f} below "
                f"{self.low_confidence_threshold:.2f}; page text is degraded quality"
            )
        return OcrPageResult(
            lines=lines,
            full_text="\n".join(line.text for line in lines),
            mean_confidence=mean_confidence,
            warnings=warnings,
            provider_id=self.provider_id,
        )

    def _parse_plain_text(self, content: str) -> OcrPageResult:
        """Native transcription mode: raw text, no per-line confidence.

        Honesty rules: confidence stays None (never invented), the result is
        explicitly flagged OCR_CONFIDENCE_UNAVAILABLE, and such output cannot
        source canonical OCR-derived Evidence on its own (Evidence requires
        ocr_confidence) — downstream must treat it as degraded/fallback.
        """
        text_lines = [line.strip() for line in content.splitlines()]
        lines = [OcrLine(text=line) for line in text_lines if line]
        self.stats.record_response_validation(True)
        return OcrPageResult(
            lines=lines,
            full_text="\n".join(line.text for line in lines),
            mean_confidence=None,
            warnings=[
                "OCR_CONFIDENCE_UNAVAILABLE: backend transcribes without line "
                "confidence; output is fallback-grade, not canonical evidence"
            ],
            provider_id=self.provider_id,
        )

    def _coerce_bbox(self, value: Any, index: int) -> BBox:
        """Contracted bbox shapes: [x0, y0, x1, y1] list or {x0..y1} object.

        Anything else (including a 3-element list) is OCR_002 — geometry is
        never guessed.
        """
        candidate: Any = value
        if isinstance(value, (list, tuple)):
            if len(value) != 4 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
            ):
                candidate = None
            else:
                candidate = {"x0": value[0], "y0": value[1], "x1": value[2], "y1": value[3]}
        try:
            if candidate is None:
                raise ValueError("bbox shape")
            return BBox.model_validate(candidate)
        except Exception as exc:  # noqa: BLE001 - reshaped as OCR_002
            self.stats.record_response_validation(False)
            raise DomainError(
                "OCR_002",
                message=f"OCR line {index} has an invalid bbox.",
                details={"index": index, "reason": type(exc).__name__},
            ) from exc

    async def recognize_page(self, image: bytes, request: OcrPageRequest) -> OcrPageResult:
        """OCR one rendered page image with resilience (429 → PROVIDER_002,
        timeout → OCR_001, malformed → OCR_002, 5xx → PROVIDER_001)."""
        if not image:
            raise DomainError(
                "OCR_002",
                message="Empty page image submitted to OCR.",
                details={"page_number": request.page_number},
            )
        started = time.monotonic()
        result: OcrPageResult = await run_with_resilience(
            lambda: self._post_ocr(image, request),
            retry=self.retry,
            breaker=self.breaker,
            limiter=self.limiter,
            classify=self._classify,
            stats=self.stats,
            sleep=self._sleep,
        )
        return result.model_copy(
            update={
                "latency_ms": int((time.monotonic() - started) * 1000),
                "provider_id": self.provider_id,
            }
        )

    # -- canary ---------------------------------------------------------------------

    async def run_canary(self, expected_text: str = "PaperIntel OCR canary") -> CanaryRecord:
        """Active probe: OCR a synthetic image with known text."""
        image = render_canary_image(expected_text)
        sha = hashlib.sha256(image).hexdigest()
        request = OcrPageRequest(image_sha256=sha, page_number=1, mime_type="image/png")
        started = time.monotonic()
        try:
            result = await self.recognize_page(image, request)
            normalized = re.sub(r"\s+", " ", result.full_text).lower()
            if expected_text.lower() not in normalized:
                raise ValueError("expected canary text not recognized")
            self.canary = CanaryRecord(
                state=CanaryState.OK,
                detail=f"recognized {len(result.lines)} line(s)",
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

    # -- status / health ----------------------------------------------------------------

    def status(self) -> OcrProviderStatus:
        availability = availability_from(
            self.breaker.state,
            self.stats.transport_error_rate,
            self.stats.total_calls > 0,
            self.canary.effective_state(self._clock, self.canary_ttl_s),
        )
        return OcrProviderStatus(
            provider_id=self.provider_id,
            availability=availability,
            concurrency=self.limiter.configured,
            latency=self.stats.latency_mean,
            transport_error_rate=self.stats.transport_error_rate,
            response_validation_rate=self.stats.response_validation_rate,
            canary_state=self.canary.effective_state(self._clock, self.canary_ttl_s),
        )

    async def health(self) -> ModuleHealthRecord:
        status = self.status()
        return ModuleHealthRecord(
            module_id=f"providers.{self.provider_id}",
            state=status.availability,
            checks={
                "configured": "PASS",
                "response_validation": "PASS"
                if status.response_validation_rate is None or status.response_validation_rate > 0.5
                else "WARN",
            },
            metrics={
                "total_calls": self.stats.total_calls,
                "latency_mean": status.latency,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def render_canary_image(text: str, *, width: int = 640, height: int = 160) -> bytes:
    """Render a synthetic PNG containing ``text`` (PyMuPDF, local, no I/O)."""
    import pymupdf as fitz  # noqa: PLC0415

    document = fitz.open()
    page = document.new_page(width=width, height=height)
    page.insert_text((24, height // 2), text, fontsize=28, fontname="helv")
    pixmap = page.get_pixmap(dpi=144)
    png = pixmap.tobytes("png")
    document.close()
    return png


__all__ = [
    "DEFAULT_LOW_CONFIDENCE_THRESHOLD",
    "OCR_PROMPT",
    "OCR_PROMPT_SHA256",
    "OCR_PROMPT_VERSION",
    "PaddleOCRHttpProvider",
    "render_canary_image",
]

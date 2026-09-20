"""Deterministic mock OCR provider (spec doc 06 §3).

Modes (frozen list): VALID, LOW_CONFIDENCE, DIGIT_CORRUPTION, EMPTY, TIMEOUT,
MALFORMED_RESPONSE. Same (mode, seed, image hash) → identical output.
"""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass

from paperintel.errors import DomainError
from paperintel.providers.base import (
    OcrLine,
    OcrPageRequest,
    OcrPageResult,
    OCRProvider,
)
from paperintel.providers.resilience import CanaryRecord
from paperintel.schemas.common import BBox
from paperintel.schemas.enums import CanaryState, OcrMockMode
from paperintel.schemas.health import ModuleHealthRecord, OcrProviderStatus

#: Ground-truth lines used by the mock (mirrors the F06 synthetic-truth
#: fixture facts: dataset size 1,000; accuracy 82.5%; improvement 2.1pp).
_TRUTH_LINES: tuple[str, ...] = (
    "We evaluate on a dataset of 1,000 samples.",
    "Method A reaches 82.5% accuracy.",
    "This is an improvement of 2.1 percentage points over the baseline.",
)

_DIGIT_CORRUPTIONS = {"1,000": "1.000", "82.5": "825", "2.1": "21"}


@dataclass(slots=True)
class MockOcrCallRecord:
    image_sha256: str
    mode: OcrMockMode
    result_text: str | None
    error_code: str | None


class MockOCRProvider(OCRProvider):
    def __init__(
        self,
        provider_id: str = "prv_mockocr",
        *,
        mode: OcrMockMode = OcrMockMode.VALID,
        seed: int = 2000,
    ) -> None:
        super().__init__(provider_id)
        self.mode = mode
        self.seed = seed
        self.calls: list[MockOcrCallRecord] = []
        self.canary_record = CanaryRecord()
        from paperintel.providers.resilience import (
            CircuitBreaker,
            ConcurrencyLimiter,
            ProviderStats,
            RetryPolicy,
        )

        self.stats = ProviderStats()
        self.breaker = CircuitBreaker()
        self.limiter = ConcurrencyLimiter(2)
        self.retry = RetryPolicy(max_attempts=3, base_delay_s=0, max_delay_s=0)

    async def run_canary(self) -> CanaryRecord:
        """Honest canary against the mock's own modes: VALID-style modes → OK;
        TIMEOUT/MALFORMED/EMPTY → FAILED with the catalog code."""
        from paperintel.providers.paddle_ocr import render_canary_image  # noqa: PLC0415

        started = time.monotonic()
        image = render_canary_image("PaperIntel OCR canary")
        request = OcrPageRequest(
            image_sha256=hashlib.sha256(image).hexdigest(),
            page_number=1,
            mime_type="image/png",
        )
        try:
            result = await self.recognize_page(image, request)
            if not result.lines or not result.full_text.strip():
                raise DomainError(
                    "OCR_002",
                    message="Canary produced no text lines.",
                    details={"mode": self.mode.value},
                )
            detail = f"mock mode {self.mode.value}: {len(result.lines)} line(s)"
            if result.warnings:
                detail += f"; warnings={result.warnings}"
            self.canary_record = CanaryRecord(
                state=CanaryState.OK,
                detail=detail,
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=time.monotonic(),
            )
        except DomainError as exc:
            self.canary_record = CanaryRecord(
                state=CanaryState.FAILED,
                detail=f"canary failed: {exc.code}",
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=time.monotonic(),
            )
        return self.canary_record

    def status(self) -> OcrProviderStatus:
        return OcrProviderStatus(
            provider_id=self.provider_id,
            availability="HEALTHY",
            concurrency=0,  # local mock: no external ceiling
            canary_state=self.canary_record.effective_state(time.monotonic, 86400.0),
        )

    def _lines_for_mode(self, image_sha: str) -> list[OcrLine]:
        rng = random.Random(f"{self.seed}:{image_sha}")
        match self.mode:
            case OcrMockMode.VALID:
                confidence = 0.97
                texts = list(_TRUTH_LINES)
            case OcrMockMode.LOW_CONFIDENCE:
                confidence = 0.42
                texts = list(_TRUTH_LINES)
            case OcrMockMode.DIGIT_CORRUPTION:
                confidence = 0.91
                texts = [_replace_digits(line) for line in _TRUTH_LINES]
            case OcrMockMode.EMPTY:
                return []
            case _:  # pragma: no cover - transport modes never reach here
                raise AssertionError(f"_lines_for_mode called for {self.mode}")
        lines: list[OcrLine] = []
        y = 72.0
        for text in texts:
            lines.append(
                OcrLine(
                    text=text,
                    confidence=round(confidence - rng.random() * 0.01, 4),
                    bbox=BBox(x0=54.0, y0=y, x1=54.0 + 6.0 * len(text), y1=y + 12.0),
                )
            )
            y += 18.0
        return lines

    async def recognize_page(self, image: bytes, request: OcrPageRequest) -> OcrPageResult:
        from paperintel.providers.resilience import run_with_resilience

        return await run_with_resilience(
            lambda: self._recognize_once(image, request),
            retry=self.retry,
            breaker=self.breaker,
            limiter=self.limiter,
            stats=self.stats,
        )

    async def _recognize_once(self, image: bytes, request: OcrPageRequest) -> OcrPageResult:
        image_sha = hashlib.sha256(image).hexdigest()
        error_code: str | None = None
        result: OcrPageResult | None = None
        try:
            if request.image_sha256 != image_sha:
                # Never silently accept a mismatched render (spec doc 00 §7).
                raise DomainError(
                    "OCR_002",
                    message="Rendered image does not match the declared image_sha256.",
                    details={"declared": request.image_sha256, "actual": image_sha},
                )
            match self.mode:
                case OcrMockMode.TIMEOUT:
                    raise DomainError("OCR_001", details={"provider_id": self.provider_id})
                case OcrMockMode.MALFORMED_RESPONSE:
                    raise DomainError(
                        "OCR_002",
                        message="OCR provider returned a malformed response payload.",
                        details={"provider_id": self.provider_id},
                    )
                case _:
                    lines = self._lines_for_mode(image_sha)
                    full_text = "\n".join(line.text for line in lines)
                    mean_conf = (
                        round(sum(line.confidence for line in lines) / len(lines), 4)
                        if lines
                        else None
                    )
                    warnings: list[str] = []
                    if self.mode is OcrMockMode.LOW_CONFIDENCE:
                        warnings.append("OCR_003: mean confidence below threshold")
                    result = OcrPageResult(
                        lines=lines,
                        full_text=full_text,
                        mean_confidence=mean_conf,
                        warnings=warnings,
                        latency_ms=1,
                        provider_id=self.provider_id,
                    )
        except DomainError as exc:
            error_code = exc.code
            self.calls.append(MockOcrCallRecord(image_sha, self.mode, None, error_code))
            raise
        self.calls.append(
            MockOcrCallRecord(image_sha, self.mode, result.full_text if result else "", None)
        )
        assert result is not None  # for type checkers; unreachable otherwise
        return result

    async def health(self) -> ModuleHealthRecord:
        return ModuleHealthRecord(
            module_id=f"providers.ocr.mock.{self.mode.value.lower()}",
            state="HEALTHY",
            checks={"deterministic": "PASS"},
            metrics={"calls_total": len(self.calls)},
        )


def _replace_digits(text: str) -> str:
    for good, bad in _DIGIT_CORRUPTIONS.items():
        text = text.replace(good, bad)
    return text


__all__ = ["MockOCRProvider", "MockOcrCallRecord"]

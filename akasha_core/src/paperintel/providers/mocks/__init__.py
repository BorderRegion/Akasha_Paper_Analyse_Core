"""Deterministic mock providers for tests, gates, and `paperctl selftest --mock`."""

from paperintel.providers.mocks.llm import (
    CANARY_EVIDENCE_ID,
    CANARY_EVIDENCE_TEXT,
    MockCallRecord,
    MockLLMProvider,
)
from paperintel.providers.mocks.ocr import MockOcrCallRecord, MockOCRProvider

__all__ = [
    "CANARY_EVIDENCE_ID",
    "CANARY_EVIDENCE_TEXT",
    "MockCallRecord",
    "MockLLMProvider",
    "MockOCRProvider",
    "MockOcrCallRecord",
]

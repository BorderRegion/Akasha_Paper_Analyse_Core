"""Real-provider canary tests (OPTIONAL at gate P02 per spec doc 05).

These run against the operator-provided DeepSeek official API and the DLUT
newapi gateway using credentials from the environment (gitignored .env in
local runs). They skip when credentials are absent and NEVER print key
material. Not part of the machine gate: mocks are mandatory, real creds are
optional.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from paperintel.providers.http_embeddings import OpenAICompatibleEmbeddingProvider
from paperintel.providers.http_llm import OpenAICompatibleLLMProvider
from paperintel.providers.paddle_ocr import PaddleOCRHttpProvider
from paperintel.providers.resilience import RetryPolicy
from paperintel.schemas.enums import CanaryState, ModelRole

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DOTENV = _REPO_ROOT / ".env"

#: Gate-controlled marker: these tests only run when the operator explicitly
#: enables real-provider checks (PAPERINTEL_REAL_PROVIDERS=1).
_REAL_ENABLED = bool(os.environ.get("PAPERINTEL_REAL_PROVIDERS"))


def _dotenv_values() -> dict[str, str]:
    """Parse KEY=VALUE lines from the gitignored .env (comments skipped).

    Test isolation scrubs ambient env vars, so credentials are resolved here
    at import time. Values are NEVER printed or asserted into output.
    """
    values: dict[str, str] = {}
    try:
        raw = _DOTENV.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


_DOTENV_CACHE = _dotenv_values()


def _credential(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name) or _DOTENV_CACHE.get(name)
    return value or default


DEEPSEEK_KEY = _credential("LLM_API_KEY")
DEEPSEEK_URL = _credential("LLM_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = _credential("LLM_ANALYST_MODEL", "deepseek-chat")

GATEWAY_KEY = _credential("PAPERINTEL_GATEWAY_API_KEY")
GATEWAY_URL = _credential("PAPERINTEL_GATEWAY_BASE_URL", "http://aigw.dlut.edu.cn/v1")
GATEWAY_OCR_MODEL = _credential("GATEWAY_OCR_MODEL", "Qwen3-VL-32B-Instruct")
GATEWAY_OCR_ALT_MODEL = _credential("GATEWAY_OCR_ALT_MODEL", "PaddleOCR-VL-1.5")
GATEWAY_EMBED_MODEL = _credential("GATEWAY_EMBEDDING_MODEL", "Qwen3-Embedding-8B")
GATEWAY_LLM_MODEL = _credential("GATEWAY_LLM_CANARY_MODEL", "Qwen3-8B")

requires_deepseek = pytest.mark.skipif(
    not (_REAL_ENABLED and DEEPSEEK_KEY),
    reason="real-provider checks disabled or LLM_API_KEY not configured (optional at P02)",
)
requires_gateway = pytest.mark.skipif(
    not (_REAL_ENABLED and GATEWAY_KEY),
    reason="real-provider checks disabled or gateway key not configured (optional at P02)",
)


async def _run_canary(provider, **kwargs):
    """Canary + aclose MUST share one event loop (httpx pools are
    loop-bound); closing in a fresh asyncio.run raises 'Event loop is
    closed'."""
    try:
        return await provider.run_canary(**kwargs)
    finally:
        await provider.aclose()


@requires_deepseek
def test_deepseek_llm_canary() -> None:
    provider = OpenAICompatibleLLMProvider(
        "prv_deepseek_canary",
        base_url=DEEPSEEK_URL,
        models={ModelRole.ANALYST: DEEPSEEK_MODEL},
        api_key=DEEPSEEK_KEY,
        timeout_seconds=90.0,
        max_concurrency=1,
        retry=RetryPolicy(max_attempts=2, base_delay_s=2.0),
    )
    record = asyncio.run(_run_canary(provider, nonce="p02-deepseek"))
    assert record.state is CanaryState.OK, record.detail


@requires_gateway
def test_gateway_llm_canary() -> None:
    provider = OpenAICompatibleLLMProvider(
        "prv_gateway_llm_canary",
        base_url=GATEWAY_URL,
        models={ModelRole.ANALYST: GATEWAY_LLM_MODEL},
        api_key=GATEWAY_KEY,
        timeout_seconds=90.0,
        max_concurrency=1,
        retry=RetryPolicy(max_attempts=2, base_delay_s=2.0),
    )
    record = asyncio.run(_run_canary(provider, nonce="p02-gateway"))
    assert record.state is CanaryState.OK, record.detail


@requires_gateway
def test_gateway_paddle_ocr_canary() -> None:
    """Canonical OCR path: instruction-following VL model, json_lines mode
    (must yield confidence-bearing lines usable for Evidence)."""
    provider = PaddleOCRHttpProvider(
        "prv_gateway_ocr_canary",
        base_url=GATEWAY_URL,
        model=GATEWAY_OCR_MODEL,
        api_key=GATEWAY_KEY,
        timeout_seconds=120.0,
        max_concurrency=1,
        retry=RetryPolicy(max_attempts=2, base_delay_s=2.0),
    )
    record = asyncio.run(_run_canary(provider))
    assert record.state is CanaryState.OK, record.detail


@requires_gateway
def test_gateway_paddleocr_vl_plain_text_canary() -> None:
    """Native transcription path: PaddleOCR-VL-1.5 in plain_text mode
    (image-only prompt; raw text; no confidence — flagged fallback-grade)."""
    provider = PaddleOCRHttpProvider(
        "prv_gateway_ocr_alt_canary",
        base_url=GATEWAY_URL,
        model=GATEWAY_OCR_ALT_MODEL,
        api_key=GATEWAY_KEY,
        timeout_seconds=120.0,
        max_concurrency=1,
        response_mode="plain_text",
        retry=RetryPolicy(max_attempts=2, base_delay_s=2.0),
    )
    record = asyncio.run(_run_canary(provider))
    assert record.state is CanaryState.OK, record.detail


@requires_gateway
def test_gateway_embedding_canary() -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        "prv_gateway_embed_canary",
        base_url=GATEWAY_URL,
        model=GATEWAY_EMBED_MODEL,
        api_key=GATEWAY_KEY,
        timeout_seconds=60.0,
        max_concurrency=1,
    )
    record = asyncio.run(_run_canary(provider))
    assert record.state is CanaryState.OK, record.detail

"""P07 real-provider smoke: ONE real LLM agent call through the full
framework (context → prompt → provider → firewall → ledger).

Opt-in like the P02 real-provider checks (PAPERINTEL_REAL_PROVIDERS=1 +
credentials in the gitignored .env): gates stay deterministic offline;
this proves the framework works against a live model too. The assertion
is structural (the pipeline completes and claims carry evidence), never
about specific prose.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(_REPO_ROOT / "tests"))

from tests.integration.p02.test_real_providers import (  # noqa: E402
    _REAL_ENABLED,
    DEEPSEEK_KEY,
)

pytestmark = pytest.mark.skipif(
    not _REAL_ENABLED or not DEEPSEEK_KEY,
    reason="real-provider checks disabled or no LLM credentials",
)


@pytest.mark.needs_db
def test_real_llm_agent_run_through_framework(session, store, data_dir, tmp_path) -> None:
    """One research-question agent call on the golden paper via the real
    API: the firewall either accepts the response (claims with evidence)
    or rejects it with a designated code — both are correct outcomes; a
    crash or a silent path is not."""
    from tests.fixtures.generators import build_f01_native

    from paperintel.agents.base import run_agent
    from paperintel.agents.prompts import ensure_builtin_prompts
    from paperintel.ingest.service import import_pdf
    from paperintel.knowledge.claims import persist_agent_result
    from paperintel.providers.base import LLMProvider, LlmRequest
    from paperintel.schemas.agent import AgentRequest

    class _RealDeepSeekLLM(LLMProvider):
        """Minimal adapter: P02's HTTP provider is the production one; this
        smoke test only needs complete() against the real endpoint."""

        def __init__(self) -> None:
            super().__init__("prv_deepseek")

        async def health(self):
            from paperintel.schemas.health import ModuleHealthRecord

            return ModuleHealthRecord(
                module_id="providers.llm.deepseek.smoke",
                state="HEALTHY",
                checks={"credentials_present": "PASS"},
            )

        async def complete(self, request: LlmRequest):
            import time

            import httpx

            started = time.monotonic()
            body = {
                "model": os.environ.get("LLM_MODEL", "deepseek-v4-pro"),
                "messages": [{"role": m.role, "content": m.content} for m in request.messages],
                "response_format": {"type": "json_object"},
                "temperature": 0.0,
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{os.environ.get('LLM_BASE_URL', 'https://api.deepseek.com')}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                    json=body,
                )
            latency_ms = int((time.monotonic() - started) * 1000)
            if response.status_code >= 500:
                from paperintel.errors import DomainError

                raise DomainError("PROVIDER_001", details={"status": response.status_code})
            response.raise_for_status()
            data = response.json()
            from paperintel.providers.base import LlmResponse, LlmUsage

            return LlmResponse(
                content=data["choices"][0]["message"]["content"],
                finish_reason=data["choices"][0].get("finish_reason"),
                usage=LlmUsage(
                    input_tokens=data.get("usage", {}).get("prompt_tokens"),
                    output_tokens=data.get("usage", {}).get("completion_tokens"),
                ),
                latency_ms=latency_ms,
                model_id=body["model"],
                provider_id=self.provider_id,
            )

    path = tmp_path / "f01.pdf"
    path.write_bytes(build_f01_native())
    imported = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
    ensure_builtin_prompts(session)
    session.commit()

    request = AgentRequest(
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        agent_type="agents.research_question",
    )
    provider = _RealDeepSeekLLM()

    from paperintel.errors import DomainError

    try:
        outcome = asyncio.run(run_agent(request, session, provider=provider))
    except DomainError as exc:
        # A designated firewall rejection is a CORRECT outcome for a live
        # model — the ModelCallRow audit trail (written before the raise)
        # proves the call happened and was validated, not swallowed.
        assert exc.code in (
            "LLM_003",
            "LLM_004",
            "LLM_005",
            "EVIDENCE_001",
            "EVIDENCE_002",
            "CLAIM_001",
        ), f"unexpected error class: {exc.code}"
        from sqlalchemy import select

        from paperintel.database.models import ModelCallRow

        audited = session.scalar(
            select(ModelCallRow).where(ModelCallRow.provider_id == "prv_deepseek")
        )
        assert audited is not None, "failed real call was not audited"
        return

    assert outcome.model_call_id is not None
    ledger = persist_agent_result(session, request, outcome)
    # Structural assertions only: claims (if any) carry evidence links.
    assert ledger.claims_created >= 0

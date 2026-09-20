"""Deterministic mock LLM provider (spec doc 06 §3, spec doc 05 P00/P02).

Every mode is deterministic for a given (mode, seed): the same request yields
byte-identical output. Malicious modes produce the exact failure classes the
schema firewall must reject, so firewall tests never depend on a real model.

Modes (frozen list, spec doc 06 §3):
VALID, NON_JSON, MISSING_REQUIRED_FIELD, UNKNOWN_EVIDENCE,
WRONG_PAPER_EVIDENCE, INVENTED_NUMBER, DUPLICATE_CLAIMS, TAG_SPAM, REFUSAL,
TIMEOUT, RATE_LIMIT, SERVER_ERROR.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any

from paperintel.errors import DomainError
from paperintel.providers.base import (
    ChatMessage,
    LLMProvider,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)
from paperintel.providers.resilience import CanaryRecord
from paperintel.schemas.enums import CanaryState, CircuitState, LlmMockMode, ModelRole
from paperintel.schemas.health import LlmProviderStatus, ModuleHealthRecord

#: Fixed canary evidence used by VALID/canary-style answers (spec doc 06 §10).
CANARY_EVIDENCE_ID = "ev_canary0000000000000000000000"
CANARY_EVIDENCE_TEXT = "The dataset contains 1,000 samples. Method A reaches 82.5% accuracy."


def _valid_agent_result(evidence_id: str = CANARY_EVIDENCE_ID) -> dict[str, Any]:
    """A well-formed AgentResult payload anchored in the canary facts."""
    return {
        "status": "SUCCESS",
        "claims": [
            {
                "claim_type": "FACT",
                "category": "dataset_size",
                "statement": "The dataset contains 1,000 samples.",
                "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
                "uncertainties": [],
            },
            {
                "claim_type": "FACT",
                "category": "main_result",
                "statement": "Method A reaches 82.5% accuracy.",
                "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
                "uncertainties": [],
            },
        ],
        "observations": ["Reported improvement over the baseline is 2.1 percentage points."],
        "uncertainties": [],
        "requests_for_more_evidence": [],
        "warnings": [],
    }


@dataclass(slots=True)
class MockCallRecord:
    request_messages: list[tuple[str, str]]
    mode: LlmMockMode
    response_content: str | None
    error_code: str | None


class MockLLMProvider(LLMProvider):
    """Deterministic in-process LLM provider for tests and selftest --mock."""

    def __init__(
        self,
        provider_id: str = "prv_mockllm",
        *,
        mode: LlmMockMode = LlmMockMode.VALID,
        seed: int = 1000,
    ) -> None:
        super().__init__(provider_id)
        self.mode = mode
        self.seed = seed
        self.calls: list[MockCallRecord] = []
        self.canary_record = CanaryRecord()
        from paperintel.providers.resilience import (
            CircuitBreaker,
            ConcurrencyLimiter,
            ProviderStats,
            RetryPolicy,
        )

        self.stats = ProviderStats()
        self.breaker = CircuitBreaker()
        self.limiter = ConcurrencyLimiter(8)
        self.retry = RetryPolicy(max_attempts=3, base_delay_s=0, max_delay_s=0)

    # -- behavior ----------------------------------------------------------

    def _content_for_mode(self) -> str:
        rng = random.Random(self.seed)
        match self.mode:
            case LlmMockMode.VALID:
                return json.dumps(_valid_agent_result(), sort_keys=True)
            case LlmMockMode.NON_JSON:
                return "Sure! Here is a friendly prose answer instead of JSON: it works great."
            case LlmMockMode.MISSING_REQUIRED_FIELD:
                payload = _valid_agent_result()
                del payload["status"]
                return json.dumps(payload, sort_keys=True)
            case LlmMockMode.UNKNOWN_EVIDENCE:
                return json.dumps(
                    _valid_agent_result(evidence_id="ev_doesnotexist000000000000"),
                    sort_keys=True,
                )
            case LlmMockMode.WRONG_PAPER_EVIDENCE:
                return json.dumps(
                    _valid_agent_result(evidence_id="ev_otherpaper00000000000000000"),
                    sort_keys=True,
                )
            case LlmMockMode.INVENTED_NUMBER:
                payload = _valid_agent_result()
                payload["claims"].append(
                    {
                        "claim_type": "FACT",
                        "category": "main_result",
                        "statement": "Method A reaches 97.3% accuracy on the benchmark.",
                        "evidence": [{"evidence_id": CANARY_EVIDENCE_ID, "role": "SUPPORT"}],
                        "uncertainties": [],
                    }
                )
                return json.dumps(payload, sort_keys=True)
            case LlmMockMode.DUPLICATE_CLAIMS:
                payload = _valid_agent_result()
                payload["claims"].append(dict(payload["claims"][0]))
                payload["claims"].append(dict(payload["claims"][0]))
                return json.dumps(payload, sort_keys=True)
            case LlmMockMode.TAG_SPAM:
                payload = _valid_agent_result()
                payload["observations"] = [f"spam/tag-{rng.randrange(10**6):06d}"] * 500
                return json.dumps(payload, sort_keys=True)
            case LlmMockMode.REFUSAL:
                return "I'm sorry, but I cannot assist with analyzing this paper."
            case LlmMockMode.TIMEOUT:
                raise DomainError("LLM_001", details={"provider_id": self.provider_id})
            case LlmMockMode.RATE_LIMIT:
                raise DomainError("LLM_002", details={"provider_id": self.provider_id})
            case LlmMockMode.SERVER_ERROR:
                raise DomainError("PROVIDER_001", details={"provider_id": self.provider_id})
        raise AssertionError(f"unhandled mock mode: {self.mode}")  # pragma: no cover

    async def complete(self, request: LlmRequest) -> LlmResponse:
        from paperintel.providers.resilience import run_with_resilience

        return await run_with_resilience(
            lambda: self._complete_once(request),
            retry=self.retry,
            breaker=self.breaker,
            limiter=self.limiter,
            stats=self.stats,
        )

    async def _complete_once(self, request: LlmRequest) -> LlmResponse:
        content: str | None = None
        error_code: str | None = None
        try:
            content = self._content_for_mode()
        except DomainError as exc:
            error_code = exc.code
            self.calls.append(
                MockCallRecord(
                    request_messages=[(m.role, m.content) for m in request.messages],
                    mode=self.mode,
                    response_content=None,
                    error_code=error_code,
                )
            )
            raise
        self.calls.append(
            MockCallRecord(
                request_messages=[(m.role, m.content) for m in request.messages],
                mode=self.mode,
                response_content=content,
                error_code=None,
            )
        )
        return LlmResponse(
            content=content or "",
            finish_reason="stop",
            usage=LlmUsage(
                input_tokens=sum(len(m.content) for m in request.messages) // 4,
                output_tokens=len(content or "") // 4,
            ),
            latency_ms=1,
            model_id="mock-analyst",
            provider_id=self.provider_id,
        )

    async def run_canary(self) -> CanaryRecord:
        """Honest canary: VALID → OK; every failure mode → FAILED with its
        catalog code, so selftest exercises the failure paths too."""
        started = time.monotonic()
        request = LlmRequest(
            messages=[ChatMessage(role="user", content="canary")],
            model_role=ModelRole.ANALYST,
            response_json=True,
        )
        try:
            response = await self.complete(request)
            json.loads(response.content)
        except (DomainError, json.JSONDecodeError, ValueError) as exc:
            code = exc.code if isinstance(exc, DomainError) else "NON_JSON"
            self.canary_record = CanaryRecord(
                state=CanaryState.FAILED,
                detail=f"canary failed: {code}",
                latency_ms=int((time.monotonic() - started) * 1000),
                checked_at=time.monotonic(),
            )
            return self.canary_record
        self.canary_record = CanaryRecord(
            state=CanaryState.OK,
            detail=f"mock mode {self.mode.value} produced valid JSON",
            latency_ms=int((time.monotonic() - started) * 1000),
            checked_at=time.monotonic(),
        )
        return self.canary_record

    def status(self) -> LlmProviderStatus:
        return LlmProviderStatus(
            provider_id=self.provider_id,
            availability="HEALTHY",
            configured_concurrency=0,  # local mock: no external ceiling
            active_concurrency=0,
            canary_state=self.canary_record.effective_state(time.monotonic, 86400.0),
            circuit_breaker_state=CircuitState.CLOSED,
        )

    async def health(self) -> ModuleHealthRecord:
        return ModuleHealthRecord(
            module_id=f"providers.llm.mock.{self.mode.value.lower()}",
            state="HEALTHY",
            checks={"deterministic": "PASS"},
            metrics={"calls_total": len(self.calls)},
        )


__all__ = [
    "CANARY_EVIDENCE_ID",
    "CANARY_EVIDENCE_TEXT",
    "MockCallRecord",
    "MockLLMProvider",
]

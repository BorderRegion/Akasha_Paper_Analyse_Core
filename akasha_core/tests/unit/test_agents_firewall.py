"""P06 firewall unit tests: malicious fixtures through the full pipeline
(spec doc 05 P06 — every malicious output class must be rejected with its
designated catalog code; no silent fallbacks)."""

from __future__ import annotations

import json

import pytest

from paperintel.agents.firewall import (
    MAX_OBSERVATIONS,
    MAX_STATEMENT_CHARS,
    detect_refusal,
    semantic_validation,
    validate_response,
)
from paperintel.errors import DomainError
from paperintel.providers.mocks.llm import CANARY_EVIDENCE_ID
from paperintel.schemas.agent import AgentResult
from paperintel.schemas.enums import AgentStatus, SchemaStatus


def _claim(**overrides):
    base = {
        "claim_type": "FACT",
        "category": "dataset",
        "statement": "The dataset contains 1,000 samples.",
        "evidence": [{"evidence_id": CANARY_EVIDENCE_ID, "role": "SUPPORT"}],
        "uncertainties": [],
    }
    base.update(overrides)
    return base


def _payload(**overrides) -> dict:
    base = {
        "status": "SUCCESS",
        "claims": [_claim()],
        "observations": [],
        "uncertainties": [],
        "requests_for_more_evidence": [],
        "warnings": [],
    }
    base.update(overrides)
    return base


def _resolve_ok(evidence_ids):
    """Evidence resolver stub: canary ID exists and supports the claims."""

    class _Row:
        def __init__(self, evidence_id):
            self.text = "The dataset contains 1,000 samples. Method A reaches 82.5% accuracy."
            self.evidence_id = evidence_id

    return [_Row(eid) for eid in evidence_ids]


def _validate(content: str) -> AgentResult:
    outcome = validate_response(content, AgentResult)
    assert outcome.accepted
    return outcome.payload


class TestJsonParsing:
    def test_non_json_output_rejected_llm_003(self) -> None:
        with pytest.raises(DomainError) as excinfo:
            _validate("Sure! Here is a friendly prose answer instead of JSON.")
        assert excinfo.value.code == "LLM_003"

    def test_markdown_fenced_json_tolerated_once(self) -> None:
        fenced = "```json\n" + json.dumps(_payload()) + "\n```"
        result = _validate(fenced)
        assert result.status is AgentStatus.SUCCESS

    def test_empty_response_rejected(self) -> None:
        with pytest.raises(DomainError) as excinfo:
            _validate("   ")
        assert excinfo.value.code == "LLM_003"

    def test_json_array_rejected(self) -> None:
        with pytest.raises(DomainError) as excinfo:
            _validate(json.dumps([_payload()]))
        assert excinfo.value.code == "LLM_003"


class TestSchemaValidation:
    def test_missing_required_field_rejected_llm_004(self) -> None:
        payload = _payload()
        del payload["status"]
        with pytest.raises(DomainError) as excinfo:
            _validate(json.dumps(payload))
        assert excinfo.value.code == "LLM_004"

    def test_wrong_field_type_rejected_llm_004(self) -> None:
        payload = _payload()
        payload["claims"] = "not-a-list"
        with pytest.raises(DomainError) as excinfo:
            _validate(json.dumps(payload))
        assert excinfo.value.code == "LLM_004"

    def test_extra_field_rejected_llm_004(self) -> None:
        payload = _payload()
        payload["surprise"] = True
        with pytest.raises(DomainError) as excinfo:
            _validate(json.dumps(payload))
        assert excinfo.value.code == "LLM_004"

    def test_one_constrained_repair_attempt(self) -> None:
        """LLM_004 is retryable: exactly ONE repair attempt, recorded."""
        payload = _payload()
        del payload["status"]
        calls = []

        def repair(error: DomainError) -> str:
            calls.append(error.code)
            return json.dumps(_payload())

        outcome = validate_response(json.dumps(payload), AgentResult, repair=repair)
        assert calls == ["LLM_004"]
        assert outcome.status is SchemaStatus.PASSED_AFTER_REPAIR
        assert any("repair succeeded" in note for note in outcome.notes)

    def test_failed_repair_reraises_llm_004(self) -> None:
        payload = _payload()
        del payload["status"]

        def repair(error: DomainError) -> str:
            return json.dumps(payload)  # still broken

        with pytest.raises(DomainError) as excinfo:
            validate_response(json.dumps(payload), AgentResult, repair=repair)
        assert excinfo.value.code == "LLM_004"


class TestSemanticValidation:
    def test_valid_result_passes(self) -> None:
        result = AgentResult.model_validate(_payload())
        validated, warnings = semantic_validation(result, resolve_evidence=_resolve_ok)
        assert len(validated.claims) == 1
        assert warnings == []

    def test_nonexistent_evidence_rejected_evidence_001(self) -> None:
        payload = _payload(
            claims=[
                _claim(
                    evidence=[{"evidence_id": "ev_missing00000000000000000000", "role": "SUPPORT"}]
                )
            ]
        )
        result = AgentResult.model_validate(payload)
        with pytest.raises(DomainError) as excinfo:
            semantic_validation(result, resolve_evidence=_raise_evidence_001)
        assert excinfo.value.code == "EVIDENCE_001"

    def test_wrong_paper_evidence_rejected_evidence_002(self) -> None:
        result = AgentResult.model_validate(_payload())
        with pytest.raises(DomainError) as excinfo:
            semantic_validation(result, resolve_evidence=_raise_evidence_002)
        assert excinfo.value.code == "EVIDENCE_002"

    def test_unsupported_fact_number_dropped_claim_001(self) -> None:
        """INVENTED_NUMBER: 97.3% appears nowhere in the cited evidence.
        The candidate is dropped; valid claims survive."""
        payload = _payload(
            claims=[
                _claim(),
                _claim(
                    category="main_result",
                    statement="Method A reaches 97.3% accuracy on the benchmark.",
                ),
            ]
        )
        result = AgentResult.model_validate(payload)
        validated, warnings = semantic_validation(result, resolve_evidence=_resolve_ok)
        assert len(validated.claims) == 1
        assert "97.3%" not in validated.claims[0].statement
        assert any("CLAIM_001" in warning for warning in warnings)

    def test_duplicated_claim_rejected_llm_005(self) -> None:
        payload = _payload(claims=[_claim(), _claim()])
        result = AgentResult.model_validate(payload)
        with pytest.raises(DomainError) as excinfo:
            semantic_validation(result, resolve_evidence=_resolve_ok)
        assert excinfo.value.code == "LLM_005"

    def test_tag_spam_rejected_llm_005(self) -> None:
        payload = _payload(observations=[f"spam/tag-{i:06d}" for i in range(500)])
        result = AgentResult.model_validate(payload)
        with pytest.raises(DomainError) as excinfo:
            semantic_validation(result, resolve_evidence=_resolve_ok)
        assert excinfo.value.code == "LLM_005"
        assert excinfo.value.details["observation_count"] > MAX_OBSERVATIONS

    def test_overlong_field_rejected_llm_005(self) -> None:
        payload = _payload(claims=[_claim(statement="x" * (MAX_STATEMENT_CHARS + 1))])
        result = AgentResult.model_validate(payload)
        with pytest.raises(DomainError) as excinfo:
            semantic_validation(result, resolve_evidence=_resolve_ok)
        assert excinfo.value.code == "LLM_005"
        assert excinfo.value.details["statement_length"] > MAX_STATEMENT_CHARS

    def test_claim_count_limit_rejected_llm_005(self) -> None:
        from paperintel.agents.firewall import MAX_CLAIMS_PER_RESPONSE

        claims = [
            _claim(category=f"c{i}", statement=f"Statement number {i} is supported.")
            for i in range(MAX_CLAIMS_PER_RESPONSE + 1)
        ]
        result = AgentResult.model_validate(_payload(claims=claims))
        with pytest.raises(DomainError) as excinfo:
            semantic_validation(result, resolve_evidence=_resolve_ok)
        assert excinfo.value.code == "LLM_005"


class TestRefusal:
    def test_model_refusal_rejected_llm_005(self) -> None:
        assert detect_refusal("I'm sorry, but I cannot assist with that request.")
        with pytest.raises(DomainError) as excinfo:
            _validate("I'm sorry, but I cannot assist with analyzing this paper.")
        assert excinfo.value.code == "LLM_005"

    def test_normal_content_not_flagged_as_refusal(self) -> None:
        assert not detect_refusal(json.dumps(_payload()))


class TestInsufficientEvidence:
    def test_insufficient_evidence_is_valid_not_failure(self) -> None:
        payload = _payload(status="INSUFFICIENT_EVIDENCE", claims=[])
        result = _validate(json.dumps(payload))
        assert result.status is AgentStatus.INSUFFICIENT_EVIDENCE


def _raise_evidence_001(evidence_ids):
    raise DomainError(
        "EVIDENCE_001", message="Evidence not found.", details={"evidence_id": evidence_ids[0]}
    )


def _raise_evidence_002(evidence_ids):
    raise DomainError(
        "EVIDENCE_002",
        message="Evidence belongs to another paper version.",
        details={"evidence_id": evidence_ids[0]},
    )

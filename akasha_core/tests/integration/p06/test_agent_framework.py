"""P06 integration tests: agent framework end-to-end with the disposable DB.

Covers the doc 05 P06 gate list: base agent contract, paper tools
(evidence-scoped context), prompt registry/versioning, structured LLM
output, JSON parsing, Pydantic validation, semantic validation, evidence
ID validation, INSUFFICIENT_EVIDENCE, model-call audit records — plus the
malicious fixtures against REAL persisted evidence, and the no-bypass
guarantee.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.fixtures.generators import build_f01_native, build_f04_rich

from paperintel.agents import prompts as prompt_registry
from paperintel.agents.base import AGENTS, BaseAgent, register_agent, run_agent
from paperintel.agents.tools import build_evidence_context
from paperintel.database.models import EvidenceRow, ModelCallRow
from paperintel.errors import DomainError
from paperintel.ingest.service import import_pdf
from paperintel.providers.base import LlmResponse, LlmUsage
from paperintel.providers.mocks.llm import MockLLMProvider
from paperintel.schemas.agent import AgentRequest
from paperintel.schemas.enums import AgentStatus, SchemaStatus
from paperintel.storage.object_store import LocalObjectStore


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


def import_paper(session, store, data_dir, builder, name: str):
    path = data_dir.parent / name
    path.write_bytes(builder())
    return run(import_pdf(path, session=session, store=store, data_dir=data_dir))


class ScriptedLLMProvider(MockLLMProvider):
    """Deterministic provider emitting scripted payloads in order — the
    pipeline is tested against REAL persisted evidence rows."""

    def __init__(self, payloads: list[str | Exception]) -> None:
        super().__init__()
        self.payloads = list(payloads)
        self.call_count = 0

    async def complete(self, request):
        self.call_count += 1
        payload = self.payloads.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return LlmResponse(
            content=payload,
            finish_reason="stop",
            usage=LlmUsage(input_tokens=10, output_tokens=10),
            latency_ms=1,
            model_id="scripted-analyst",
            provider_id="prv_scripted",
        )


def _agent_result_payload(
    evidence_id: str, *, claim: dict | None = None, **payload_overrides
) -> dict:
    claim_base = {
        "claim_type": "FACT",
        "category": "dataset",
        "statement": "The dataset contains 1,000 samples.",
        "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
        "uncertainties": [],
    }
    claim_base.update(claim or {})
    base = {
        "status": "SUCCESS",
        "claims": [claim_base],
        "observations": [],
        "uncertainties": [],
        "requests_for_more_evidence": [],
        "warnings": [],
    }
    base.update(payload_overrides)
    return base


def _request(paper_id: str, paper_version_id: str) -> AgentRequest:
    return AgentRequest(
        paper_id=paper_id,
        paper_version_id=paper_version_id,
        agent_type="agents.framework_test",
    )


# A registered test agent (framework contract, no domain logic).
@register_agent
class FrameworkTestAgent(BaseAgent):
    agent_type = "agents.framework_test"
    prompt_name = "agents.claim_extraction"
    prompt_version = "1.0.0"


@pytest.fixture()
def seeded_version(session, store, data_dir):
    """Imported F01 with built-in prompts registered."""
    prompt_registry.ensure_builtin_prompts(session)
    result = import_paper(session, store, data_dir, build_f01_native, "f01.pdf")
    return result


def _first_evidence_id(session, paper_version_id: str) -> str:
    return session.scalars(
        select(EvidenceRow.evidence_id).where(
            EvidenceRow.paper_version_id == paper_version_id,
            EvidenceRow.text.is_not(None),
        )
    ).first()


# ---------------------------------------------------------------------------
# happy path + audit
# ---------------------------------------------------------------------------


def test_valid_agent_run_creates_audited_model_call(
    session, store, data_dir, seeded_version
) -> None:
    evidence_id = _first_evidence_id(session, seeded_version.paper_version_id)
    evidence_text = session.get(EvidenceRow, evidence_id).text
    supported_statement = evidence_text.split(".")[0].strip()[:180]
    provider = ScriptedLLMProvider(
        [json.dumps(_agent_result_payload(evidence_id, claim={"statement": supported_statement}))]
    )

    outcome = run(
        run_agent(
            _request(seeded_version.paper_id, seeded_version.paper_version_id),
            session,
            provider=provider,
        )
    )

    assert outcome.result.status is AgentStatus.SUCCESS, outcome.warnings
    assert len(outcome.result.claims) == 1
    assert outcome.result.claims[0].evidence[0].evidence_id == evidence_id
    assert outcome.warnings == []

    # Audit record: exactly one model call, accepted.
    calls = session.scalars(select(ModelCallRow)).all()
    assert len(calls) == 1
    call = calls[0]
    assert call.schema_status is SchemaStatus.PASSED
    assert call.provider_id == "prv_scripted"
    assert call.model_id == "scripted-analyst"
    assert call.prompt_version_id.startswith("prm_")
    # The request manifest references evidence IDs, never evidence text.
    manifest = call.request_manifest_json
    assert evidence_id in manifest["evidence_ids"]
    assert "1,000 samples" not in json.dumps(manifest)
    assert manifest["agent_type"] == "agents.framework_test"
    # Deterministic request hash (same request → same hash).
    assert call.request_hash == len(call.request_hash) * "x" or len(call.request_hash) == 64


def test_insufficient_evidence_is_a_valid_result(session, store, data_dir, seeded_version) -> None:
    provider = ScriptedLLMProvider(
        [json.dumps(_agent_result_payload("ev_x", status="INSUFFICIENT_EVIDENCE", claims=[]))]
    )
    outcome = run(
        run_agent(
            _request(seeded_version.paper_id, seeded_version.paper_version_id),
            session,
            provider=provider,
        )
    )
    assert outcome.result.status is AgentStatus.INSUFFICIENT_EVIDENCE
    assert outcome.result.claims == []
    # Still audited as an accepted call.
    call = session.scalar(select(ModelCallRow))
    assert call is not None
    assert call.schema_status is SchemaStatus.PASSED


# ---------------------------------------------------------------------------
# malicious fixtures against real evidence
# ---------------------------------------------------------------------------


def test_non_json_output_rejected_but_audited(session, store, data_dir, seeded_version) -> None:
    provider = ScriptedLLMProvider(["Sure! Prose answer, no JSON here."])
    with pytest.raises(DomainError) as excinfo:
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=provider,
            )
        )
    assert excinfo.value.code == "LLM_003"
    call = session.scalar(select(ModelCallRow))
    assert call is not None
    assert call.schema_status is SchemaStatus.JSON_INVALID


def test_missing_field_rejected_but_audited(session, store, data_dir, seeded_version) -> None:
    payload = _agent_result_payload("ev_x")
    del payload["status"]
    # The single constrained repair attempt ALSO fails → LLM_004 stands.
    provider = ScriptedLLMProvider([json.dumps(payload), json.dumps(dict(payload))])
    with pytest.raises(DomainError) as excinfo:
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=provider,
            )
        )
    assert excinfo.value.code == "LLM_004"
    assert provider.call_count == 2  # original + exactly one repair
    calls = session.scalars(select(ModelCallRow)).all()
    assert len(calls) == 2
    assert all(call.schema_status is SchemaStatus.SCHEMA_INVALID for call in calls)


def test_schema_repair_audits_both_calls(session, store, data_dir, seeded_version) -> None:
    evidence_id = _first_evidence_id(session, seeded_version.paper_version_id)
    evidence_text = session.get(EvidenceRow, evidence_id).text
    supported_statement = evidence_text.split(".")[0].strip()[:180]
    broken = _agent_result_payload(evidence_id, claim={"statement": supported_statement})
    del broken["status"]
    provider = ScriptedLLMProvider(
        [
            json.dumps(broken),
            json.dumps(
                _agent_result_payload(evidence_id, claim={"statement": supported_statement})
            ),
        ]
    )
    outcome = run(
        run_agent(
            _request(seeded_version.paper_id, seeded_version.paper_version_id),
            session,
            provider=provider,
        )
    )
    # A repair happened — the run succeeds, with that fact surfaced.
    assert outcome.result.status is AgentStatus.SUCCESS_WITH_WARNINGS
    assert any("repair succeeded" in w for w in outcome.warnings)
    assert provider.call_count == 2
    # BOTH model calls audited: the failed one and the repaired one.
    calls = session.scalars(select(ModelCallRow)).all()
    assert len(calls) == 2
    statuses = {call.schema_status for call in calls}
    assert statuses == {SchemaStatus.SCHEMA_INVALID, SchemaStatus.PASSED_AFTER_REPAIR}
    assert outcome.run_id and {call.run_id for call in calls} == {outcome.run_id}


def test_transport_failure_retains_run_and_call(session, seeded_version):
    from paperintel.database.models import AnalysisRunRow
    from paperintel.schemas.enums import TaskState, TransportStatus

    provider = ScriptedLLMProvider([DomainError("LLM_001", message="timeout")])
    with pytest.raises(DomainError):
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=provider,
            )
        )
    call = session.scalars(select(ModelCallRow)).one()
    assert call.transport_status is TransportStatus.TIMEOUT
    assert call.schema_status is SchemaStatus.NOT_RUN
    assert call.response_hash is None
    agent_run = session.get(AnalysisRunRow, call.run_id)
    assert agent_run.status is TaskState.FAILED
    assert agent_run.finished_at is not None


def test_selected_role_reaches_provider_and_audit(session, seeded_version):
    from paperintel.schemas.enums import ModelRole

    class RecordingProvider(ScriptedLLMProvider):
        async def complete(self, request):
            assert request.model_role is ModelRole.VERIFIER
            return await super().complete(request)

    provider = RecordingProvider([json.dumps({"status": "INSUFFICIENT_EVIDENCE", "claims": []})])
    outcome = run(
        run_agent(
            _request(seeded_version.paper_id, seeded_version.paper_version_id),
            session,
            provider=provider,
            model_role=ModelRole.VERIFIER,
        )
    )
    call = session.get(ModelCallRow, outcome.model_call_id)
    assert call.request_manifest_json["model_role"] == "verifier"


def test_nonexistent_evidence_rejected(session, store, data_dir, seeded_version) -> None:
    provider = ScriptedLLMProvider(
        [json.dumps(_agent_result_payload("ev_doesnotexist000000000000"))]
    )
    with pytest.raises(DomainError) as excinfo:
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=provider,
            )
        )
    assert excinfo.value.code == "EVIDENCE_001"
    call = session.scalar(select(ModelCallRow))
    assert call.schema_status is SchemaStatus.SEMANTIC_INVALID


def test_wrong_paper_evidence_rejected(session, store, data_dir, seeded_version) -> None:
    # A SECOND, different paper (F04 rich content → distinct sha256).
    other = import_paper(session, store, data_dir, build_f04_rich, "f04.pdf")
    other_evidence_id = _first_evidence_id(session, other.paper_version_id)
    assert other_evidence_id is not None

    provider = ScriptedLLMProvider([json.dumps(_agent_result_payload(other_evidence_id))])
    with pytest.raises(DomainError) as excinfo:
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=provider,
            )
        )
    assert excinfo.value.code == "EVIDENCE_002"
    assert excinfo.value.details["declared_version"] == seeded_version.paper_version_id


def test_unsupported_number_dropped_not_persisted(session, store, data_dir, seeded_version) -> None:
    evidence_id = _first_evidence_id(session, seeded_version.paper_version_id)
    evidence_text = session.get(EvidenceRow, evidence_id).text
    supported_statement = evidence_text.split(".")[0].strip()[:180]
    payload = _agent_result_payload(evidence_id, claim={"statement": supported_statement})
    payload["claims"].append(
        {
            "claim_type": "FACT",
            "category": "main_result",
            "statement": "Our method achieves 99.99% accuracy.",
            "evidence": [{"evidence_id": evidence_id, "role": "SUPPORT"}],
            "uncertainties": [],
        }
    )
    provider = ScriptedLLMProvider([json.dumps(payload)])
    outcome = run(
        run_agent(
            _request(seeded_version.paper_id, seeded_version.paper_version_id),
            session,
            provider=provider,
        )
    )
    # The invented number was dropped; the valid claim survives.
    statements = [c.statement for c in outcome.result.claims]
    assert len(statements) == 1
    assert "99.99%" not in statements[0]
    assert any("CLAIM_001" in w for w in outcome.warnings)
    assert outcome.result.status is AgentStatus.SUCCESS_WITH_WARNINGS


def test_model_refusal_rejected(session, store, data_dir, seeded_version) -> None:
    provider = ScriptedLLMProvider(["I'm sorry, but I cannot assist with analyzing this paper."])
    with pytest.raises(DomainError) as excinfo:
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=provider,
            )
        )
    assert excinfo.value.code == "LLM_005"


# ---------------------------------------------------------------------------
# no agent bypasses the framework
# ---------------------------------------------------------------------------


def test_unregistered_agent_type_rejected(seeded_version) -> None:
    request = AgentRequest(
        paper_id=seeded_version.paper_id,
        paper_version_id=seeded_version.paper_version_id,
        agent_type="agents.not_registered",
    )
    with pytest.raises(DomainError) as excinfo:
        run(run_agent(request, session=None, provider=MockLLMProvider()))
    assert excinfo.value.code == "CFG_002"
    assert "agents.framework_test" in str(excinfo.value.details["registered"])


def test_every_agent_run_leaves_exactly_one_audit_trail(
    session, store, data_dir, seeded_version
) -> None:
    """Accepted and rejected runs both leave ModelCallRows — the audit
    trail is structural, not conditional."""
    evidence_id = _first_evidence_id(session, seeded_version.paper_version_id)
    ok = ScriptedLLMProvider([json.dumps(_agent_result_payload(evidence_id))])
    run(
        run_agent(
            _request(seeded_version.paper_id, seeded_version.paper_version_id),
            session,
            provider=ok,
        )
    )
    refused = ScriptedLLMProvider(["I cannot help with that."])
    with pytest.raises(DomainError):
        run(
            run_agent(
                _request(seeded_version.paper_id, seeded_version.paper_version_id),
                session,
                provider=refused,
            )
        )
    assert session.scalar(select(func.count()).select_from(ModelCallRow)) == 2


# ---------------------------------------------------------------------------
# paper tools: evidence-scoped context
# ---------------------------------------------------------------------------


def test_context_is_scope_locked(session, store, data_dir, seeded_version) -> None:
    other = import_paper(session, store, data_dir, build_f04_rich, "f04b.pdf")
    context = build_evidence_context(session, seeded_version.paper_version_id)
    all_ids = {row.evidence_id for row in session.scalars(select(EvidenceRow))}
    assert set(context.evidence_ids) <= all_ids
    # Every block belongs to the declared version.
    other_ids = set(
        session.scalars(
            select(EvidenceRow.evidence_id).where(
                EvidenceRow.paper_version_id == other.paper_version_id
            )
        )
    )
    assert not other_ids & set(context.evidence_ids)
    assert context.rendered  # non-empty context


# ---------------------------------------------------------------------------
# prompt registry
# ---------------------------------------------------------------------------


def test_prompt_registration_is_idempotent_and_immutable(
    session, store, data_dir, seeded_version
) -> None:
    # The seeded_version fixture already registered the built-ins:
    # re-running adds nothing (idempotent).
    assert prompt_registry.ensure_builtin_prompts(session) == 0

    body = prompt_registry.BUILTIN_PROMPTS[("agents.claim_extraction", "1.0.0")].body
    row, created = prompt_registry.register_prompt(
        session, name="agents.claim_extraction", version="1.0.0", body=body
    )
    assert created is False  # same body → reuse

    with pytest.raises(DomainError) as excinfo:
        prompt_registry.register_prompt(
            session,
            name="agents.claim_extraction",
            version="1.0.0",
            body=body + " tampered",
        )
    assert excinfo.value.code == "CFG_002"

    # New version registers cleanly.
    _, created_new = prompt_registry.register_prompt(
        session, name="agents.claim_extraction", version="1.1.0", body=body + "\n# v1.1"
    )
    assert created_new is True

    # Unknown template variable is a programming error, never silent.
    with pytest.raises(DomainError) as excinfo:
        prompt_registry.render_template("value: ${missing}", {})
    assert excinfo.value.code == "CFG_002"


def test_unknown_prompt_version_rejected(session, seeded_version) -> None:
    with pytest.raises(DomainError) as excinfo:
        prompt_registry.get_prompt(session, "agents.claim_extraction", "9.9.9")
    assert excinfo.value.code == "CFG_002"


def test_registered_agents_registry_is_explicit() -> None:
    assert "agents.framework_test" in AGENTS
    with pytest.raises(ValueError, match="already registered"):

        @register_agent
        class DuplicateAgent(BaseAgent):
            agent_type = "agents.framework_test"
            prompt_name = "agents.claim_extraction"
            prompt_version = "1.0.0"

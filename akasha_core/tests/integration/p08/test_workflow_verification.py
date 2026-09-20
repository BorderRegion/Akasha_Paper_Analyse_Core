"""P08 workflow integration: VERIFIED + SYNTHESIZED stages.

The synthesizer must only ever see verified claims (doc 01 §9.13): after
the VERIFIED stage runs, supported claims exist and the synthesizer is
allowed to produce output; before that it structurally refuses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from tests.fixtures.canary import seed_canary_evidence

from paperintel.agents.base import run_agent
from paperintel.agents.prompts import ensure_builtin_prompts
from paperintel.database.models import ClaimRow, TaskRow, VerificationRow
from paperintel.schemas.agent import AgentRequest
from paperintel.schemas.enums import PipelineStage, ResourceTier, SupportState, TaskState
from paperintel.workflow import engine
from paperintel.workflow.celery_app import run_task_once
from paperintel.workflow.handlers import HANDLERS, TASK_SYNTHESIZE, TASK_VERIFY_CLAIMS


def _job_with_pipeline(session, imported, *, requested_tier: ResourceTier):
    from paperintel.triage.service import compute_triage

    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.ANALYZED,
    )
    compute_triage(session, paper_id=imported.paper_id, requested_tier=requested_tier)
    seed_canary_evidence(session, imported.paper_version_id)
    session.flush()
    return job


@pytest.mark.needs_db
def test_verify_stage_runs_tier_verifiers_and_updates_states(session, imported) -> None:
    """The VERIFIED stage verifies claims at the paper's TRIAGE tier and
    records one VerificationRow per (claim, verifier)."""
    from paperintel.knowledge.claims import persist_agent_result
    from paperintel.providers.mocks.llm import MockLLMProvider
    from paperintel.schemas.enums import LlmMockMode

    ensure_builtin_prompts(session)
    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    request = AgentRequest(
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        agent_type="agents.research_question",
    )
    provider = MockLLMProvider(mode=LlmMockMode.VALID)
    outcome = asyncio.run(run_agent(request, session, provider=provider))
    assert outcome.result.claims, "mock VALID produced no claims"
    persist_agent_result(session, request, outcome)
    session.flush()

    from paperintel.triage.service import compute_triage

    compute_triage(session, paper_id=imported.paper_id, requested_tier=ResourceTier.T3_DEEP)
    session.flush()

    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.ANALYZED,
    )
    task, _ = engine.enqueue_task(
        session,
        job,
        task_type=TASK_VERIFY_CLAIMS,
        module_id=TASK_VERIFY_CLAIMS,
        input_manifest={"paper_version_id": imported.paper_version_id},
        idempotency_key="verify-test",
    )
    session.commit()

    outcome = run_task_once(session, task.task_id)
    assert outcome["outcome"] == "succeeded", outcome
    session.commit()

    row = session.get(TaskRow, task.task_id)
    manifest = row.output_manifest
    assert manifest["effective_tier"] == "T3_DEEP"
    assert manifest["claims_verified"] >= 1
    assert manifest["states"]

    # The canary claims are supported by their own evidence → SUPPORTED.
    claims = session.scalars(
        select(ClaimRow).where(ClaimRow.paper_version_id == imported.paper_version_id)
    ).all()
    assert all(claim.support_state is SupportState.SUPPORTED for claim in claims)
    verifications = session.scalars(
        select(VerificationRow).where(
            VerificationRow.created_by_run_id == manifest["verification_run_id"]
        )
    ).all()
    assert len(verifications) == len(claims) * 9  # T3 = complete applicable set
    assert evidence_id  # silence unused warning; evidence was cited


@pytest.mark.needs_db
def test_synthesizer_activates_only_after_verification(session, imported) -> None:
    """Before verification: INSUFFICIENT_EVIDENCE with zero model calls.
    After a verification pass marks claims SUPPORTED: synthesis runs."""
    from paperintel.knowledge.claims import persist_agent_result
    from paperintel.providers.mocks.llm import MockLLMProvider
    from paperintel.schemas.enums import AgentStatus, LlmMockMode
    from paperintel.verification.service import run_verification

    ensure_builtin_prompts(session)
    seed_canary_evidence(session, imported.paper_version_id)
    request = AgentRequest(
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        agent_type="agents.research_question",
    )
    provider = MockLLMProvider(mode=LlmMockMode.VALID)
    persist_agent_result(
        session, request, asyncio.run(run_agent(request, session, provider=provider))
    )
    session.flush()

    synth_request = AgentRequest(
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        agent_type="agents.synthesizer",
    )
    synth_provider = MockLLMProvider(mode=LlmMockMode.VALID)

    # 1. UNVERIFIED claims exist → synthesizer refuses without a model call.
    before = asyncio.run(run_agent(synth_request, session, provider=synth_provider))
    assert before.result.status is AgentStatus.INSUFFICIENT_EVIDENCE
    assert before.model_call_id is None
    assert synth_provider.calls == []

    # 2. Verify → SUPPORTED claims exist → synthesizer runs.
    run_verification(session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T2_FULL)
    session.flush()
    assert all(
        claim.support_state is SupportState.SUPPORTED
        for claim in session.scalars(
            select(ClaimRow).where(ClaimRow.paper_version_id == imported.paper_version_id)
        )
    )

    after = asyncio.run(run_agent(synth_request, session, provider=synth_provider))
    assert after.model_call_id is not None
    assert len(synth_provider.calls) == 1

    # The synthesis prompt carried the VERIFIED claim text as its input.
    prompt = synth_provider.calls[0].request_messages[-1][1]
    assert "Verified claims" in prompt or "verified" in prompt.lower()
    assert "clm_" in prompt


@pytest.mark.needs_db
def test_synthesize_handler_persists_through_the_ledger(session, imported, providers_env) -> None:
    from paperintel.knowledge.claims import persist_agent_result
    from paperintel.providers.mocks.llm import MockLLMProvider
    from paperintel.schemas.enums import LlmMockMode
    from paperintel.verification.service import run_verification

    ensure_builtin_prompts(session)
    seed_canary_evidence(session, imported.paper_version_id)
    request = AgentRequest(
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        agent_type="agents.experiment",
    )
    persist_agent_result(
        session,
        request,
        asyncio.run(run_agent(request, session, provider=MockLLMProvider(mode=LlmMockMode.VALID))),
    )
    run_verification(session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T2_FULL)
    session.flush()

    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.VERIFIED,
    )
    task, _ = engine.enqueue_task(
        session,
        job,
        task_type=TASK_SYNTHESIZE,
        module_id=TASK_SYNTHESIZE,
        input_manifest={"paper_version_id": imported.paper_version_id},
        idempotency_key="synth-test",
    )
    session.commit()

    outcome = run_task_once(session, task.task_id)
    assert outcome["outcome"] == "succeeded", outcome
    session.commit()

    row = session.get(TaskRow, task.task_id)
    assert row.output_manifest["claims_created"] >= 1
    assert row.output_manifest["run_id"].startswith("run_")
    # A synthesis run is recorded with the synthesizer agent type.
    from paperintel.database.models import AnalysisRunRow

    run = session.get(AnalysisRunRow, row.output_manifest["run_id"])
    assert run.agent_type == "agents.synthesizer"


@pytest.mark.needs_db
def test_plan_marks_verified_and_synthesized_satisfied_after_runs(session, imported) -> None:
    """plan_job derives stage satisfaction from ACTUAL progress: after a
    verification run and a synthesis run exist, re-planning reports both
    stages satisfied instead of re-enqueueing them."""
    from paperintel.verification.service import run_verification

    job = _job_with_pipeline(session, imported, requested_tier=ResourceTier.T2_FULL)
    session.commit()

    plan_before = engine.plan_job(
        session,
        job,
        data_dir=str(Path(imported.report_path).parents[2]),
        content_sha256=Path(imported.report_path).stem,
        report_path=imported.report_path,
    )
    assert "VERIFIED" in {entry["stage"] for entry in plan_before["enqueued"]}
    session.commit()

    # A stage label or seeded evidence is not proof that analysis succeeded.
    analysis_task = session.scalar(select(TaskRow).where(
        TaskRow.job_id == job.job_id, TaskRow.task_type == "agents.run_suite"
    ))
    assert run_task_once(session, analysis_task.task_id)["outcome"] == "succeeded"

    # Complete actual verification and synthesis before testing satisfaction.
    run_verification(session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T2_FULL)
    session.flush()
    synthesis_task = session.scalar(select(TaskRow).where(
        TaskRow.job_id == job.job_id, TaskRow.task_type == TASK_SYNTHESIZE
    ))
    assert run_task_once(session, synthesis_task.task_id)["outcome"] == "succeeded"

    plan_after = engine.plan_job(
        session,
        job,
        data_dir=str(Path(imported.report_path).parents[2]),
        content_sha256=Path(imported.report_path).stem,
        report_path=imported.report_path,
    )
    assert "VERIFIED" in plan_after["satisfied"]
    assert "SYNTHESIZED" in plan_after["satisfied"]
    # ANALYZED stays satisfied too; no duplicate task was enqueued.
    assert "ANALYZED" in plan_after["satisfied"]
    verify_tasks = session.scalars(
        select(TaskRow).where(TaskRow.job_id == job.job_id, TaskRow.task_type == TASK_VERIFY_CLAIMS)
    ).all()
    assert len(verify_tasks) == 1


@pytest.mark.needs_db
def test_handler_registry_covers_p08_stages() -> None:
    """The doc 03 §8 stage names map to real handlers (no silent gaps)."""
    assert TASK_VERIFY_CLAIMS in HANDLERS
    assert TASK_SYNTHESIZE in HANDLERS
    assert TaskState.SUCCEEDED.value == "SUCCEEDED"
    assert json.dumps({"stage": PipelineStage.VERIFIED.value})

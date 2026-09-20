"""P07 workflow integration: TRIAGED + ANALYZED stages through the engine
and the CLI — the agent suite runs as real workflow tasks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from tests.fixtures.canary import seed_canary_evidence
from tests.fixtures.generators import build_f01_native
from typer.testing import CliRunner

from paperintel.cli.main import app
from paperintel.database.models import ClaimRow, TaskRow, TriageResultRow
from paperintel.schemas.enums import PipelineStage, TaskState
from paperintel.workflow.celery_app import run_task_once

runner = CliRunner()

REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture()
def cli_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE", str(REPO_ROOT / "config" / "providers.mock.yaml")
    )
    pdf = data_dir / "f01.pdf"
    pdf.write_bytes(build_f01_native())
    return data_dir


@pytest.mark.needs_db
def test_cli_rerun_analysis_stage_end_to_end(cli_env) -> None:
    """rerun --stage ANALYZED plans, runs triage + the agent suite, and
    lands claims in the ledger (mock provider, canary evidence)."""
    pdf = cli_env / "f01.pdf"
    imported = runner.invoke(app, ["import", str(pdf)])
    assert imported.exit_code == 0, imported.output
    paper = json.loads(imported.output)["imported"][0]
    version_id = paper["paper_version_id"]

    # Seed the canary evidence so the mock LLM's answers are supported
    # end-to-end (existence, scope, numeric support all real).
    from paperintel.config.settings import get_settings, reset_settings_cache
    from paperintel.database.base import (
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )

    reset_settings_cache()
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    with session_scope(factory) as session:
        seed_canary_evidence(session, version_id)
        session.commit()

    result = runner.invoke(app, ["rerun", paper["paper_id"], "--stage", "ANALYZED"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stage"] == "ANALYZED"
    assert payload["task_id"].startswith("tsk_")

    # Execute the queued pipeline tasks eagerly (triage first, then agents).
    with session_scope(factory) as session:
        from paperintel.workflow import engine as wf_engine

        job = session.scalars(select(TaskRow).where(TaskRow.task_id == payload["task_id"])).first()
        job_id = job.job_id
        outcomes = []
        while True:
            queued = session.scalars(
                select(TaskRow)
                .where(TaskRow.job_id == job_id, TaskRow.state == TaskState.QUEUED)
                .order_by(TaskRow.created_at)
            ).all()
            if not queued:
                break
            for task in queued:
                outcomes.append(run_task_once(session, task.task_id))
            session.commit()
        assert all(o["outcome"] == "succeeded" for o in outcomes), outcomes

        # Triage decision recorded (resource allocation, explainable).
        triage = session.scalar(select(TriageResultRow))
        assert triage is not None
        assert triage.paper_id == paper["paper_id"]
        assert triage.reason_codes

        # Claims landed with evidence links. The full pipeline (P08)
        # also ran the VERIFIED stage, so the claims carry verified
        # support states rather than staying UNVERIFIED.
        claims = session.scalars(select(ClaimRow)).all()
        assert claims, "agent suite produced no claims"
        assert all(c.paper_version_id == version_id for c in claims)
        assert all(
            c.support_state.value in ("SUPPORTED", "PARTIALLY_SUPPORTED", "UNVERIFIED")
            for c in claims
        )
        assert any(c.support_state.value == "SUPPORTED" for c in claims)

        job_row = session.scalars(select(TaskRow).where(TaskRow.job_id == job_id)).first()
        progress = wf_engine.job_progress(session, job_id)
        # The job ran the whole implemented pipeline (through P09 search
        # indexing); only CORPUS_READY (P12) remains future-owned.
        assert progress["current_stage"] == PipelineStage.SEARCH_INDEXED.value

        # The mocked provider's two canary facts are in the ledger.
        statements = " ".join(c.statement for c in claims)
        assert "1,000 samples" in statements
        assert "82.5% accuracy" in statements
        assert job_row is not None


@pytest.mark.needs_db
def test_agent_failure_fails_task_loudly(cli_env, monkeypatch) -> None:
    """A provider whose answers cite evidence OUT OF SCOPE fails the
    ANALYZED task with EVIDENCE_002 — never a silent skip. The provider is
    chosen through CONFIG (a hostile-only providers file), not a code
    patch."""
    pdf = cli_env / "f01.pdf"
    imported = runner.invoke(app, ["import", str(pdf)])
    paper = json.loads(imported.output)["imported"][0]

    # Hostile-only provider config: INVENTED_NUMBER cites the canary
    # evidence ID — seeded for the FIRST test's version (module DB), so it
    # exists but is out of scope for THIS version → EVIDENCE_002.
    hostile_config = cli_env / "providers_hostile.yaml"
    hostile_config.write_text(
        'spec_version: "1.0.0"\n'
        "providers:\n"
        "  hostile_llm:\n"
        "    kind: mock_llm\n"
        "    options:\n"
        '      mode: "INVENTED_NUMBER"\n'
        "      seed: 1000\n"
    )
    monkeypatch.setenv("PAPERINTEL_PROVIDERS_FILE", str(hostile_config))

    from paperintel.config.settings import get_settings, reset_settings_cache
    from paperintel.database.base import (
        create_engine_from_settings,
        create_session_factory,
        session_scope,
    )

    reset_settings_cache()
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)

    result = runner.invoke(app, ["rerun", paper["paper_id"], "--stage", "ANALYZED"])
    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["task_id"]

    with session_scope(factory) as session:
        claims_before = session.scalar(select(func.count()).select_from(ClaimRow))
        outcome = run_task_once(session, task_id)
        session.commit()
        assert outcome["outcome"] == "failed"
        assert outcome["code"] == "EVIDENCE_002"
        row = session.get(TaskRow, task_id)
        assert row.error_code == "EVIDENCE_002"
        # No claims persisted from a failed run (module DB is shared with
        # the previous test — assert no NEW claims).
        claims_after = session.scalar(select(func.count()).select_from(ClaimRow))
        assert claims_after == claims_before

"""P05 CLI tests: paperctl jobs/job/rerun/replay/trace (disposable DB).

Isolation notes (learned the hard way):
- The autouse per-test env scrub wipes DATABASE_URL/PAPERINTEL_DATA_DIR,
  so env application MUST be function-scoped (a module-scoped setenv is
  erased before the first test runs — CLI invocations then silently target
  the dev database).
- The module shares ONE database, ONE data_dir, and ONE PDF file:
  build_f01_native() embeds generation timestamps, so per-test rebuilds
  are legitimately different content (different sha256 → different
  versions). A shared file exercises dedup exactly like production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.fixtures.generators import build_f01_native
from typer.testing import CliRunner

from paperintel.cli.main import app

runner = CliRunner()


@pytest.fixture(scope="module")
def shared(test_db_url: str, tmp_path_factory) -> dict:
    """Module-shared resources: test DB url, store data_dir, one PDF."""
    data_dir = tmp_path_factory.mktemp("cli-data")
    pdf = data_dir / "f01.pdf"
    pdf.write_bytes(build_f01_native())
    return {
        "database_url": test_db_url,
        "data_dir": data_dir,
        "pdf": pdf,
    }


@pytest.fixture()
def cli_env(shared: dict, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Function-scoped env application (survives the autouse scrub)."""
    monkeypatch.setenv("DATABASE_URL", shared["database_url"])
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(shared["data_dir"]))
    return shared["data_dir"]


def _import(shared: dict) -> dict:
    result = runner.invoke(app, ["import", str(shared["pdf"])])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["imported"][0]


@pytest.mark.needs_db
def test_cli_rerun_creates_job_and_replays_stage(cli_env, shared) -> None:
    imported = _import(shared)
    result = runner.invoke(app, ["rerun", imported["paper_id"], "--stage", "EVIDENCE_INDEXED"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["job_planned_now"] is True
    assert payload["stage"] == "EVIDENCE_INDEXED"
    assert payload["task_id"].startswith("tsk_")
    assert payload["task_state"] == "QUEUED"


@pytest.mark.needs_db
def test_cli_jobs_and_job_detail(cli_env, shared) -> None:
    imported = _import(shared)  # dedup → same paper/version as before
    runner.invoke(app, ["rerun", imported["paper_id"], "--stage", "STRUCTURED"])
    listing = runner.invoke(app, ["jobs"])
    assert listing.exit_code == 0, listing.output
    jobs = json.loads(listing.output)
    assert jobs["count"] == 1  # one job for the one shared version
    job_entry = jobs["jobs"][0]
    assert job_entry["paper_id"] == imported["paper_id"]
    assert job_entry["tasks_total"] > 0

    detail = runner.invoke(app, ["job", job_entry["job_id"]])
    assert detail.exit_code == 0, detail.output
    payload = json.loads(detail.output)
    assert payload["job"]["job_id"] == job_entry["job_id"]
    assert payload["progress"]["tasks_by_state"]
    task_types = {t["task_type"] for t in payload["tasks"]}
    # Import already persisted evidence → stages satisfied; the replayed
    # STRUCTURED task is queued; future stages are SKIPPED.
    assert "structure.reconstruct_sections" in task_types
    assert any(t["state"] == "SKIPPED" for t in payload["tasks"])

    filtered = runner.invoke(app, ["jobs", "--state", "RUNNING"])
    assert filtered.exit_code == 0, filtered.output
    assert json.loads(filtered.output)["count"] >= 0
    bad = runner.invoke(app, ["jobs", "--state", "NOT_A_STATE"])
    assert bad.exit_code == 1


@pytest.mark.needs_db
def test_cli_replay_task_eager(cli_env, shared) -> None:
    imported = _import(shared)
    runner.invoke(app, ["rerun", imported["paper_id"], "--stage", "STRUCTURED"])
    listing = runner.invoke(app, ["jobs"])
    job_id = json.loads(listing.output)["jobs"][0]["job_id"]
    detail = runner.invoke(app, ["job", job_id])
    tasks = json.loads(detail.output)["tasks"]
    queued = next(t for t in tasks if t["state"] == "QUEUED")

    result = runner.invoke(app, ["replay", queued["task_id"], "--eager"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["replayed"]["outcome"] == "succeeded"

    # Terminal tasks refuse replay.
    terminal = runner.invoke(app, ["replay", queued["task_id"], "--eager"])
    assert terminal.exit_code == 1
    text = terminal.stderr if terminal.stderr_bytes else terminal.output
    assert json.loads(text)["error"]["code"] == "INTERNAL_002"


@pytest.mark.needs_db
def test_cli_trace_follows_job_lifecycle(cli_env, shared) -> None:
    imported = _import(shared)
    runner.invoke(app, ["rerun", imported["paper_id"], "--stage", "STRUCTURED"])
    listing = runner.invoke(app, ["jobs"])
    trace_id = json.loads(listing.output)["jobs"][0]["trace_id"]

    result = runner.invoke(app, ["trace", trace_id])
    assert result.exit_code == 0, result.output
    events = json.loads(result.output)["events"]
    kinds = [e["kind"] for e in events]
    assert "job.created" in kinds
    assert "task.enqueued" in kinds
    assert "task.skipped" in kinds
    assert "task.replayed" in kinds

    unknown = runner.invoke(app, ["trace", "trc_UNKNOWN"])
    assert unknown.exit_code == 1


@pytest.mark.needs_db
def test_cli_rerun_unknown_inputs_fail_loudly(cli_env, shared) -> None:
    _import(shared)
    paper = runner.invoke(app, ["rerun", "pap_01UNKNOWN000000000000000000"])
    assert paper.exit_code == 1
    stage = runner.invoke(app, ["rerun", "pap_x", "--stage", "NOT_A_STAGE"])
    assert stage.exit_code == 1
    text = stage.stderr if stage.stderr_bytes else stage.output
    assert json.loads(text)["error"]["code"] == "CFG_002"

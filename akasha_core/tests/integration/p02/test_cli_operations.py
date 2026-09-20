"""CLI operations commands that require live services (doctor, db status)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from paperintel.cli.main import app

runner = CliRunner()


@pytest.mark.needs_db
def test_db_status_reports_healthy(test_db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    result = runner.invoke(app, ["db", "status"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["module_id"] == "database.core"
    assert payload["state"] == "HEALTHY"
    assert payload["checks"]["alembic_revision_matches_head"] == "PASS"
    assert payload["checks"]["pgvector_extension"] == "PASS"
    assert payload["head_revision"]


@pytest.mark.needs_db
def test_db_unknown_subcommand_exits_2() -> None:
    result = runner.invoke(app, ["db", "frobnicate"])
    assert result.exit_code == 2
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_002"


@pytest.mark.needs_db
@pytest.mark.needs_redis
def test_doctor_all_green(tmp_path, monkeypatch, test_db_url, repo_root) -> None:
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_PROVIDERS_FILE", str(repo_root / "config/providers.mock.yaml"))
    monkeypatch.setenv("PAPERINTEL_TASKS_EAGER", "1")
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(tmp_path / "data"))
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["checks"]["config"] == "PASS"
    assert payload["checks"]["database"] == "HEALTHY"
    assert payload["checks"]["redis"] == "PASS"
    assert payload["checks"]["object_store"] in {"HEALTHY", "DEGRADED"}
    assert payload["problems"] == []


@pytest.mark.needs_db
def test_doctor_reports_redis_failure_loudly(tmp_path, monkeypatch) -> None:
    """Pointing redis at a dead port must surface as a problem, never as a
    silent pass (spec doc 06 §16)."""
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["checks"]["redis"].startswith("FAIL")
    assert "redis" in payload["problems"]

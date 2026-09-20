"""P10 CLI tests: paperctl disk and gc (dry-run default, explicit execute)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paperintel.cli.main import app

runner = CliRunner()


@pytest.fixture()
def cli_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    (data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)
    (data_dir / "objects").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    return data_dir


def _age(path: Path, hours: float) -> None:
    old = time.time() - hours * 3600
    os.utime(path, (old, old))


@pytest.mark.needs_db
def test_cli_disk_reports_breakdown(cli_env: Path) -> None:
    (cli_env / "objects" / "paper.pdf").write_bytes(b"p" * 2048)
    (cli_env / "cache" / "report.json").write_bytes(b"c" * 512)
    (cli_env / "tmp" / "scratch.png").write_bytes(b"t" * 256)

    result = runner.invoke(app, ["disk"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    by_class = {entry["retention_class"]: entry["bytes"] for entry in payload["by_retention_class"]}
    assert by_class["KEEP"] == 2048
    assert by_class["CACHE"] == 512
    assert by_class["TEMP"] == 256
    assert payload["store_bytes"] == 2816
    assert payload["disk"]["level"] in ("OK", "WARNING", "CRITICAL")
    assert "critical_free_percent" in payload["policy"]


@pytest.mark.needs_db
def test_cli_gc_explicit_dry_run(cli_env: Path) -> None:
    stale = cli_env / "tmp" / "old-render.png"
    stale.write_bytes(b"x" * 4096)
    _age(stale, hours=72)

    result = runner.invoke(app, ["gc", "--dry-run"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["reclaimable_bytes"] >= 4096
    assert any("old-render.png" in entry["object"] for entry in payload["candidates"])
    # A dry run never deletes.
    assert stale.exists()
    assert payload["removed"] == []
    # The doc 07 §7 reporting fields are present for every candidate.
    for entry in payload["candidates"]:
        assert {"object", "size_bytes", "retention_class", "reason", "last_reference"} <= set(entry)


@pytest.mark.needs_db
def test_cli_gc_execute_removes_candidates(cli_env: Path) -> None:
    stale = cli_env / "tmp" / "expired.png"
    stale.write_bytes(b"y" * 1024)
    _age(stale, hours=99)
    canonical = cli_env / "objects" / "canonical.pdf"
    canonical.write_bytes(b"k" * 2048)

    result = runner.invoke(app, ["gc"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert not stale.exists()
    assert canonical.exists()  # canonical content is never collected
    assert payload["reclaimed_bytes"] >= 1024
    assert payload["errors"] == []


@pytest.mark.needs_db
def test_cli_gc_rejects_conflicting_flags(cli_env: Path) -> None:
    result = runner.invoke(app, ["gc", "--dry-run", "--execute"])
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_002"

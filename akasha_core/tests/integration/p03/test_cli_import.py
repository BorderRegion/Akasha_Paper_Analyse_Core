"""P03 CLI import tests against a disposable database.

The CLI runs in-process via CliRunner; DATABASE_URL is monkeypatched to the
module-scoped disposable test database (never the dev DB). Imports COMMIT
there (real session_scope semantics); the database is dropped at teardown.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.fixtures.generators import (
    build_f01_native,
    build_f02_scanned,
    build_f05_corrupt,
    build_f06_truth,
)
from typer.testing import CliRunner

from paperintel.cli.main import app

runner = CliRunner()


@pytest.fixture()
def cli_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    data_dir = tmp_path / "cli-data"
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def write_pdf(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


@pytest.mark.needs_db
def test_cli_import_and_dedup_roundtrip(cli_env, tmp_path) -> None:
    path = write_pdf(tmp_path, "f01.pdf", build_f01_native())
    first = runner.invoke(app, ["import", str(path)])
    assert first.exit_code == 0, first.output
    payload = json.loads(first.output)
    assert payload["ok"] is True
    imported = payload["imported"][0]
    assert imported["paper_id"].startswith("pap_")
    assert imported["paper_version_id"].startswith("pver_")
    assert imported["deduplicated"] is False
    assert imported["page_count"] == 2
    assert imported["quality_state"] == "GOOD"
    assert Path(imported["report_path"]).is_file()

    second = runner.invoke(app, ["import", str(path)])
    assert second.exit_code == 0, second.output
    payload2 = json.loads(second.output)
    assert payload2["imported"][0]["deduplicated"] is True
    assert payload2["imported"][0]["paper_version_id"] == imported["paper_version_id"]


@pytest.mark.needs_db
def test_cli_import_with_mock_ocr_provider(cli_env, tmp_path) -> None:
    providers = tmp_path / "providers.yaml"
    providers.write_text(
        """
spec_version: "1.0.0"
providers:
  mock_ocr:
    kind: mock_ocr
""",
        encoding="utf-8",
    )
    path = write_pdf(tmp_path, "f02.pdf", build_f02_scanned())
    result = runner.invoke(app, ["import", str(path), "--providers", str(providers)])
    assert result.exit_code == 0, result.output
    imported = json.loads(result.output)["imported"][0]
    assert imported["source_method_counts"].get("OCR", 0) > 0


@pytest.mark.needs_db
def test_cli_import_scanned_without_ocr_fails_loudly(cli_env, tmp_path) -> None:
    path = write_pdf(tmp_path, "f02b.pdf", build_f02_scanned())
    result = runner.invoke(app, ["import", str(path), "--no-ocr"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["failed"][0]["error"]["code"] == "EXTRACT_001"


@pytest.mark.needs_db
def test_cli_import_corrupt_file_reports_pdf001(cli_env, tmp_path) -> None:
    path = write_pdf(tmp_path, "corrupt.pdf", build_f05_corrupt())
    result = runner.invoke(app, ["import", str(path)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["failed"][0]["error"]["code"] == "PDF_001"
    # No secret material or stack traces leak into the envelope.
    assert "Traceback" not in result.output


@pytest.mark.needs_db
def test_cli_import_mixed_success_and_failure_continues(cli_env, tmp_path) -> None:
    good = write_pdf(tmp_path, "good.pdf", build_f06_truth())
    bad = write_pdf(tmp_path, "bad.pdf", build_f05_corrupt())
    result = runner.invoke(app, ["import", str(good), str(bad)])
    assert result.exit_code == 1  # one failure → non-zero overall
    payload = json.loads(result.output)
    assert len(payload["imported"]) == 1  # the good file still imported
    assert payload["imported"][0]["quality_state"] == "GOOD"
    assert payload["failed"][0]["file"] == "bad.pdf"


@pytest.mark.needs_db
def test_cli_import_identity_options_single_file_only(cli_env, tmp_path) -> None:
    a = write_pdf(tmp_path, "a.pdf", build_f01_native())
    b = write_pdf(tmp_path, "b.pdf", build_f06_truth())
    result = runner.invoke(app, ["import", str(a), str(b), "--title", "Clash"])
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_002"

    single = runner.invoke(
        app,
        ["import", str(a), "--title", "CLI Titled Work", "--doi", "10.999/cli.test"],
    )
    assert single.exit_code == 0, single.output
    imported = json.loads(single.output)["imported"][0]
    assert imported["version_label"] == "v1"


@pytest.mark.needs_db
def test_cli_import_directory_glob(cli_env, tmp_path) -> None:
    folder = tmp_path / "papers"
    folder.mkdir()
    (folder / "one.pdf").write_bytes(build_f01_native())
    (folder / "two.pdf").write_bytes(build_f06_truth())
    (folder / "notes.txt").write_text("ignore me", encoding="utf-8")
    result = runner.invoke(app, ["import", str(folder)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["imported"]) == 2

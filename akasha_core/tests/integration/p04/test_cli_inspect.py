"""P04 CLI inspect tests: paperctl inspect <paper_id> (disposable DB)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.fixtures.generators import build_f01_native
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


def _import(tmp_path: Path) -> dict:
    pdf = tmp_path / "f01.pdf"
    pdf.write_bytes(build_f01_native())
    result = runner.invoke(app, ["import", str(pdf)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["imported"][0]


@pytest.mark.needs_db
def test_cli_inspect_snapshot(cli_env, tmp_path) -> None:
    imported = _import(tmp_path)
    result = runner.invoke(app, ["inspect", imported["paper_id"]])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["paper"]["paper_id"] == imported["paper_id"]
    assert payload["paper"]["canonical_title"]
    assert payload["version"]["paper_version_id"] == imported["paper_version_id"]
    assert payload["version"]["page_count"] == 2
    assert len(payload["versions"]) == 1

    sections = payload["sections"]
    assert sections
    assert payload["section_counts"]["total"] >= 1
    assert "OTHER" in payload["section_counts"]["by_class"]
    assert "REFERENCES" in payload["section_counts"]["by_class"]

    evidence = payload["evidence"]
    assert evidence["evidence_total"] > 0
    assert evidence["evidence_active"] == evidence["evidence_total"]
    assert evidence["by_type"].get("PARAGRAPH", 0) > 0
    assert payload["evidence_entries"]  # non-empty default listing
    assert payload["pipeline_version"]


@pytest.mark.needs_db
def test_cli_inspect_pending_fields_visible(cli_env, tmp_path) -> None:
    imported = _import(tmp_path)
    result = runner.invoke(app, ["inspect", imported["paper_id"]])
    payload = json.loads(result.output)
    pending = payload["pending"]
    assert pending["claims"] == "P07"
    assert pending["search_index"] == "P09"


@pytest.mark.needs_db
def test_cli_inspect_evidence_resolution(cli_env, tmp_path) -> None:
    imported = _import(tmp_path)
    listing = runner.invoke(
        app,
        ["inspect", imported["paper_id"], "--limit", "3"],
    )
    payload = json.loads(listing.output)
    evidence_id = payload["evidence_entries"][0]["evidence_id"]

    resolved = runner.invoke(app, ["inspect", imported["paper_id"], "--evidence", evidence_id])
    assert resolved.exit_code == 0, resolved.output
    entry = json.loads(resolved.output)["evidence_entry"]
    assert entry["evidence_id"] == evidence_id
    assert entry["text"]
    assert entry["source_method"] == "PDF_NATIVE"

    unknown = runner.invoke(app, ["inspect", imported["paper_id"], "--evidence", "ev_NOPE"])
    assert unknown.exit_code == 1
    assert json.loads(unknown.stderr if unknown.stderr_bytes else unknown.output)["error"][
        "code"
    ] == ("EVIDENCE_001")


@pytest.mark.needs_db
def test_cli_inspect_unknown_paper_fails_loudly(cli_env, tmp_path) -> None:
    result = runner.invoke(app, ["inspect", "pap_01UNKNOWN000000000000000000"])
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_002"


@pytest.mark.needs_db
def test_cli_inspect_version_selection(cli_env, tmp_path) -> None:
    imported = _import(tmp_path)
    # Select by label.
    by_label = runner.invoke(app, ["inspect", imported["paper_id"], "--version", "v1"])
    assert by_label.exit_code == 0, by_label.output
    assert json.loads(by_label.output)["version"]["version_label"] == "v1"
    # Unknown label → CFG_002.
    missing = runner.invoke(app, ["inspect", imported["paper_id"], "--version", "v99"])
    assert missing.exit_code == 1

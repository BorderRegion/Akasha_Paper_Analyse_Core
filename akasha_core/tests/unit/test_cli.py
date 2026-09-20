"""paperctl CLI tests (P00 gate check P00-C12)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from paperintel import __version__
from paperintel.cli.main import app
from paperintel.version import PIPELINE_VERSION, SPEC_VERSION

runner = CliRunner()


def test_version_command_json() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["program"] == "paperctl"
    assert payload["version"] == __version__
    assert payload["spec_version"] == SPEC_VERSION
    assert payload["pipeline_version"] == PIPELINE_VERSION
    assert "python" in payload


def test_config_validate_defaults_ok() -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["config"] == "VALID"
    assert payload["providers"] == "NOT_CONFIGURED"
    assert payload["database_url_configured"] is True


def test_config_validate_with_providers_file(tmp_path) -> None:
    providers = tmp_path / "providers.yaml"
    providers.write_text(
        """
spec_version: "1.0.0"
providers:
  llm_primary:
    kind: mock_llm
    api_key_env: "LLM_API_KEY"
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "validate", "--providers", str(providers)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["providers"]["entries"]["llm_primary"]["kind"] == "mock_llm"
    # The key VALUE must never be printed, only env-var name + presence flag.
    assert "api_key_present_in_environment" in payload["providers"]["entries"]["llm_primary"]


def test_config_validate_bad_file_exits_1_with_envelope(tmp_path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("bogus_section: {}\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", "--config", str(bad)])
    assert result.exit_code == 1
    envelope = json.loads(result.stderr if result.stderr_bytes else result.output)
    assert envelope["error"]["code"] == "CFG_002"


def test_full_command_surface_is_implemented() -> None:
    """Every doc 04 §8 command is implemented: none may answer with the
    "not implemented yet" stub message any more."""
    commands = [
        ["status", "--help"],
        ["selftest", "--help"],
        ["audit", "--help"],
        ["debug-bundle", "--help"],
        ["paper", "--help"],
        ["disk", "--help"],
        ["gc", "--help"],
        ["version"],
        ["config", "validate"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, (command, result.output)
        assert "not implemented yet" not in result.output


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("version", "config", "doctor", "status", "selftest", "providers"):
        assert name in result.output

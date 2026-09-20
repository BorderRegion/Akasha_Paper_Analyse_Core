"""CLI provider/ops command tests (offline parts; DB-backed parts live in
tests/integration/p02/)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from paperintel.cli.main import app

runner = CliRunner()

MOCK_CONFIG = "config/providers.mock.yaml"


def test_providers_status_offline_with_mocks() -> None:
    result = runner.invoke(app, ["providers", "status", "--providers", MOCK_CONFIG])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload["providers"]) == {
        "mock_embed",
        "mock_llm",
        "mock_llm_hostile",
        "mock_metadata",
        "mock_ocr",
        "object_store",
    }
    llm = payload["providers"]["mock_llm"]
    assert llm["family"] == "LLMProvider"
    assert llm["provider_id"] == "prv_mock_llm"
    assert llm["circuit_breaker_state"] == "CLOSED"
    # Doc 04 §6 exposure fields are present for LLM providers.
    for field_name in (
        "availability",
        "configured_concurrency",
        "active_concurrency",
        "rate_limit_events",
        "canary_state",
        "circuit_breaker_state",
    ):
        assert field_name in llm
    # Object store reports health-record style payload.
    assert payload["providers"]["object_store"]["kind"] == "health"
    assert "fingerprint_sha256" in payload


def test_providers_status_never_prints_secrets(tmp_path, monkeypatch) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
spec_version: "1.0.0"
providers:
  llm_primary:
    kind: openai_compatible
    base_url: "https://api.example.invalid/v1"
    api_key_env: "CLI_TEST_LLM_KEY"
    models:
      analyst:
        model: "test-model"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLI_TEST_LLM_KEY", "sk-super-secret-cli-value")
    result = runner.invoke(app, ["providers", "status", "--providers", str(config)])
    assert result.exit_code == 0, result.output
    assert "sk-super-secret-cli-value" not in result.output
    assert "super-secret" not in result.output
    payload = json.loads(result.output)
    assert payload["providers"]["llm_primary"]["family"] == "LLMProvider"


def test_providers_status_missing_declared_key_exits_1(tmp_path, monkeypatch) -> None:
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
spec_version: "1.0.0"
providers:
  llm_primary:
    kind: openai_compatible
    base_url: "https://api.example.invalid/v1"
    api_key_env: "CLI_TEST_KEY_ABSENT"
    models:
      analyst:
        model: "test-model"
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("CLI_TEST_KEY_ABSENT", raising=False)
    result = runner.invoke(app, ["providers", "status", "--providers", str(config)])
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    envelope = json.loads(text)
    assert envelope["error"]["code"] == "CFG_001"


def test_providers_canary_mocks_all_ok() -> None:
    result = runner.invoke(app, ["providers", "canary", "--providers", MOCK_CONFIG])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["canaries"]["mock_llm"]["canary_state"] == "OK"
    assert payload["canaries"]["mock_ocr"]["canary_state"] == "OK"
    assert payload["canaries"]["object_store"]["canary_state"] == "UNSUPPORTED"


def test_providers_canary_single_provider_filter() -> None:
    result = runner.invoke(
        app, ["providers", "canary", "--providers", MOCK_CONFIG, "--provider", "mock_llm"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert list(payload["canaries"]) == ["mock_llm"]


def test_providers_canary_unknown_provider_exits_1() -> None:
    result = runner.invoke(
        app, ["providers", "canary", "--providers", MOCK_CONFIG, "--provider", "nope"]
    )
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_002"


def test_providers_canary_hostile_mock_fails_exit_1(tmp_path) -> None:
    """A mock in TIMEOUT mode must produce a FAILED canary and exit 1 —
    the canary surface never pretends health."""
    config = tmp_path / "providers.yaml"
    config.write_text(
        """
spec_version: "1.0.0"
providers:
  broken_llm:
    kind: mock_llm
    options:
      mode: "TIMEOUT"
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["providers", "canary", "--providers", str(config)])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["canaries"]["broken_llm"]["canary_state"] == "FAILED"
    assert "LLM_001" in payload["canaries"]["broken_llm"]["detail"]


def test_providers_without_config_exits_1() -> None:
    result = runner.invoke(app, ["providers", "status"])
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_001"

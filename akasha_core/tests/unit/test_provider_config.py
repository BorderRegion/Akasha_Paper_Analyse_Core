"""Provider configuration loader tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperintel.config.provider_config import (
    load_provider_config,
    provider_config_fingerprint,
    resolve_api_key,
)
from paperintel.errors import DomainError

SPEC_EXAMPLE = (
    Path(__file__).resolve().parents[2]
    / "spec"
    / "paperintel_final_spec"
    / "templates"
    / "provider_config.example.yaml"
)

VALID_ENV = {
    "LLM_BASE_URL": "https://llm.example.com/v1",
    "LLM_ANALYST_MODEL": "analyst-x",
    "LLM_VERIFIER_MODEL": "verifier-x",
    "LLM_SYNTH_MODEL": "synth-x",
    "PADDLE_OCR_BASE_URL": "http://127.0.0.1:8866",
    "LLM_API_KEY": "sk-test-not-real",
    "PADDLE_OCR_API_KEY": "",
}


def test_spec_example_loads_with_env() -> None:
    config = load_provider_config(SPEC_EXAMPLE, env=VALID_ENV)
    assert config.spec_version == "1.0.0"
    llm = config.providers["llm_primary"]
    assert llm.kind.value == "openai_compatible"
    assert llm.base_url == "https://llm.example.com/v1"
    assert llm.api_key_env == "LLM_API_KEY"
    assert set(llm.models) == {"analyst", "verifier", "synthesizer"}
    assert llm.models["analyst"].model == "analyst-x"
    ocr = config.providers["ocr_primary"]
    assert ocr.kind.value == "paddle_http"
    assert ocr.max_concurrency == 8


def test_missing_env_var_raises_cfg001() -> None:
    env = dict(VALID_ENV)
    del env["LLM_BASE_URL"]
    with pytest.raises(DomainError) as excinfo:
        load_provider_config(SPEC_EXAMPLE, env=env)
    error = excinfo.value
    assert error.code == "CFG_001"
    assert error.details["env_var"] == "LLM_BASE_URL"


def test_missing_file_raises_cfg001(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as excinfo:
        load_provider_config(tmp_path / "absent.yaml", env=VALID_ENV)
    assert excinfo.value.code == "CFG_001"


def test_unknown_provider_kind_rejected(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        """
spec_version: "1.0.0"
providers:
  llm_primary:
    kind: some_unknown_sdk
    base_url: "https://x.example.com"
""",
        encoding="utf-8",
    )
    with pytest.raises(DomainError) as excinfo:
        load_provider_config(path, env={})
    assert excinfo.value.code == "CFG_002"


def test_remote_kind_without_url_rejected(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        """
spec_version: "1.0.0"
providers:
  llm_primary:
    kind: openai_compatible
""",
        encoding="utf-8",
    )
    with pytest.raises(DomainError) as excinfo:
        load_provider_config(path, env={})
    assert excinfo.value.code == "CFG_002"


def test_resolve_api_key_reads_env_only() -> None:
    config = load_provider_config(SPEC_EXAMPLE, env=VALID_ENV)
    llm = config.providers["llm_primary"]
    assert resolve_api_key(llm, env=VALID_ENV) == "sk-test-not-real"
    assert resolve_api_key(llm, env={}) is None
    # Empty string counts as unset (never a silent fallback to "").
    ocr = config.providers["ocr_primary"]
    assert resolve_api_key(ocr, env=VALID_ENV) is None


def test_key_material_never_in_config_object() -> None:
    config = load_provider_config(SPEC_EXAMPLE, env=VALID_ENV)
    dumped = config.model_dump_json()
    assert "sk-test-not-real" not in dumped
    assert "LLM_API_KEY" in dumped  # env var NAME is fine, VALUE is not


def test_fingerprint_stable_and_secret_free() -> None:
    config_a = load_provider_config(SPEC_EXAMPLE, env=VALID_ENV)
    env_b = dict(VALID_ENV, LLM_API_KEY="totally-different-key")
    config_b = load_provider_config(SPEC_EXAMPLE, env=env_b)
    fp_a = provider_config_fingerprint(config_a)
    fp_b = provider_config_fingerprint(config_b)
    # Key VALUES differ but are not part of the config → same fingerprint.
    assert fp_a == fp_b
    assert len(fp_a) == 64
    # Changing a structural value changes the fingerprint.
    env_c = dict(VALID_ENV, LLM_ANALYST_MODEL="analyst-y")
    config_c = load_provider_config(SPEC_EXAMPLE, env=env_c)
    assert provider_config_fingerprint(config_c) != fp_a

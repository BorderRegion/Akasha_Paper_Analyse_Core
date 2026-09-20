"""Configuration tests (P00 gate check P00-C10: config rejects unknown/invalid
critical keys)."""

from __future__ import annotations

from pathlib import Path

import pytest

from paperintel.config.settings import AppConfig, load_config
from paperintel.errors import DomainError


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_defaults_load_without_file() -> None:
    config = load_config()
    assert isinstance(config, AppConfig)
    assert config.core.log_level == "INFO"
    assert config.database.url.startswith("postgresql+psycopg://")
    assert config.disk.warning_free_percent == 15.0


def test_env_overrides_apply(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@dbhost:5432/other")
    monkeypatch.setenv("API_PORT", "9999")
    config = load_config()
    assert config.database.url == "postgresql+psycopg://u:p@dbhost:5432/other"
    assert config.api.port == 9999
    assert config.core.data_dir == tmp_path / "data"  # from conftest isolation


def test_yaml_file_values_and_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(
        tmp_path,
        "paperintel.yaml",
        """
core:
  env: staging
  log_level: DEBUG
api:
  port: 8500
""",
    )
    monkeypatch.setenv("API_PORT", "8600")
    monkeypatch.delenv("PAPERINTEL_ENV", raising=False)
    config = load_config(path)
    assert config.core.env == "staging"
    assert config.core.log_level == "DEBUG"
    # Environment wins over the file.
    assert config.api.port == 8600


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "core:\n  env: dev\nnot_a_real_section: 1\n")
    with pytest.raises(DomainError) as excinfo:
        load_config(path)
    assert excinfo.value.code == "CFG_002"
    assert "not_a_real_section" in (excinfo.value.message or "")


def test_unknown_section_key_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "database:\n  url: postgresql+psycopg://x\npoolsize: 3\n")
    with pytest.raises(DomainError) as excinfo:
        load_config(path)
    assert excinfo.value.code == "CFG_002"


def test_invalid_value_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "api:\n  port: not-a-number\n")
    with pytest.raises(DomainError) as excinfo:
        load_config(path)
    error = excinfo.value
    assert error.code == "CFG_002"
    assert error.details["errors"][0]["loc"] == "api.port"


def test_out_of_range_value_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "disk:\n  warning_free_percent: 150\n")
    with pytest.raises(DomainError) as excinfo:
        load_config(path)
    assert excinfo.value.code == "CFG_002"


def test_missing_file_raises_cfg001(tmp_path: Path) -> None:
    with pytest.raises(DomainError) as excinfo:
        load_config(tmp_path / "absent.yaml")
    assert excinfo.value.code == "CFG_001"


def test_non_mapping_file_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "- just\n- a list\n")
    with pytest.raises(DomainError) as excinfo:
        load_config(path)
    assert excinfo.value.code == "CFG_002"


def test_settings_are_frozen() -> None:
    from pydantic import ValidationError

    config = load_config()
    with pytest.raises(ValidationError):
        config.core.env = "hacked"  # type: ignore[misc]


def test_error_envelope_does_not_leak_secret_values(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.yaml", "api:\n  token: super-secret-value\n  port: -1\n")
    with pytest.raises(DomainError) as excinfo:
        load_config(path)
    envelope = excinfo.value.to_envelope()
    assert "super-secret-value" not in str(envelope)

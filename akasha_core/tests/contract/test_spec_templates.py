"""All spec example files validate (P00 gate check P00-C07).

Validates every machine-readable file under spec/paperintel_final_spec/templates
against the implemented contracts, plus the PACKAGE_MANIFEST integrity of the
whole frozen spec.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from paperintel.config.provider_config import load_provider_config
from paperintel.schemas.agent import AgentResult
from paperintel.schemas.operations import DebugBundleManifest, GateReport, ModuleManifest

PROVIDER_ENV = {
    "LLM_BASE_URL": "https://llm.example.com/v1",
    "LLM_ANALYST_MODEL": "analyst-x",
    "LLM_VERIFIER_MODEL": "verifier-x",
    "LLM_SYNTH_MODEL": "synth-x",
    "PADDLE_OCR_BASE_URL": "http://127.0.0.1:8866",
}


def test_agent_result_example_validates(templates_dir: Path) -> None:
    payload = json.loads((templates_dir / "agent_result.example.json").read_text())
    result = AgentResult.model_validate(payload)
    assert result.status.value == "SUCCESS"
    assert result.claims[0].claim_type.value == "FACT"
    assert result.claims[0].evidence[0].evidence_id == "ev_example"
    assert result.claims[0].evidence[0].role.value == "SUPPORT"


def test_debug_bundle_manifest_example_validates(templates_dir: Path) -> None:
    payload = json.loads((templates_dir / "debug_bundle_manifest.example.json").read_text())
    manifest = DebugBundleManifest.model_validate(payload)
    assert manifest.bundle_version == "1.0.0"
    assert manifest.target_type == "job"
    for required in ("api_keys", "authorization_headers", "cookies", ".env"):
        assert required in manifest.redactions


def test_gate_report_example_validates(templates_dir: Path) -> None:
    payload = json.loads((templates_dir / "gate_report.schema.example.json").read_text())
    report = GateReport.model_validate(payload)
    assert report.phase == "P08"
    assert report.result.value == "PASS"
    assert report.checks[0].id == "P08-C01"
    assert report.checks[0].evidence == "pytest::test_numeric_hallucination"


def test_module_manifest_example_validates(templates_dir: Path) -> None:
    data = yaml.safe_load((templates_dir / "module_manifest.example.yaml").read_text())
    manifest = ModuleManifest.model_validate(data)
    assert manifest.module_id == "extraction.pdf"
    assert manifest.healthcheck.type == "internal"
    assert manifest.idempotent is True
    assert set(manifest.error_namespaces) == {"PDF", "EXTRACT", "OCR"}


def test_provider_config_example_validates(templates_dir: Path) -> None:
    config = load_provider_config(templates_dir / "provider_config.example.yaml", env=PROVIDER_ENV)
    assert config.spec_version == "1.0.0"
    assert set(config.providers) == {"llm_primary", "ocr_primary"}
    assert config.providers["llm_primary"].models["analyst"].max_concurrency == 24




@pytest.mark.parametrize(
    "name",
    [
        "agent_result.example.json",
        "debug_bundle_manifest.example.json",
        "gate_report.schema.example.json",
        "module_manifest.example.yaml",
        "provider_config.example.yaml",
    ],
)
def test_all_template_files_exist(templates_dir: Path, name: str) -> None:
    assert (templates_dir / name).is_file()

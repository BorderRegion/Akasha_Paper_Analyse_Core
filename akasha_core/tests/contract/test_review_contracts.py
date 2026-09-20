"""Frozen-spec assertions independent of implementation inventories."""

import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from paperintel.api.app import create_app
from paperintel.config.settings import ApiSettings, AppConfig
from paperintel.providers.resilience import availability_from
from paperintel.schemas.enums import CanaryState, CircuitState, ModuleHealthState






@pytest.mark.parametrize("path", ["/metrics", "/ui", "/ui/papers/pap_unknown", "/v1/system/status"])
def test_sensitive_surfaces_reject_unauthenticated_requests_before_db(path):
    settings = AppConfig(api=ApiSettings(token="test-only-token"))
    app = create_app(state=SimpleNamespace(settings=settings))
    assert TestClient(app).get(path).status_code == 401


def test_canary_failure_degrades_successful_transport():
    assert (
        availability_from(CircuitState.CLOSED, 0.0, True, CanaryState.FAILED)
        == ModuleHealthState.DEGRADED
    )




def test_config_hash_tracks_provider_content_not_location(tmp_path):
    from paperintel.config.fingerprint import analysis_config_hash

    provider_file = tmp_path / "providers.yaml"
    provider_file.write_text('spec_version: "1.0.0"\nproviders: {}\n')
    settings = AppConfig(providers_file=provider_file)
    original = analysis_config_hash(settings=settings, tier="T2_FULL")
    assert len(original) == 64
    assert original != analysis_config_hash(settings=settings, tier="T3_DEEP")
    provider_file.write_text('spec_version: "1.0.0"\nproviders:\n  mock:\n    kind: mock_llm\n')
    assert original != analysis_config_hash(settings=settings, tier="T2_FULL")

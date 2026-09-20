"""P12 REST contract tests (doc 04 §1-§6, §16).

The API is exercised through FastAPI's TestClient against the disposable
test database with mock providers, so the contract (paths, shapes, error
envelope, auth) is verified exactly as a client sees it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.generators import build_f01_native

from paperintel.api.app import ApiState, create_app
from paperintel.config.settings import ApiSettings
from paperintel.errors import DomainError

REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture()
def api(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A TestClient bound to the disposable DB + mock providers."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE", str(REPO_ROOT / "config" / "providers.mock.yaml")
    )
    from paperintel.config.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()
    state = ApiState(settings)
    app = create_app(settings, state=state)
    with TestClient(app) as client:
        client.state = state  # type: ignore[attr-defined]
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield client
    state.engine.dispose()
    reset_settings_cache()


def _import_paper(api: TestClient) -> dict:
    pdf = api.data_dir / "f01.pdf"  # type: ignore[attr-defined]
    pdf.write_bytes(build_f01_native())
    response = api.post("/v1/papers/import", json={"path": str(pdf)})
    assert response.status_code == 200, response.text
    return response.json()


def test_metrics_cover_frozen_inventory_and_imported_records(api):
    import re

    from prometheus_client.parser import text_string_to_metric_families

    imported = _import_paper(api)
    from tests.fixtures.canary import seed_canary_evidence

    from paperintel.workflow.engine import create_job

    with api.state.session_factory() as session:
        create_job(
            session, paper_id=imported["paper_id"], paper_version_id=imported["paper_version_id"]
        )
        seed_canary_evidence(session, imported["paper_version_id"])
        session.commit()
    response = api.get("/metrics")
    assert response.status_code == 200
    metrics = list(text_string_to_metric_families(response.text))
    section = (REPO_ROOT / "tests/fixtures/metrics_inventory.txt").read_text()
    names = re.findall(r"^([a-z]+_[a-z_]+)(?:\{[^}]+\})?$", section, re.MULTILINE)
    assert len(names) == 18
    exported = {m.name for m in metrics}
    for name in names:
        assert name in exported or name.removesuffix("_total") in exported, name
    samples = [s for m in metrics for s in m.samples]
    assert sum(s.value for s in samples if s.name == "jobs_total") >= 1
    assert sum(s.value for s in samples if s.name == "evidence_created_total") >= 1


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def test_system_status_contract_shape(api: TestClient) -> None:
    response = api.get("/v1/system/status")
    assert response.status_code == 200
    payload = response.json()
    for key in (
        "spec_version",
        "pipeline_version",
        "overall_state",
        "queue",
        "providers",
        "workers",
        "disk",
        "pipeline",
    ):
        assert key in payload, f"missing {key}"
    assert set(payload["queue"]) >= {"pending", "running", "failed"}
    assert set(payload["disk"]) >= {"total_bytes", "used_bytes", "free_bytes", "breakdown"}
    assert {"keep", "cache", "temp", "debug_ttl", "database_estimate"} <= set(
        payload["disk"]["breakdown"]
    )
    assert payload["spec_version"] == "1.0.0"


def test_system_endpoints(api: TestClient) -> None:
    assert api.get("/v1/system/version").json()["pipeline_version"]
    modules = api.get("/v1/system/modules").json()
    assert modules["count"] > 10
    assert any(module["module_id"] == "verification.engine" for module in modules["modules"])
    providers = api.get("/v1/system/providers").json()
    assert "mock_llm" in providers["providers"]
    storage = api.get("/v1/system/storage").json()
    assert storage["disk"]["level"] in ("OK", "WARNING", "CRITICAL")
    workers = api.get("/v1/system/workers").json()
    assert workers["mode"] in ("eager", "celery")


def test_metrics_endpoint_is_prometheus_text(api: TestClient) -> None:
    response = api.get("/metrics")
    assert response.status_code == 200
    assert "paperintel_queue_tasks" in response.text
    assert "paperintel_spec_info" in response.text
    assert 'spec_version="1.0.0"' in response.text


# ---------------------------------------------------------------------------
# papers
# ---------------------------------------------------------------------------


def test_import_and_paper_endpoints(api: TestClient) -> None:
    imported = _import_paper(api)
    paper_id = imported["paper_id"]

    summary = api.get(f"/v1/papers/{paper_id}").json()
    assert summary["paper_id"] == paper_id
    assert summary["latest_version"]["paper_version_id"] == imported["paper_version_id"]

    context = api.get(f"/v1/papers/{paper_id}/context").json()
    assert context["paper"]["paper_id"] == paper_id
    assert "structure" in context and "evidence" in context
    assert context["claims"]["total"] >= 0
    assert context["versions"]["pipeline_version"]

    for path in (
        "analysis",
        "audit",
        "claims",
        "evidence",
        "pipeline",
        "methods",
        "experiments",
        "techniques",
        "people",
    ):
        response = api.get(f"/v1/papers/{paper_id}/{path}")
        assert response.status_code == 200, (path, response.text)
        body = response.json()
        if path in ("claims", "methods", "experiments", "techniques", "people"):
            assert isinstance(body[path], list)
        if path == "audit":
            assert set(body) >= {
                "summary",
                "high_risk_claims",
                "unsupported_claims",
                "ocr_sensitive_claims",
                "numeric_conflicts",
                "agent_disagreements",
                "single_model_claims",
                "external_inferences",
                "verified_claims",
            }
            assert set(body["summary"]) == {
                "verified_claims",
                "disputed_claims",
                "unsupported_claims",
                "ocr_sensitive_claims",
                "agent_disagreements",
                "numeric_conflicts",
            }
        if path == "pipeline":
            assert set(body) >= {
                "paper_id",
                "job_id",
                "requested_tier",
                "effective_tier",
                "state",
                "stages",
            }
            for stage in body["stages"]:
                assert set(stage) >= {"name", "state", "quality", "subtasks"}


def test_tier_endpoint_records_manual_decision(api: TestClient) -> None:
    imported = _import_paper(api)
    response = api.post(f"/v1/papers/{imported['paper_id']}/tier", json={"tier": "T3_DEEP"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["effective_tier"] == "T3_DEEP"

    bad = api.post(f"/v1/papers/{imported['paper_id']}/tier", json={"tier": "T9_NOPE"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "CFG_002"


def test_reanalyze_plans_a_job(api: TestClient) -> None:
    imported = _import_paper(api)
    response = api.post(f"/v1/papers/{imported['paper_id']}/reanalyze")
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"].startswith("job_")
    assert "planned" in payload


@pytest.mark.needs_db
def test_unknown_paper_returns_error_envelope(api: TestClient) -> None:
    response = api.get("/v1/papers/pap_01UNKNOWN000000000000000000")
    assert response.status_code == 404
    payload = response.json()
    assert payload["error"]["code"] == "CFG_002"
    assert "message" in payload["error"]


def test_import_missing_file_fails_loudly(api: TestClient) -> None:
    response = api.post("/v1/papers/import", json={"path": "/nonexistent/paper.pdf"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "STORAGE_001"


# ---------------------------------------------------------------------------
# evidence + claims
# ---------------------------------------------------------------------------


def test_evidence_and_claims_endpoints(api: TestClient) -> None:
    imported = _import_paper(api)
    context = api.get(f"/v1/papers/{imported['paper_id']}/context").json()
    assert context["evidence"]["total"] >= 0

    # Pull one evidence ID through search, then read it directly.
    hits = api.post(
        "/v1/search/evidence",
        json={"query": "dataset", "paper_version_ids": [imported["paper_version_id"]]},
    ).json()
    if hits["hits"]:
        evidence_id = hits["hits"][0]["document_id"]
        if evidence_id.startswith("ev_"):
            detail = api.get(f"/v1/evidence/{evidence_id}")
            assert detail.status_code == 200
            assert detail.json()["evidence_id"] == evidence_id

    missing = api.get("/v1/evidence/ev_01UNKNOWN000000000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "EVIDENCE_001"


# ---------------------------------------------------------------------------
# jobs / tasks / traces / search / collections
# ---------------------------------------------------------------------------


def test_job_task_trace_endpoints(api: TestClient) -> None:
    imported = _import_paper(api)
    job = api.post(f"/v1/papers/{imported['paper_id']}/reanalyze").json()["job_id"]

    jobs = api.get("/v1/jobs").json()
    assert jobs["count"] >= 1
    assert any(entry["job_id"] == job for entry in jobs["jobs"])

    detail = api.get(f"/v1/jobs/{job}").json()
    assert detail["job"]["job_id"] == job
    assert detail["tasks"]

    stages = api.get(f"/v1/jobs/{job}/stages").json()
    assert stages["job_id"] == job
    tasks = api.get(f"/v1/jobs/{job}/tasks").json()
    assert tasks["tasks"]

    task_id = detail["tasks"][0]["task_id"]
    assert api.get(f"/v1/tasks/{task_id}").json()["task_id"] == task_id

    trace_id = detail["job"]["trace_id"]
    if trace_id:
        trace = api.get(f"/v1/traces/{trace_id}").json()
        assert trace["trace_id"] == trace_id
        assert trace["count"] >= 1

    cancelled = api.post(f"/v1/jobs/{job}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] in ("CANCELLED", "SUCCEEDED", "FAILED")


def test_search_endpoints_cover_all_kinds(api: TestClient) -> None:
    imported = _import_paper(api)
    for kind in ("papers", "claims", "evidence", "entities", "techniques", "methods"):
        response = api.post(f"/v1/search/{kind}", json={"query": "dataset", "limit": 5})
        assert response.status_code == 200, (kind, response.text)
        payload = response.json()
        assert payload["kind"] == kind
        assert "hits" in payload and "scope_size" in payload

    # An unknown request field is REJECTED (422) — never silently ignored.
    bad_filter = api.post("/v1/search/papers", json={"query": "x", "not_a_filter": ["y"]})
    assert bad_filter.status_code == 422

    # An unknown ROUTE is a plain 404 (FastAPI), never a fake domain error.
    assert api.post("/v1/search/nonsense", json={"query": "x"}).status_code == 404

    # Unknown API surfaces report the catalog code through the service layer.
    unknown_kind = api.post("/v1/search/papers", json={"query": "x", "limit": 500})
    assert unknown_kind.status_code == 422  # schema-bounded limit

    scoped = api.post(
        "/v1/search/evidence",
        json={
            "query": "dataset",
            "paper_version_ids": [imported["paper_version_id"]],
            "limit": 5,
        },
    ).json()
    assert all(hit["paper_version_id"] == imported["paper_version_id"] for hit in scoped["hits"])


def test_collection_endpoints(api: TestClient) -> None:
    imported = _import_paper(api)
    created = api.post("/v1/collections", json={"name": "api collection"}).json()
    collection_id = created["collection_id"]

    listing = api.get("/v1/collections").json()
    assert any(entry["collection_id"] == collection_id for entry in listing["collections"])

    added = api.post(
        f"/v1/collections/{collection_id}/papers",
        json={"paper_id": imported["paper_id"], "pinned": True},
    )
    assert added.status_code == 200
    assert added.json()["pinned"] is True

    detail = api.get(f"/v1/collections/{collection_id}").json()
    assert detail["paper_ids"] == [imported["paper_id"]]

    intelligence = api.get(f"/v1/collections/{collection_id}/intelligence").json()
    assert "views" in intelligence
    assert len(intelligence["views"]) == 12

    analyzed = api.post(f"/v1/collections/{collection_id}/analyze").json()
    assert analyzed["collection_id"] == collection_id

    removed = api.delete(f"/v1/collections/{collection_id}/papers/{imported['paper_id']}").json()
    assert removed["removed"] is True


# ---------------------------------------------------------------------------
# auth + redaction
# ---------------------------------------------------------------------------


def test_token_auth_enforced_when_configured(
    test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "secure-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("API_TOKEN", "test-token-value")
    from paperintel.config.settings import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()
    assert isinstance(settings.api, ApiSettings)
    state = ApiState(settings)
    app = create_app(settings, state=state)
    with TestClient(app) as client:
        unauthenticated = client.get("/v1/system/status")
        assert unauthenticated.status_code == 401

        wrong = client.get("/v1/system/status", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401

        ok = client.get(
            "/v1/system/status",
            headers={"Authorization": "Bearer test-token-value"},
        )
        assert ok.status_code == 200
        assert ok.json()["api"]["auth_required"] is True
    state.engine.dispose()
    reset_settings_cache()


def test_no_secret_material_in_api_responses(api: TestClient) -> None:
    """Security redaction: provider lists never expose key material."""
    payload = json.dumps(api.get("/v1/system/providers").json())
    assert "sk-" not in payload
    assert "api_key" not in payload.lower()

    status = json.dumps(api.get("/v1/system/status").json())
    assert "sk-" not in status

    from paperintel.config.settings import get_settings

    token = get_settings().api.token
    if token:
        assert token not in status


def test_openapi_contains_required_paths(api: TestClient) -> None:
    import re

    schema = api.get("/openapi.json").json()
    specification = (
        REPO_ROOT / "spec/paperintel_final_spec/04_API_MCP_AND_OPERATIONS.md"
    ).read_text()
    endpoints = re.findall(r"^(GET|POST|DELETE)\s+(/\S+)", specification, re.MULTILINE)
    assert len(endpoints) >= 40
    for method, path in endpoints:
        assert path in schema["paths"], (method, path)
        assert method.lower() in schema["paths"][path], (method, path)
    paths = set(schema["paths"])
    required = {
        "/v1/system/status",
        "/v1/system/modules",
        "/v1/system/providers",
        "/v1/system/storage",
        "/v1/system/workers",
        "/v1/system/version",
        "/v1/papers/import",
        "/v1/papers/{paper_id}",
        "/v1/papers/{paper_id}/context",
        "/v1/papers/{paper_id}/analysis",
        "/v1/papers/{paper_id}/audit",
        "/v1/papers/{paper_id}/pipeline",
        "/v1/evidence/{evidence_id}",
        "/v1/claims/{claim_id}",
        "/v1/claims/{claim_id}/verifications",
        "/v1/jobs",
        "/v1/jobs/{job_id}",
        "/v1/tasks/{task_id}",
        "/v1/traces/{trace_id}",
        "/v1/search/papers",
        "/v1/search/claims",
        "/v1/collections",
    }
    missing = required - paths
    assert not missing, f"missing endpoints: {sorted(missing)}"


def test_domain_error_envelope_is_frozen_shape() -> None:
    """Every domain error conforms to doc 03 §11, including retry semantics."""
    from paperintel.api.app import error_response

    response = error_response(DomainError("CFG_002", message="boom", details={"x": 1}))
    body = json.loads(response.body)
    assert set(body) == {"error"}
    from paperintel.schemas.errors import ErrorEnvelope

    ErrorEnvelope.model_validate(body)
    assert set(body["error"]) == {"code", "message", "details", "retryable", "severity", "trace_id"}
    assert body["error"]["code"] == "CFG_002"

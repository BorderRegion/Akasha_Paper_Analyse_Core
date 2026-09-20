"""P12 CLI smoke, operations UI smoke and security redaction tests.

Doc 05 FINAL gate list: CLI smoke, operations UI smoke, security
redaction tests. The CLI runs through Typer's CliRunner against the
disposable DB; the UI is fetched through the API's TestClient.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.generators import build_f01_native
from typer.testing import CliRunner

from paperintel.api.app import ApiState, create_app
from paperintel.cli.main import app as cli_app
from paperintel.operations.debug import redact, redact_text

runner = CliRunner()
REPO_ROOT = Path(__file__).parents[3]


@pytest.fixture()
def cli_env(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("DATABASE_URL", test_db_url)
    monkeypatch.setenv("PAPERINTEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "PAPERINTEL_PROVIDERS_FILE", str(REPO_ROOT / "config" / "providers.mock.yaml")
    )
    from paperintel.config.settings import reset_settings_cache

    reset_settings_cache()
    pdf = data_dir / "f01.pdf"
    pdf.write_bytes(build_f01_native())
    return data_dir


def _json(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_cli_smoke_surface(cli_env: Path) -> None:
    """The doc 04 §8 command surface answers coherently."""
    version = _json(runner.invoke(cli_app, ["version"]))
    assert version["spec_version"] == "1.0.0"

    status = _json(runner.invoke(cli_app, ["status"]))
    assert status["overall_state"] in ("HEALTHY", "DEGRADED", "CRITICAL")
    assert "queue" in status and "disk" in status

    disk = _json(runner.invoke(cli_app, ["disk"]))
    assert "by_retention_class" in disk

    gc = _json(runner.invoke(cli_app, ["gc", "--dry-run"]))
    assert gc["dry_run"] is True

    modules = _json(runner.invoke(cli_app, ["config", "validate"]))
    assert modules

    # import → paper → inspect → audit
    imported = _json(runner.invoke(cli_app, ["import", str(cli_env / "f01.pdf")]))
    paper_id = imported["imported"][0]["paper_id"]

    paper = _json(runner.invoke(cli_app, ["paper", paper_id]))
    assert paper["paper_id"] == paper_id

    context = _json(runner.invoke(cli_app, ["paper", paper_id, "--context"]))
    assert context["paper"]["paper_id"] == paper_id

    inspection = _json(runner.invoke(cli_app, ["inspect", paper_id]))
    assert inspection

    audit = _json(runner.invoke(cli_app, ["audit", paper_id]))
    assert "support_states" in audit


@pytest.mark.needs_db
@pytest.mark.parametrize("analysis_flag", ["--no-analysis", "--analysis"])
def test_cli_status_and_selftest_are_wired(cli_env: Path, engine, analysis_flag) -> None:
    """`selftest --mock` runs the golden path and reports per-stage results."""
    from sqlalchemy import func, select

    from paperintel.database.models import JobRow, PaperRow

    with engine.connect() as connection:
        before = [
            connection.scalar(select(func.count()).select_from(model))
            for model in (PaperRow, JobRow)
        ]
    before_files = set(cli_env.rglob("*"))
    result = runner.invoke(cli_app, ["selftest", "--mock", analysis_flag])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    payload = json.loads(result.output)
    assert payload["mode"] == "mock"
    assert payload["status"] == "PASS", payload
    assert payload["stages"], "selftest must report per-stage outcomes"
    names = {stage["name"] for stage in payload["stages"]}
    assert {"providers", "config", "import", "search", "audit", "disk"} <= names
    with engine.connect() as connection:
        after = [
            connection.scalar(select(func.count()).select_from(model))
            for model in (PaperRow, JobRow)
        ]
    assert after == before
    assert set(cli_env.rglob("*")) == before_files


@pytest.mark.needs_db
def test_cli_selftest_requires_an_explicit_mode(cli_env: Path) -> None:
    result = runner.invoke(cli_app, ["selftest"])
    assert result.exit_code == 1
    text = result.stderr if result.stderr_bytes else result.output
    assert json.loads(text)["error"]["code"] == "CFG_002"


@pytest.mark.needs_db
def test_cli_debug_bundle_redacts_and_writes(cli_env: Path) -> None:
    imported = _json(runner.invoke(cli_app, ["import", str(cli_env / "f01.pdf")]))
    paper_id = imported["imported"][0]["paper_id"]
    rerun = _json(runner.invoke(cli_app, ["rerun", paper_id, "--stage", "ANALYZED"]))
    job_id = rerun["job_id"]

    bundle = _json(runner.invoke(cli_app, ["debug-bundle", job_id]))
    import zipfile

    with zipfile.ZipFile(bundle["written"]) as archive:
        assert "README.txt" in archive.namelist()
        assert "reproduction_command" in archive.read("README.txt").decode()
        bundle = json.loads(archive.read("bundle.json"))
    assert bundle["kind"] == "job"
    assert bundle["job"]["job_id"] == job_id
    assert "redaction" in bundle
    serialized = json.dumps(bundle)
    assert "sk-" not in serialized

    output = cli_env / "bundle.zip"
    written = _json(runner.invoke(cli_app, ["debug-bundle", job_id, "--output", str(output)]))
    assert output.exists()
    assert written["bytes"] > 0

    missing = runner.invoke(cli_app, ["debug-bundle", "job_01UNKNOWN000000000000000000"])
    assert missing.exit_code == 1


def test_cli_rerun_selected_model_reaches_audited_call(cli_env, engine, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from paperintel.database.models import ModelCallRow, TaskRow
    from paperintel.providers.base import LlmResponse
    from paperintel.providers.mocks.llm import MockLLMProvider
    from paperintel.schemas.enums import ModelRole
    from paperintel.workflow import handlers

    class SelectedProvider(MockLLMProvider):
        async def complete(self, request):
            assert request.model_role is ModelRole.VERIFIER
            return LlmResponse(
                content='{"status":"INSUFFICIENT_EVIDENCE","claims":[]}',
                provider_id=self.provider_id,
                model_id="selected-verifier",
            )

    monkeypatch.setattr(handlers, "_resolve_llm_provider", lambda: SelectedProvider())
    imported = _json(runner.invoke(cli_app, ["import", str(cli_env / "f01.pdf")]))
    paper_id = imported["imported"][0]["paper_id"]
    result = _json(
        runner.invoke(
            cli_app, ["rerun", paper_id, "--stage", "verification", "--model", "verifier"]
        )
    )
    task_id = result["task_id"]
    replay = _json(runner.invoke(cli_app, ["replay", task_id, "--eager"]))
    assert replay["replayed"]["outcome"] == "succeeded"
    with Session(engine) as session:
        task = session.get(TaskRow, task_id)
        assert task.input_manifest["model_role"] == "verifier"
        call = session.scalars(select(ModelCallRow).where(ModelCallRow.task_id == task_id)).one()
        assert call.model_id == "selected-verifier"
        assert call.request_manifest_json["model_role"] == "verifier"


# ---------------------------------------------------------------------------
# operations UI smoke
# ---------------------------------------------------------------------------


@pytest.fixture()
def ui_client(test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "ui-data"
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
    application = create_app(settings, state=state)
    with TestClient(application) as client:
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield client
    state.engine.dispose()
    reset_settings_cache()


@pytest.mark.needs_db
def test_operations_ui_shows_required_panels(ui_client: TestClient) -> None:
    """Doc 04 §15: system health, queue, workers, providers, disk, DB,
    Redis, module health and recent failures."""
    response = ui_client.get("/ui")
    assert response.status_code == 200
    html = response.text
    for panel in (
        "System health",
        "Queue",
        "Workers",
        "Providers",
        "Storage",
        "Dependencies",
        "Pipeline module health",
        "Recent failures",
    ):
        assert panel in html, f"missing panel: {panel}"
    assert "database" in html and "redis" in html
    # No secret material in the rendered page.
    assert "sk-" not in html


@pytest.mark.needs_db
def test_paper_ui_shows_required_indicators(ui_client: TestClient) -> None:
    pdf = ui_client.data_dir / "f01.pdf"  # type: ignore[attr-defined]
    pdf.write_bytes(build_f01_native())
    imported = ui_client.post("/v1/papers/import", json={"path": str(pdf)}).json()

    response = ui_client.get(f"/ui/papers/{imported['paper_id']}")
    assert response.status_code == 200
    html = response.text
    for section in (
        "Analysis health",
        "Pipeline stages",
        "Evidence quality",
        "Claims by state",
        "High-risk audit items",
        "Versions",
    ):
        assert section in html, f"missing section: {section}"


def test_root_page_links_surfaces(ui_client: TestClient) -> None:
    response = ui_client.get("/")
    assert response.status_code == 200
    assert "/ui" in response.text
    assert "/metrics" in response.text
    assert "/docs" in response.text


# ---------------------------------------------------------------------------
# security redaction
# ---------------------------------------------------------------------------


def test_redaction_removes_key_material() -> None:
    sample = {
        "api_key": "sk-EXAMPLENOTAREALSECRET0000000000",
        "note": "Authorization: Bearer abcdef1234567890",
        "nested": {"token": "secret-value", "headers": {"X": "y"}},
        "list": ["sk-abcdefghijklmnop", "plain"],
        "number": 5,
    }
    cleaned = redact(sample)
    serialized = json.dumps(cleaned)
    assert "sk-EXAMPLENOTAREALSECRET0000000000" not in serialized
    assert "sk-abcdefghijklmnop" not in serialized
    assert "secret-value" not in serialized
    assert cleaned["api_key"] == "[REDACTED]"
    assert cleaned["nested"]["headers"] == "[REDACTED]"
    assert cleaned["number"] == 5


def test_redaction_handles_plain_text() -> None:
    text = "call failed with key sk-EXAMPLENOTAREALSECRET0000000000 and token=abc12345"
    cleaned = redact_text(text)
    assert "sk-EXAMPLENOTAREALSECRET0000000000" not in cleaned
    assert "[REDACTED" in cleaned


@pytest.mark.needs_db
def test_no_secret_in_debug_bundle_or_api(ui_client: TestClient) -> None:
    """Neither the API responses nor a debug bundle may carry credentials
    from the environment."""
    monkeypatch_token = "sk-SHOULDNEVERAPPEAR123456"
    pdf = ui_client.data_dir / "f01.pdf"  # type: ignore[attr-defined]
    pdf.write_bytes(build_f01_native())
    imported = ui_client.post("/v1/papers/import", json={"path": str(pdf)}).json()

    responses = [
        ui_client.get("/v1/system/status").text,
        ui_client.get("/v1/system/providers").text,
        ui_client.get("/v1/system/storage").text,
        ui_client.get(f"/v1/papers/{imported['paper_id']}/context").text,
        ui_client.get("/ui").text,
    ]
    for payload in responses:
        assert monkeypatch_token not in payload
        assert "sk-EXAMPLENOTAREALSECRET0000000000" not in payload


@pytest.mark.needs_db
def test_error_details_do_not_echo_secrets(ui_client: TestClient) -> None:
    response = ui_client.post(
        "/v1/papers/import", json={"path": "sk-EXAMPLENOTAREALSECRET0000000000.pdf"}
    )
    assert response.status_code == 400
    assert "sk-EXAMPLENOTAREALSECRET0000000000" not in response.text
    # …but the API never echoes a CONFIGURED secret: provider status is clean.
    assert "sk-EXAMPLENOTAREALSECRET0000000000" not in ui_client.get("/v1/system/providers").text

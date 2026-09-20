"""UX-013 — real typed responses, runtime contract validation, no invented fields.

Requirement (docs/06 §服务与版本 + §通用响应):
- the machine-readable route list is `contracts/endpoints.json` and the core
  response schema is `contracts/ui.schema.json`; the three must agree, and F02
  is where the mismatch is caught;
- read models are `{data, meta:{contract_version, snapshot_id, observed_at,
  partial, warnings}}` — deep `dict` shapes are not the client contract;
- errors are `{error:{code,message,retryable,trace_id,details}}` with a real
  HTTP status, adapting both DomainError and FastAPI's 422;
- every response carries `X-Trace-ID`; `X-Client-Request-ID` is echoed.

The JSON Schema is validated with `jsonschema_lite`, which REFUSES any keyword
it does not implement — a schema change cannot silently turn this test into a
no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.integration.ui_contracts.jsonschema_lite import (
    SUPPORTED_KEYWORDS,
    assert_supported,
    validate,
)

FRONTEND_DIR = Path(
    __import__("os").environ.get("PAPERINTEL_FRONTEND_DIR", str(Path(__file__).resolve().parents[4] / "akasha_core_front"))
)


def _schema() -> dict:
    path = FRONTEND_DIR / "contracts" / "ui.schema.json"
    assert path.is_file(), (
        f"the design package contract is missing: {path}. F02 validates real responses "
        "against it; do not stub it."
    )
    return json.loads(path.read_text(encoding="utf-8"))


def _endpoints() -> dict:
    path = FRONTEND_DIR / "contracts" / "endpoints.json"
    assert path.is_file(), f"the machine-readable route list is missing: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized(path: str) -> str:
    """Ignore path-parameter NAMES: `{id}` and `{note_id}` are the same route."""
    out, depth = [], 0
    for char in path:
        if char == "{":
            depth += 1
            out.append("{")
        elif char == "}":
            depth -= 1
            out.append("}")
        elif depth == 0:
            out.append(char)
    return "".join(out)


_META_KEYS = {"contract_version", "snapshot_id", "observed_at", "partial", "warnings"}


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_vendored_schema_uses_only_validated_keywords(requirement: str) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    from tests.integration.ui_contracts.jsonschema_lite import collect_keywords

    schema = _schema()
    assert_supported(schema)
    used = collect_keywords(schema)
    assert used, "the contract schema is empty"
    assert used <= SUPPORTED_KEYWORDS


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_capabilities_response_matches_the_vendored_schema(
    requirement: str, token_env, bearer
) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    schema = _schema()
    response = token_env["client"].get("/v1/ui/capabilities", headers=bearer)
    assert response.status_code == 200, response.text
    body = response.json()
    # oneOf at the schema root: exactly one branch must match.
    validate(body, schema, root=schema)
    validate(body["data"], schema["$defs"]["Capabilities"], root=schema)
    codes = {entry["code"] for entry in body["data"]["capabilities"]}
    assert {"library.query", "reader.document", "import.upload"} <= codes
    assert body["data"]["limits"]["upload_file_bytes"] > 0


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_preferences_response_matches_the_vendored_schema(
    requirement: str, token_env, bearer
) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    schema = _schema()
    body = token_env["client"].get("/v1/ui/preferences", headers=bearer).json()
    validate(body["data"], schema["$defs"]["Preferences"], root=schema)


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_read_models_are_enveloped_and_carry_a_trace_id(
    requirement: str, token_env, bearer, import_pdf_token_env
) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    imported = import_pdf_token_env()
    client = token_env["client"]
    headers = {**bearer, "X-Client-Request-ID": "ui-ux-013"}

    reads = [
        ("GET", "/v1/ui/bootstrap", None),
        ("GET", "/v1/ui/capabilities", None),
        ("POST", "/v1/ui/library/query", {"limit": 5}),
        ("GET", f"/v1/ui/papers/{imported.paper_id}/workspace", None),
        ("GET", f"/v1/ui/papers/{imported.paper_id}/versions", None),
        ("GET", f"/v1/ui/papers/{imported.paper_id}/jobs", None),
        ("GET", f"/v1/ui/papers/{imported.paper_id}/notes", None),
        ("GET", "/v1/ui/preferences", None),
    ]
    for method, path, payload in reads:
        response = client.request(method, path, json=payload, headers=headers)
        assert response.status_code == 200, f"{method} {path}: {response.text}"
        body = response.json()
        assert set(body) == {"data", "meta"}, f"{method} {path} is not enveloped"
        assert set(body["meta"]) == _META_KEYS
        assert body["meta"]["contract_version"] == "1.0.0"
        assert isinstance(body["meta"]["partial"], bool)
        assert body["meta"]["warnings"] == [] or body["meta"]["warnings"]
        assert response.headers.get("X-Trace-ID"), f"{method} {path} has no trace id"
        assert response.headers.get("X-Client-Request-ID") == "ui-ux-013"


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_responses_validate_against_their_declared_models(
    requirement: str, token_env, bearer, import_pdf_token_env
) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    """Typed responses: the payload is re-validated with the SAME pydantic model
    the server declares, and every model forbids unknown keys."""
    from paperintel.schemas.ui.models import (
        ImportBatchView,
        LibraryQuery,
        NoteView,
        PageEvidence,
        PersonalState,
        Workspace,
    )
    from paperintel.schemas.ui.session import Bootstrap, Capabilities, SessionInfo

    imported = import_pdf_token_env()
    client = token_env["client"]

    bootstrap = client.get("/v1/ui/bootstrap").json()["data"]
    Bootstrap.model_validate(bootstrap)

    caps = client.get("/v1/ui/capabilities", headers=bearer).json()["data"]
    Capabilities.model_validate(caps)

    session = client.post("/v1/ui/session", json={"token": token_env["client"].app_token}).json()
    SessionInfo.model_validate(session["data"])

    page = client.post("/v1/ui/library/query", json={"limit": 5}, headers=bearer).json()["data"]
    assert page["kind"] == "PAPERS", "library results must be discriminated by kind"
    LibraryQuery.model_validate({"limit": 5})
    for item in page["items"]:
        PersonalState.model_validate(item["personal"])

    workspace = client.get(f"/v1/ui/papers/{imported.paper_id}/workspace", headers=bearer).json()[
        "data"
    ]
    Workspace.model_validate(workspace)

    note = client.post(
        "/v1/ui/notes",
        json={
            "paper_id": imported.paper_id,
            "paper_version_id": imported.paper_version_id,
            "body": "typed note",
        },
        headers=bearer,
    ).json()["data"]
    NoteView.model_validate(note)

    batch = client.post("/v1/ui/import-batches", json={}, headers=bearer).json()["data"]
    ImportBatchView.model_validate(
        client.get(f"/v1/ui/import-batches/{batch['batch_id']}", headers=bearer).json()["data"]
    )

    page_evidence = client.get(
        f"/v1/ui/paper-versions/{imported.paper_version_id}/pages/1/evidence", headers=bearer
    ).json()["data"]
    PageEvidence.model_validate(page_evidence)

    # Unknown keys are rejected by the contract models themselves.
    for model in (Bootstrap, Capabilities, Workspace, ImportBatchView):
        assert model.model_config.get("extra") == "forbid"
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Capabilities.model_validate({**caps, "unexpected": 1})


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_error_shapes_are_adapted_with_real_statuses(requirement: str, token_env, bearer) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    client = token_env["client"]

    cases = [
        (client.get("/v1/ui/capabilities"), 401),
        (
            client.patch("/v1/ui/preferences", json={"theme": "DARK"}),
            401,
        ),
        (client.get("/v1/ui/papers/pap_does_not_exist/workspace", headers=bearer), 404),
        (client.post("/v1/ui/library/query", json={"limit": 500}, headers=bearer), 422),
        (
            client.post(
                "/v1/ui/library/query", json={"cursor": "!!!not-base64!!!"}, headers=bearer
            ),
            400,
        ),
        (client.post("/v1/ui/operations", json={"kind": "shell_exec"}, headers=bearer), 400),
    ]
    for response, expected_status in cases:
        assert response.status_code == expected_status, response.text
        error = response.json()["error"]
        assert set(error) == {"code", "message", "retryable", "trace_id", "details"}
        assert isinstance(error["retryable"], bool)
        assert error["trace_id"], "errors must carry a trace id"
        assert response.headers.get("X-Trace-ID") == error["trace_id"]
        assert expected_status != 200

    stale = client.post(
        "/v1/ui/library/query",
        json={"cursor": _stale_cursor(), "limit": 5},
        headers=bearer,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "RESET_CURSOR"


def _stale_cursor() -> str:
    from paperintel.schemas.ui.models import LibraryQuery
    from paperintel.services.ui import library as library_service

    return library_service.encode_cursor(
        filter_hash_value=library_service.filter_hash(LibraryQuery()),
        scope="stale-scope-revision",
        sort_key="RELEVANCE",
        offset=50,
    )


@pytest.mark.needs_db
@pytest.mark.parametrize("requirement", ["UX-013"], ids=["UX-013"])
def test_every_implemented_ui_route_is_documented_and_f02_routes_exist(requirement: str) -> None:
    assert requirement == "UX-013", "test/requirement mapping drift"
    """The route list, the schema and the server must agree (docs/06 §服务与版本)."""
    from paperintel.api.app import create_app

    spec = create_app().openapi()
    implemented = {
        (method.upper(), _normalized(path))
        for path, operations in spec["paths"].items()
        for method in operations
        if method.lower() in {"get", "post", "patch", "delete", "put"} and path.startswith("/v1/ui")
    }
    documented = {
        (_e["method"].upper(), _normalized(_e["path"])) for _e in _endpoints()["endpoints"]
    }

    undocumented = sorted(implemented - documented)
    assert not undocumented, f"/v1/ui routes missing from contracts/endpoints.json: {undocumented}"

    f02 = {
        (_e["method"].upper(), _normalized(_e["path"]))
        for _e in _endpoints()["endpoints"]
        if _e.get("phase") in {"F00", "F01", "F02"}
    }
    missing = sorted(f02 - implemented)
    assert not missing, f"documented F02 endpoints are not implemented: {missing}"

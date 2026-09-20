"""P12 MCP contract tests (doc 04 §7) + CLI smoke + operations UI smoke.

MCP tools must call the same service layer as REST (no separate business
logic), expose the required tool set, and return domain errors as tool
errors carrying the frozen code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fixtures.generators import build_f01_native

from paperintel.api.app import ApiState, create_app
from paperintel.mcp.server import PROTOCOL_VERSION, build_tools, handle_request

REPO_ROOT = Path(__file__).parents[3]

REQUIRED_TOOLS = {
    "search_papers",
    "get_paper",
    "get_paper_context",
    "get_paper_analysis",
    "get_paper_audit",
    "search_claims",
    "get_claim",
    "get_claim_evidence",
    "get_evidence",
    "get_page_evidence",
    "get_table",
    "get_figure",
    "search_methods",
    "search_techniques",
    "search_authors",
    "search_datasets",
    "compare_papers",
    "search_collection",
    "analyze_collection",
    "get_risky_claims",
    "reverify_claim",
    "get_system_status",
    "get_job_status",
    "get_module_status",
}


@pytest.fixture()
def env(session, test_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Session + settings + a TestClient bound to the same test DB."""
    data_dir = tmp_path / "mcp-data"
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
        client.data_dir = data_dir  # type: ignore[attr-defined]
        yield {"session": session, "settings": settings, "client": client}
    state.engine.dispose()
    reset_settings_cache()


def _call(session, name: str, **arguments) -> dict:
    return handle_request(
        session,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def _import_paper(client: TestClient) -> dict:
    pdf = client.data_dir / "f01.pdf"  # type: ignore[attr-defined]
    pdf.write_bytes(build_f01_native())
    response = client.post("/v1/papers/import", json={"path": str(pdf)})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# protocol surface
# ---------------------------------------------------------------------------


def test_initialize_and_tool_list() -> None:
    tools = build_tools()
    assert REQUIRED_TOOLS <= set(tools), sorted(REQUIRED_TOOLS - set(tools))

    listing = handle_request(None, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert listing["result"]["tools"]
    for manifest in listing["result"]["tools"]:
        assert manifest["inputSchema"]["type"] == "object"
        assert manifest["description"]


def test_initialize_reports_protocol_and_server() -> None:
    response = handle_request(None, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == "paperintel"
    assert "tools" in result["capabilities"]


def test_unknown_method_and_tool_are_errors() -> None:
    unknown_method = handle_request(None, {"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert unknown_method["error"]["code"] == -32601

    unknown_tool = handle_request(
        None,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "not_a_tool", "arguments": {}},
        },
    )
    assert unknown_tool["error"]["code"] == -32602

    missing_args = handle_request(
        None,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_paper", "arguments": {}},
        },
    )
    assert missing_args["error"]["code"] == -32602
    assert "paper_id" in missing_args["error"]["message"]


# ---------------------------------------------------------------------------
# tools return the same data as REST
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_tools_share_the_service_layer_with_rest(env) -> None:
    session = env["session"]
    imported = _import_paper(env["client"])
    paper_id = imported["paper_id"]

    mcp_context = _call(session, "get_paper_context", paper_id=paper_id)
    assert "result" in mcp_context, mcp_context
    rest_context = env["client"].get(f"/v1/papers/{paper_id}/context").json()
    # Identical payloads: MCP is a thin surface over the same read models.
    assert mcp_context["result"]["structuredContent"] == rest_context


@pytest.mark.needs_db
def test_paper_and_audit_tools(env) -> None:
    session = env["session"]
    imported = _import_paper(env["client"])
    paper_id = imported["paper_id"]

    summary = _call(session, "get_paper", paper_id=paper_id)["result"]["structuredContent"]
    assert summary["paper_id"] == paper_id

    analysis = _call(session, "get_paper_analysis", paper_id=paper_id)["result"][
        "structuredContent"
    ]
    assert "by_category" in analysis

    audit = _call(session, "get_paper_audit", paper_id=paper_id)["result"]["structuredContent"]
    assert "support_states" in audit


@pytest.mark.needs_db
def test_search_tools_and_scope(env) -> None:
    session = env["session"]
    imported = _import_paper(env["client"])

    papers = _call(session, "search_papers", query="extraction", limit=5)["result"][
        "structuredContent"
    ]
    assert papers["kind"] == "papers"

    evidence = _call(
        session,
        "search_collection",
        collection_id="col_01UNKNOWN000000000000000000",
        query="x",
    )
    # Unknown collection → the service raises CFG_002 → MCP tool error.
    assert evidence["result"]["isError"] is True
    assert "CFG_002" in evidence["result"]["content"][0]["text"]

    for tool in ("search_methods", "search_techniques", "search_datasets", "search_authors"):
        payload = _call(session, tool, query="dataset", limit=3)
        assert "result" in payload, (tool, payload)

    assert imported["paper_id"]


@pytest.mark.needs_db
def test_evidence_and_claim_tools(env) -> None:
    session = env["session"]
    imported = _import_paper(env["client"])
    version_id = imported["paper_version_id"]

    page = _call(session, "get_page_evidence", paper_version_id=version_id, page=1)
    assert "result" in page

    tables = _call(session, "get_table", paper_version_id=version_id, limit=5)
    assert tables["result"]["structuredContent"]["evidence_type"] == "TABLE"

    figures = _call(session, "get_figure", paper_version_id=version_id, limit=5)
    assert figures["result"]["structuredContent"]["evidence_type"] == "FIGURE"

    missing = _call(session, "get_evidence", evidence_id="ev_01UNKNOWN000000000000000000")
    assert missing["result"]["isError"] is True
    assert "EVIDENCE_001" in missing["result"]["content"][0]["text"]


@pytest.mark.needs_db
def test_system_and_job_tools(env) -> None:
    session = env["session"]
    status = _call(session, "get_system_status")["result"]["structuredContent"]
    assert "queue" in status and "disk" in status
    # Redaction: no key material in tool output.
    assert "sk-" not in json.dumps(status)

    modules = _call(session, "get_module_status")["result"]["structuredContent"]
    assert modules["count"] > 10

    missing_job = _call(session, "get_job_status", job_id="job_01UNKNOWN000000000000000000")
    assert missing_job["result"]["isError"] is True


@pytest.mark.needs_db
def test_compare_papers_requires_two(env) -> None:
    session = env["session"]
    imported = _import_paper(env["client"])
    single = _call(session, "compare_papers", paper_ids=[imported["paper_id"]])
    assert single["result"]["isError"] is True
    assert "CFG_002" in single["result"]["content"][0]["text"]


@pytest.mark.needs_db
def test_analyze_and_risky_claim_tools(env) -> None:
    session = env["session"]
    client = env["client"]
    imported = _import_paper(client)
    collection = client.post("/v1/collections", json={"name": "mcp"}).json()
    client.post(
        f"/v1/collections/{collection['collection_id']}/papers",
        json={"paper_id": imported["paper_id"]},
    )

    analyzed = _call(session, "analyze_collection", collection_id=collection["collection_id"])[
        "result"
    ]["structuredContent"]
    assert "views" in analyzed
    assert len(analyzed["views"]) == 12

    risky = _call(session, "get_risky_claims", limit=5)["result"]["structuredContent"]
    assert "risky_claims" in risky

"""mcp.server — the MCP tool surface (P12, doc 04 §7).

Every tool delegates to paperintel.services.read_models (the SAME layer
REST uses) — there is no MCP-specific business logic. The server speaks
the MCP JSON-RPC shape (initialize / tools/list / tools/call) so an
external agent can drive it, and the tool schemas are generated from one
registry.

Tool errors are returned as MCP tool errors carrying the frozen domain
code (never a bare stack trace), and no secret value ever appears in a
result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from paperintel.errors import DomainError
from paperintel.version import SPEC_VERSION

PROTOCOL_VERSION = "2024-11-05"


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., dict[str, Any]]
    #: Whether the tool may mutate state (informational, surfaced to clients).
    mutating: bool = False
    required: tuple[str, ...] = field(default_factory=tuple)

    def manifest(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}
_STR_ARRAY = {"type": "array", "items": {"type": "string"}}


def build_tools() -> dict[str, Tool]:
    """The doc 04 §7 required tool set, wired to the shared read models."""
    tools: dict[str, Tool] = {}

    def register(
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Callable[..., dict[str, Any]],
        *,
        mutating: bool = False,
    ) -> None:
        tools[name] = Tool(
            name=name,
            description=description,
            input_schema=schema,
            handler=handler,
            mutating=mutating,
            required=tuple(schema.get("required", [])),
        )

    # -- papers ----------------------------------------------------------
    def search_papers(session: Session, query: str | None = None, limit: int = 20, **filters):
        from paperintel.services import read_models

        return read_models.search_dispatch(
            session, kind="papers", query=query, filters=filters, limit=limit
        )

    register(
        "search_papers",
        "Search paper metadata (title/DOI/type) with exact structured filters.",
        _schema(
            {
                "query": _STR,
                "limit": _INT,
                "collection_ids": _STR_ARRAY,
                "tag_namespaces": _STR_ARRAY,
            }
        ),
        search_papers,
    )

    def get_paper(session: Session, paper_id: str):
        from paperintel.services import read_models

        return read_models.paper_summary(session, paper_id)

    register(
        "get_paper",
        "Fetch one paper's identity, latest version and triage tier.",
        _schema({"paper_id": _STR}, ("paper_id",)),
        get_paper,
    )

    def get_paper_context(session: Session, paper_id: str):
        from paperintel.services import read_models

        return read_models.paper_context(session, paper_id)

    register(
        "get_paper_context",
        "The preferred external-AI entry: identity, structure, evidence quality, "
        "claims by support state, tags and versions.",
        _schema({"paper_id": _STR}, ("paper_id",)),
        get_paper_context,
    )

    def get_paper_analysis(session: Session, paper_id: str):
        from paperintel.services import read_models

        return read_models.paper_analysis(session, paper_id)

    register(
        "get_paper_analysis",
        "Claims grouped by category with support states and analysis runs.",
        _schema({"paper_id": _STR}, ("paper_id",)),
        get_paper_analysis,
    )

    def get_paper_audit(session: Session, paper_id: str):
        from paperintel.services import read_models

        return read_models.paper_audit(session, paper_id)

    register(
        "get_paper_audit",
        "Version-level audit overview: support states, unverified claims, "
        "incomplete provenance bundles.",
        _schema({"paper_id": _STR}, ("paper_id",)),
        get_paper_audit,
    )

    # -- claims ----------------------------------------------------------
    def search_claims(session: Session, query: str | None = None, limit: int = 20, **filters):
        from paperintel.services import read_models

        return read_models.search_dispatch(
            session, kind="claims", query=query, filters=filters, limit=limit
        )

    register(
        "search_claims",
        "Search claim statements; supports exact claim_type/support_state/paper filters.",
        _schema(
            {
                "query": _STR,
                "limit": _INT,
                "paper_ids": _STR_ARRAY,
                "claim_types": _STR_ARRAY,
                "support_states": _STR_ARRAY,
            }
        ),
        search_claims,
    )

    def get_claim(session: Session, claim_id: str):
        from paperintel.services import read_models

        return read_models.claim_detail(session, claim_id)

    register(
        "get_claim",
        "One claim with its evidence, verifier verdicts, model calls and provenance.",
        _schema({"claim_id": _STR}, ("claim_id",)),
        get_claim,
    )

    def get_claim_evidence(session: Session, claim_id: str):
        from paperintel.services import read_models

        return read_models.claim_evidence(session, claim_id)

    register(
        "get_claim_evidence",
        "The evidence units a claim cites, with locators and content hashes.",
        _schema({"claim_id": _STR}, ("claim_id",)),
        get_claim_evidence,
    )

    def get_risky_claims(session: Session, paper_id: str | None = None, limit: int = 50):
        """Claims that are disputed/unsupported or whose provenance is
        incomplete — the audit-risk view."""
        from paperintel.services import read_models

        detail_filters = {"paper_ids": [paper_id]} if paper_id else {}
        payload = read_models.search_dispatch(
            session,
            kind="claims",
            query=None,
            filters={**detail_filters, "support_states": ["DISPUTED", "UNSUPPORTED"]},
            limit=limit,
        )
        return {
            "risky_claims": payload["hits"],
            "count": payload["count"],
            "note": "DISPUTED/UNSUPPORTED claims; use get_claim for full audit detail",
        }

    register(
        "get_risky_claims",
        "Claims marked DISPUTED or UNSUPPORTED (optionally scoped to one paper).",
        _schema({"paper_id": _STR, "limit": _INT}),
        get_risky_claims,
    )

    def reverify_claim(session: Session, claim_id: str):
        from paperintel.database.models import ClaimRow
        from paperintel.schemas.enums import ResourceTier
        from paperintel.verification.service import run_verification

        claim = session.get(ClaimRow, claim_id)
        if claim is None:
            raise DomainError(
                "CFG_002",
                message=f"Unknown claim ID: {claim_id}",
                details={"claim_id": claim_id},
            )
        report = run_verification(
            session,
            paper_version_id=claim.paper_version_id,
            tier=ResourceTier.T3_DEEP,
            claim_ids=[claim_id],
        )
        session.refresh(claim)
        return {
            "claim_id": claim_id,
            "support_state": claim.support_state.value,
            "verification_run_id": report.run_id,
        }

    register(
        "reverify_claim",
        "Re-run the complete applicable verifier set for one claim (T3 depth).",
        _schema({"claim_id": _STR}, ("claim_id",)),
        reverify_claim,
        mutating=True,
    )

    # -- evidence --------------------------------------------------------
    def get_evidence(session: Session, evidence_id: str):
        from paperintel.services import read_models

        return read_models.evidence_detail(session, evidence_id)

    register(
        "get_evidence",
        "One evidence unit with its locator, quality state and content hash.",
        _schema({"evidence_id": _STR}, ("evidence_id",)),
        get_evidence,
    )

    def get_page_evidence(session: Session, paper_version_id: str, page: int):
        from paperintel.services import read_models

        return read_models.evidence_for_page(session, paper_version_id, page)

    register(
        "get_page_evidence",
        "Evidence units located on one page of a paper version.",
        _schema({"paper_version_id": _STR, "page": _INT}, ("paper_version_id", "page")),
        get_page_evidence,
    )

    def _typed_evidence(
        session: Session, paper_version_id: str, evidence_type: str, limit: int = 20
    ):
        from paperintel.services import read_models

        return read_models.typed_evidence(session, paper_version_id, evidence_type, limit)

    register(
        "get_table",
        "Table evidence units for a paper version.",
        _schema({"paper_version_id": _STR, "limit": _INT}, ("paper_version_id",)),
        lambda session, paper_version_id, limit=20: _typed_evidence(
            session, paper_version_id, "TABLE", limit
        ),
    )
    register(
        "get_figure",
        "Figure evidence units for a paper version.",
        _schema({"paper_version_id": _STR, "limit": _INT}, ("paper_version_id",)),
        lambda session, paper_version_id, limit=20: _typed_evidence(
            session, paper_version_id, "FIGURE", limit
        ),
    )

    # -- discovery -------------------------------------------------------
    def _entity_search(kind: str):
        def handler(session: Session, query: str | None = None, limit: int = 20, **filters):
            from paperintel.services import read_models

            return read_models.search_dispatch(
                session, kind=kind, query=query, filters=filters, limit=limit
            )

        return handler

    register(
        "search_methods",
        "Search methods (method entities and method claims) across the corpus.",
        _schema({"query": _STR, "limit": _INT, "collection_ids": _STR_ARRAY}),
        _entity_search("methods"),
    )
    register(
        "search_techniques",
        "Search experimental/engineering techniques across the corpus.",
        _schema({"query": _STR, "limit": _INT, "collection_ids": _STR_ARRAY}),
        _entity_search("techniques"),
    )
    register(
        "search_datasets",
        "Search datasets (as reported in experiment claims).",
        _schema({"query": _STR, "limit": _INT, "paper_ids": _STR_ARRAY}),
        lambda session, query=None, limit=20, **filters: _claim_category_search(
            session, ("experiment.dataset",), query, limit, filters
        ),
    )
    register(
        "search_authors",
        "Search authors (AUTHOR entities and people claims).",
        _schema({"query": _STR, "limit": _INT}),
        lambda session, query=None, limit=20, **filters: _claim_category_search(
            session, ("people.author",), query, limit, filters
        ),
    )

    def compare_papers(session: Session, paper_ids: list[str]):
        from paperintel.services import read_models

        return read_models.compare_papers(session, paper_ids)

    register(
        "compare_papers",
        "Compare two or more papers side by side (claims, categories, support).",
        _schema({"paper_ids": _STR_ARRAY}, ("paper_ids",)),
        compare_papers,
    )

    def search_collection(
        session: Session, collection_id: str, query: str | None = None, limit: int = 20
    ):
        from paperintel.services import read_models

        return read_models.search_dispatch(
            session,
            kind="evidence",
            query=query,
            filters={"collection_ids": {collection_id}},
            limit=limit,
        )

    register(
        "search_collection",
        "Search inside one collection's papers (scope-locked).",
        _schema({"collection_id": _STR, "query": _STR, "limit": _INT}, ("collection_id",)),
        search_collection,
    )

    def analyze_collection(session: Session, collection_id: str):
        from paperintel.services import read_models

        detail = read_models.collection_detail(session, collection_id)
        intelligence = read_models.collection_intelligence(session, collection_id)
        return {
            "collection": detail,
            "views": {name: view["finding_count"] for name, view in intelligence["views"].items()},
            "intelligence_run_ids": {
                name: view["run_id"] for name, view in intelligence["views"].items()
            },
        }

    register(
        "analyze_collection",
        "Run the corpus intelligence views over one collection and report "
        "per-view finding counts + run ids.",
        _schema({"collection_id": _STR}, ("collection_id",)),
        analyze_collection,
    )

    # -- system ----------------------------------------------------------
    def get_system_status(session: Session):
        from paperintel.services import read_models

        return read_models.system_status(session)

    register(
        "get_system_status",
        "Queue, providers, disk and pipeline counters (secrets redacted).",
        _schema({}),
        get_system_status,
    )

    def get_job_status(session: Session, job_id: str):
        from paperintel.services import read_models

        return read_models.job_detail(session, job_id)

    register(
        "get_job_status",
        "One job's state, stage progress and tasks.",
        _schema({"job_id": _STR}, ("job_id",)),
        get_job_status,
    )

    def get_module_status(session: Session):
        from paperintel.services import read_models

        return read_models.modules_status()

    register(
        "get_module_status",
        "Bundled module manifests and their declared health checks.",
        _schema({}),
        get_module_status,
    )

    return tools


def _claim_category_search(
    session: Session, prefixes: tuple[str, ...], query: str | None, limit: int, filters: dict
) -> dict[str, Any]:
    """Category-scoped claim search (datasets/authors) through the same
    retrieval layer."""
    from paperintel.services import read_models

    return read_models.search_dispatch(
        session,
        kind="claims",
        query=query,
        filters={**filters, "claim_categories": set(prefixes)},
        limit=limit,
    )


# ---------------------------------------------------------------------------
# JSON-RPC dispatch
# ---------------------------------------------------------------------------


def handle_request(session: Session, request: dict[str, Any]) -> dict[str, Any]:
    """Handle one MCP JSON-RPC request.

    Supported methods: initialize, tools/list, tools/call. Unknown methods
    return a JSON-RPC error (-32601) — never a silent empty result.
    """
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}
    tools = build_tools()

    def ok(result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": "paperintel", "version": SPEC_VERSION},
                "capabilities": {"tools": {"listChanged": False}},
            }
        )
    if method == "tools/list":
        return ok({"tools": [tool.manifest() for tool in tools.values()]})
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        tool = tools.get(name)
        if tool is None:
            return err(-32602, f"Unknown tool: {name!r}")
        missing = [key for key in tool.required if key not in arguments]
        if missing:
            return err(-32602, f"Missing required arguments for {name}: {missing}")
        try:
            result = tool.handler(session, **arguments)
        except DomainError as exc:
            return ok(
                {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"{exc.code}: {exc.message}",
                        }
                    ],
                }
            )
        except TypeError as exc:  # bad argument shape
            return err(-32602, f"Invalid arguments for {name}: {exc}")
        structured = jsonable(result)
        return ok(
            {
                "content": [
                    {"type": "text", "text": _json_text(structured)},
                ],
                "structuredContent": structured,
            }
        )
    return err(-32601, f"Unknown method: {method!r}")


def jsonable(value: Any) -> Any:
    """Convert a service result into plain JSON data.

    Service functions return Pydantic models in places (SectionNode,
    Evidence contracts); MCP requires JSON values, so models are dumped
    recursively — the MCP surface never leaks Python objects.
    """
    if hasattr(value, "model_dump"):
        return jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _json_text(payload: Any) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str, indent=2)


__all__ = ["PROTOCOL_VERSION", "Tool", "build_tools", "handle_request", "jsonable"]

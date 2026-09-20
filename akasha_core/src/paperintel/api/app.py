"""api.app — the REST surface (P12, doc 04 §1-§6, §16).

Conventions (doc 04 §1):
- all endpoints are versioned under /v1;
- responses are JSON; errors use the frozen error envelope
  {"error": {"code", "message", "details"}} with the catalog code;
- authentication: bearer API token (doc 04 §16) — secrets are never logged
  or echoed;
- every endpoint delegates to paperintel.services.read_models (the same
  layer MCP and the CLI use).

The operations UI is served from /ui (doc 04 §15).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from paperintel.config.settings import AppConfig, get_settings
from paperintel.database.base import (
    create_engine_from_settings,
    create_session_factory,
    session_scope,
)
from paperintel.errors import DomainError
from paperintel.services.ui.session import SessionStore, verify_app_token
from paperintel.version import SPEC_VERSION


class ApiState:
    """Process-wide API resources (engine + session factory + UI sessions)."""

    def __init__(self, settings: AppConfig | None = None) -> None:
        self.settings = settings or get_settings()
        self.engine = create_engine_from_settings(self.settings)
        self.session_factory = create_session_factory(self.engine)
        # In-process browser sessions for the local deployment (docs/06).
        self.sessions = SessionStore()


def _state(request: Request) -> ApiState:
    return request.app.state.paperintel


def get_session(request: Request) -> Iterator[Session]:
    """One session per request (never a process-global session)."""
    state = _state(request)
    with session_scope(state.session_factory) as session:
        yield session


def require_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Bearer token auth (doc 04 §16).

    A configured token is REQUIRED when set; an unset token means the local
    deployment has auth disabled (documented, never silent: it is reported
    by /v1/system/status → api.auth_required).
    """
    expected = _state(request).settings.api.token
    if not expected:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.removeprefix("Bearer ").strip()
    if not verify_app_token(presented, expected):
        raise HTTPException(status_code=401, detail="invalid bearer token")


#: Detail keys that identify a looked-up entity: their presence means the
#: caller referenced something that does not exist (404), while their
#: absence means the request itself was malformed (400).
_ENTITY_DETAIL_KEYS = (
    "paper_id",
    "paper_version_id",
    "claim_id",
    "evidence_id",
    "job_id",
    "task_id",
    "collection_id",
    "trace_id",
    "entity_id",
    "asset_id",
)


def error_response(exc: DomainError) -> JSONResponse:
    """Frozen error envelope + a deterministic HTTP status.

    - RESOURCE_* → 507 (insufficient storage, doc 07 §8);
    - a referenced entity that does not exist → 404;
    - everything else → 400 (malformed or rejected request).
    """
    payload = exc.to_envelope()
    if exc.code.startswith("RESOURCE"):
        status = 507
    elif exc.code == "EVIDENCE_001" or any(
        key in (exc.details or {}) for key in _ENTITY_DETAIL_KEYS
    ):
        status = 404
    else:
        status = 400
    from paperintel.operations.debug import redact

    return JSONResponse(redact(payload), status_code=status)


def create_app(settings: AppConfig | None = None, *, state: ApiState | None = None) -> FastAPI:
    app = FastAPI(
        title="Akasha API",
        version=SPEC_VERSION,
        description="Local-first multi-agent paper intelligence pipeline",
    )
    app.state.paperintel = state or ApiState(settings)

    @app.exception_handler(DomainError)
    async def _domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return error_response(exc)

    @app.get("/metrics", response_class=PlainTextResponse, dependencies=[Depends(require_token)])
    def metrics(request: Request, session: Annotated[Session, Depends(get_session)]) -> str:
        from paperintel.services import read_models

        return read_models.metrics_snapshot(session, settings=_state(request).settings)

    _register_system(app)
    _register_papers(app)
    _register_knowledge(app)
    _register_workflow(app)
    _register_collections(app)
    _register_ui(app)
    _register_ui_api(app)
    _register_spa(app)
    return app


def _register_spa(app: FastAPI) -> None:
    """Serve the built workbench at /app (docs/08 §部署, UX-059).

    Rules:
    - assets are served from the built `dist` directory;
    - a DEEP LINK (`/app/library`, `/app/papers/pap_x`) falls back to
      `index.html`, because the SPA owns those routes;
    - `/v1/*` NEVER falls back: an unknown API path stays a JSON 404, so a broken
      client request cannot be answered with HTML;
    - the fallback is registered LAST so real routes win.
    """
    import os
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    configured = os.environ.get("PAPERINTEL_WEB_DIST")
    dist = Path(configured) if configured else Path(__file__).resolve().parents[3] / "web" / "dist"
    if not dist.is_dir():
        # No build present (API-only deployment): say so instead of pretending.
        @app.get("/app", include_in_schema=False)
        @app.get("/app/{path:path}", include_in_schema=False)
        def spa_missing(path: str = "") -> JSONResponse:
            return JSONResponse(
                {
                    "error": {
                        "code": "STORAGE_003",
                        "message": "The workbench build is not deployed in this environment.",
                        "retryable": False,
                        "trace_id": None,
                        "details": {"expected_dist": str(dist)},
                    }
                },
                status_code=404,
            )

        return

    index = dist / "index.html"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/app/assets", StaticFiles(directory=str(assets)), name="spa-assets")

    @app.get("/app", include_in_schema=False)
    @app.get("/app/{path:path}", include_in_schema=False)
    def spa_deep_link(path: str = "") -> FileResponse | JSONResponse:
        candidate = (dist / path).resolve()
        # A real file wins (favicon, worker, manifest); everything else is the SPA
        # shell so a deep link works after a refresh.
        if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index, media_type="text/html")
        return JSONResponse(
            {"error": {"code": "STORAGE_003", "message": "index.html missing.", "details": {}}},
            status_code=404,
        )


def _register_ui_api(app: FastAPI) -> None:
    """Mount the /v1/ui aggregation surface (frontend contract docs/06).

    These routers live in their own modules and reuse the SAME services layer
    as the /v1 REST surface: the browser never gets a parallel code path that
    could drift from the automation contract.
    """
    from paperintel.api.ui_routes import (
        collections_routes,
        document_routes,
        errors,
        import_routes,
        library_routes,
        operations_routes,
        read_routes,
        session_routes,
    )

    errors.register(app)
    for module in (
        session_routes,
        library_routes,
        import_routes,
        document_routes,
        operations_routes,
        collections_routes,
        read_routes,
    ):
        app.include_router(module.router)


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def _register_system(app: FastAPI) -> None:
    @app.get("/v1/system/status", dependencies=[Depends(require_token)])
    def system_status(request: Request, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        payload = read_models.system_status(session, settings=_state(request).settings)
        payload["api"] = {
            "auth_required": bool(_state(request).settings.api.token),
            "version": SPEC_VERSION,
        }
        return payload

    @app.get("/v1/system/version", dependencies=[Depends(require_token)])
    def system_version() -> dict:
        from paperintel.services import read_models

        return read_models.system_version()

    @app.get("/v1/system/modules", dependencies=[Depends(require_token)])
    def system_modules() -> dict:
        from paperintel.services import read_models

        return read_models.modules_status()

    @app.get("/v1/system/providers", dependencies=[Depends(require_token)])
    def system_providers(request: Request) -> dict:
        from paperintel.services import read_models

        return read_models.providers_status(settings=_state(request).settings)

    @app.get("/v1/system/storage", dependencies=[Depends(require_token)])
    def system_storage(request: Request) -> dict:
        from paperintel.services import read_models

        return read_models.storage_status(settings=_state(request).settings)

    @app.get("/v1/system/workers", dependencies=[Depends(require_token)])
    def system_workers(request: Request, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.workers_status(session, settings=_state(request).settings)


# ---------------------------------------------------------------------------
# papers / evidence / claims
# ---------------------------------------------------------------------------


class ImportRequest(BaseModel):
    #: Unknown request fields are REJECTED (422), never silently ignored —
    #: a client typo must not look like a successful query.
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, description="Server-local path to a PDF.")


class TierRequest(BaseModel):
    #: Unknown request fields are REJECTED (422), never silently ignored —
    #: a client typo must not look like a successful query.
    model_config = ConfigDict(extra="forbid")

    tier: str = Field(min_length=3, description="T0_INDEX | T1_SCAN | T2_FULL | T3_DEEP")


def _register_papers(app: FastAPI) -> None:
    @app.post("/v1/papers/import", dependencies=[Depends(require_token)])
    def import_paper(
        payload: ImportRequest,
        request: Request,
        session: Annotated[Session, Depends(get_session)],
    ) -> dict:
        """Import a PDF from a server-local path (doc 04 §2.2).

        The path must exist; nothing is fetched from the network here.
        """
        from pathlib import Path

        from paperintel.storage.object_store import LocalObjectStore

        pdf_path = Path(payload.path)
        if not pdf_path.is_file():
            raise DomainError(
                "STORAGE_001",
                message=f"PDF not found: {pdf_path}",
                details={"path": str(pdf_path)},
            )
        settings = _state(request).settings
        store = LocalObjectStore(Path(settings.core.data_dir) / "objects")
        result = import_pdf_sync(session, pdf_path, store, settings)
        return {
            "paper_id": result.paper_id,
            "paper_version_id": result.paper_version_id,
            "deduplicated": result.deduplicated,
            "reused_asset": result.reused_asset,
            "version_label": result.version_label,
            "imported": True,
        }

    @app.get("/v1/papers/{paper_id}", dependencies=[Depends(require_token)])
    def get_paper(paper_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.paper_summary(session, paper_id)

    @app.get("/v1/papers/{paper_id}/context", dependencies=[Depends(require_token)])
    def get_paper_context(paper_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.paper_context(session, paper_id)

    @app.get("/v1/papers/{paper_id}/analysis", dependencies=[Depends(require_token)])
    def get_paper_analysis(
        paper_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.paper_analysis(session, paper_id)

    @app.get("/v1/papers/{paper_id}/audit", dependencies=[Depends(require_token)])
    def get_paper_audit(paper_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.paper_audit(session, paper_id)

    @app.get("/v1/papers/{paper_id}/claims", dependencies=[Depends(require_token)])
    def get_paper_claims(paper_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.paper_claims(session, paper_id)

    @app.get("/v1/papers/{paper_id}/methods", dependencies=[Depends(require_token)])
    def get_paper_methods(paper_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.paper_profile(session, paper_id, "methods")

    @app.get("/v1/papers/{paper_id}/experiments", dependencies=[Depends(require_token)])
    def get_paper_experiments(
        paper_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.paper_profile(session, paper_id, "experiments")

    @app.get("/v1/papers/{paper_id}/techniques", dependencies=[Depends(require_token)])
    def get_paper_techniques(
        paper_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.paper_profile(session, paper_id, "techniques")

    @app.get("/v1/papers/{paper_id}/people", dependencies=[Depends(require_token)])
    def get_paper_people(paper_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.paper_profile(session, paper_id, "people")

    @app.get("/v1/papers/{paper_id}/evidence", dependencies=[Depends(require_token)])
    def get_paper_evidence(
        paper_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        context = read_models.paper_context(session, paper_id)
        return {"paper_id": paper_id, "evidence": context["evidence"]}

    @app.get("/v1/papers/{paper_id}/pipeline", dependencies=[Depends(require_token)])
    def get_paper_pipeline(
        paper_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.paper_pipeline(session, paper_id)

    @app.post("/v1/papers/{paper_id}/reanalyze", dependencies=[Depends(require_token)])
    def reanalyze(
        paper_id: str,
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        paper_version_id: str | None = None,
    ) -> dict:
        """Queue a fresh analysis pass for a paper version.

        ``paper_version_id`` selects the version to reanalyze; when omitted the
        latest version is used ONCE and reported back, so the client can pin it
        and never silently analyze a different version than the one it showed
        (frontend contract docs/06: the selected version is fixed in the
        response).
        """
        from pathlib import Path

        from sqlalchemy import select

        from paperintel.database.models import EvidenceRow, PaperVersionRow
        from paperintel.schemas.enums import PipelineStage
        from paperintel.workflow import engine

        if paper_version_id is None:
            version = _latest_version_or_fail(session, paper_id)
        else:
            version = session.get(PaperVersionRow, paper_version_id)
            if version is None or version.paper_id != paper_id:
                raise DomainError(
                    "CFG_002",
                    message="Unknown paper_version_id for this paper.",
                    details={
                        "paper_id": paper_id,
                        "paper_version_id": paper_version_id,
                    },
                )
        settings = _state(request).settings
        report_path = (
            Path(settings.core.data_dir)
            / "cache"
            / "extraction_reports"
            / f"{version.content_sha256}.json"
        )
        job = engine.create_job(
            session,
            paper_id=paper_id,
            paper_version_id=version.paper_version_id,
            current_stage=PipelineStage.ANALYZED,
        )
        plan = engine.plan_job(
            session,
            job,
            data_dir=str(settings.core.data_dir),
            content_sha256=version.content_sha256,
            report_path=str(report_path),
        )
        # Scope the evidence probe to the version being analyzed: another
        # version's evidence must never make this version look analyzed.
        has_evidence = (
            session.scalar(
                select(EvidenceRow.evidence_id)
                .where(EvidenceRow.paper_version_id == version.paper_version_id)
                .limit(1)
            )
            is not None
        )
        return {
            "paper_id": paper_id,
            "paper_version_id": version.paper_version_id,
            "job_id": job.job_id,
            "planned": plan,
            "has_evidence": has_evidence,
        }

    @app.post("/v1/papers/{paper_id}/tier", dependencies=[Depends(require_token)])
    def set_tier(
        paper_id: str, payload: TierRequest, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        """Record a manual tier decision (manual override always wins)."""
        from paperintel.schemas.enums import ResourceTier
        from paperintel.triage.service import compute_triage

        try:
            tier = ResourceTier(payload.tier)
        except ValueError as exc:
            raise DomainError(
                "CFG_002",
                message=f"Unknown resource tier: {payload.tier!r}",
                details={"tier": payload.tier},
            ) from exc
        row = compute_triage(session, paper_id=paper_id, requested_tier=tier)
        return {
            "paper_id": paper_id,
            "recommended_tier": row.recommended_tier.value,
            "effective_tier": row.effective_tier.value,
            "manual_override": row.manual_override,
            "reason_codes": row.reason_codes,
        }

    @app.get("/v1/evidence/{evidence_id}", dependencies=[Depends(require_token)])
    def get_evidence(evidence_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.evidence_detail(session, evidence_id)

    @app.get("/v1/evidence/{evidence_id}/asset", dependencies=[Depends(require_token)])
    def get_evidence_asset(
        evidence_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.database.models import AssetRow, EvidenceRow

        row = session.get(EvidenceRow, evidence_id)
        if row is None:
            raise DomainError(
                "EVIDENCE_001",
                message=f"Unknown evidence ID: {evidence_id}",
                details={"evidence_id": evidence_id},
            )
        if not row.asset_id:
            raise DomainError(
                "STORAGE_001",
                message="Evidence has no associated asset.",
                details={"evidence_id": evidence_id},
            )
        asset = session.get(AssetRow, row.asset_id)
        return {
            "evidence_id": evidence_id,
            "asset": {
                "asset_id": asset.asset_id,
                "kind": asset.kind.value,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "storage_key": asset.storage_key,
                "sha256": asset.sha256,
            }
            if asset
            else None,
        }

    @app.get("/v1/claims/{claim_id}", dependencies=[Depends(require_token)])
    def get_claim(claim_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.claim_detail(session, claim_id)

    @app.get("/v1/claims/{claim_id}/evidence", dependencies=[Depends(require_token)])
    def get_claim_evidence(
        claim_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.claim_evidence(session, claim_id)

    @app.get("/v1/claims/{claim_id}/verifications", dependencies=[Depends(require_token)])
    def get_claim_verifications(
        claim_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.claim_verifications(session, claim_id)

    @app.post("/v1/claims/{claim_id}/reverify", dependencies=[Depends(require_token)])
    def reverify_claim(claim_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        """Re-run the applicable verifier set for one claim."""
        from paperintel.database.models import ClaimRow
        from paperintel.schemas.enums import ResourceTier
        from paperintel.verification.service import run_verification

        claim = session.get(ClaimRow, claim_id)
        if claim is None:
            raise DomainError(
                "CFG_002", message=f"Unknown claim ID: {claim_id}", details={"claim_id": claim_id}
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
            "states": report.states,
        }


def import_pdf_sync(session: Session, pdf_path, store, settings) -> Any:
    """Run the async import in the synchronous request context."""
    import asyncio

    from paperintel.ingest.service import import_pdf

    return asyncio.run(
        import_pdf(
            pdf_path,
            session=session,
            store=store,
            data_dir=settings.core.data_dir,
        )
    )


def _latest_version_or_fail(session: Session, paper_id: str):
    from sqlalchemy import select

    from paperintel.database.models import PaperVersionRow

    version = session.scalars(
        select(PaperVersionRow)
        .where(PaperVersionRow.paper_id == paper_id)
        .order_by(PaperVersionRow.created_at.desc())
    ).first()
    if version is None:
        raise DomainError(
            "CFG_002",
            message="Paper has no imported versions.",
            details={"paper_id": paper_id},
        )
    return version


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    #: Unknown request fields are REJECTED (422), never silently ignored —
    #: a client typo must not look like a successful query.
    model_config = ConfigDict(extra="forbid")

    query: str | None = None
    limit: int = Field(default=20, ge=1, le=200)
    paper_ids: list[str] | None = None
    paper_version_ids: list[str] | None = None
    collection_ids: list[str] | None = None
    section_classes: list[str] | None = None
    evidence_types: list[str] | None = None
    claim_types: list[str] | None = None
    support_states: list[str] | None = None
    tag_namespaces: list[str] | None = None
    tag_names: list[str] | None = None
    entity_types: list[str] | None = None


def _register_knowledge(app: FastAPI) -> None:
    def _filters(payload: SearchRequest) -> dict:
        mapping = {
            "paper_ids": payload.paper_ids,
            "paper_version_ids": payload.paper_version_ids,
            "collection_ids": payload.collection_ids,
            "section_classes": payload.section_classes,
            "evidence_types": payload.evidence_types,
            "claim_types": payload.claim_types,
            "support_states": payload.support_states,
            "tag_namespaces": payload.tag_namespaces,
            "tag_names": payload.tag_names,
            "entity_types": payload.entity_types,
        }
        return {key: set(value) for key, value in mapping.items() if value}

    def _embedder(request: Request):
        from paperintel.services.embedding import configured_embedder

        try:
            return configured_embedder(_state(request).settings)
        except DomainError:
            return None

    def _run(kind: str, payload: SearchRequest, request: Request, session: Session) -> dict:
        from paperintel.services import read_models

        return read_models.search_dispatch(
            session,
            kind=kind,
            query=payload.query,
            filters=_filters(payload),
            limit=payload.limit,
            embedder=_embedder(request),
        )

    @app.post("/v1/search/papers", dependencies=[Depends(require_token)])
    def search_papers(
        payload: SearchRequest, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        return _run("papers", payload, request, session)

    @app.post("/v1/search/claims", dependencies=[Depends(require_token)])
    def search_claims(
        payload: SearchRequest, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        return _run("claims", payload, request, session)

    @app.post("/v1/search/evidence", dependencies=[Depends(require_token)])
    def search_evidence(
        payload: SearchRequest, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        return _run("evidence", payload, request, session)

    @app.post("/v1/search/entities", dependencies=[Depends(require_token)])
    def search_entities(
        payload: SearchRequest, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        return _run("entities", payload, request, session)

    @app.post("/v1/search/techniques", dependencies=[Depends(require_token)])
    def search_techniques(
        payload: SearchRequest, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        return _run("techniques", payload, request, session)

    @app.post("/v1/search/methods", dependencies=[Depends(require_token)])
    def search_methods(
        payload: SearchRequest, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        return _run("methods", payload, request, session)


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------


def _register_workflow(app: FastAPI) -> None:
    @app.get("/v1/jobs", dependencies=[Depends(require_token)])
    def list_jobs(
        session: Annotated[Session, Depends(get_session)],
        state: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict:
        from paperintel.services import read_models

        return read_models.job_list(session, state=state, limit=limit)

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
    def get_job(job_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.job_detail(session, job_id)

    @app.get("/v1/jobs/{job_id}/stages", dependencies=[Depends(require_token)])
    def get_job_stages(job_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        detail = read_models.job_detail(session, job_id)
        stages: dict[str, dict] = {}
        for task in detail["tasks"]:
            stage = task["task_type"].split(".", 1)[0]
            entry = stages.setdefault(stage, {"tasks": [], "states": {}})
            entry["tasks"].append(task["task_id"])
            entry["states"][task["state"]] = entry["states"].get(task["state"], 0) + 1
        return {
            "job_id": job_id,
            "current_stage": detail["job"]["current_stage"],
            "stages": stages,
        }

    @app.get("/v1/jobs/{job_id}/tasks", dependencies=[Depends(require_token)])
    def get_job_tasks(job_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        detail = read_models.job_detail(session, job_id)
        return {"job_id": job_id, "tasks": detail["tasks"]}

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
    def cancel_job(job_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.workflow import engine

        job = engine.cancel_job(session, job_id)
        return {"job_id": job_id, "state": job.state.value}

    @app.post("/v1/jobs/{job_id}/resume", dependencies=[Depends(require_token)])
    def resume_job(job_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.workflow import engine

        tasks = engine.resume_job(session, job_id)
        return {
            "job_id": job_id,
            "requeued": [task.task_id for task in tasks],
        }

    @app.get("/v1/tasks/{task_id}", dependencies=[Depends(require_token)])
    def get_task(task_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.task_detail(session, task_id)

    @app.post("/v1/tasks/{task_id}/replay", dependencies=[Depends(require_token)])
    def replay_task(
        task_id: str,
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        eager: bool = False,
    ) -> dict:
        """Re-execute one task through the standard runner."""
        from paperintel.database.models import TaskRow
        from paperintel.workflow.celery_app import run_task_once, submit_task

        task = session.get(TaskRow, task_id)
        if task is None:
            raise DomainError(
                "CFG_002", message=f"Unknown task ID: {task_id}", details={"task_id": task_id}
            )
        if task.state.value in (
            "SUCCEEDED",
            "SUCCEEDED_WITH_WARNINGS",
            "FAILED",
            "CANCELLED",
            "SKIPPED",
        ):
            raise DomainError(
                "INTERNAL_002",
                message=(
                    f"Task {task_id} is terminal ({task.state.value}); use the job rerun "
                    "endpoint to create a fresh replay task."
                ),
                details={"task_id": task_id, "state": task.state.value},
            )
        if eager:
            outcome = run_task_once(session, task_id)
            session.commit()
            return {"task_id": task_id, "replayed": outcome}
        dispatched = submit_task(task_id, database_url=_state(request).settings.database.url)
        return {"task_id": task_id, "dispatched": dispatched}

    @app.get("/v1/traces/{trace_id}", dependencies=[Depends(require_token)])
    def get_trace(trace_id: str, session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.trace_detail(session, trace_id)


# ---------------------------------------------------------------------------
# collections
# ---------------------------------------------------------------------------


class CollectionRequest(BaseModel):
    #: Unknown request fields are REJECTED (422), never silently ignored —
    #: a client typo must not look like a successful query.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    purpose: str = ""
    research_questions: list[str] | None = None
    preferred_tags: list[dict] | None = None
    relevance_notes: str = ""


class CollectionPaperRequest(BaseModel):
    #: Unknown request fields are REJECTED (422), never silently ignored —
    #: a client typo must not look like a successful query.
    model_config = ConfigDict(extra="forbid")

    paper_id: str
    pinned: bool = False
    priority_override_tier: str | None = None
    relevance_note: str | None = None


def _register_collections(app: FastAPI) -> None:
    @app.post("/v1/collections", dependencies=[Depends(require_token)])
    def create_collection(
        payload: CollectionRequest, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.knowledge.collections import create_collection as create

        row = create(
            session,
            name=payload.name,
            purpose=payload.purpose,
            research_questions=payload.research_questions,
            preferred_tags=payload.preferred_tags,
            relevance_notes=payload.relevance_notes,
        )
        return {"collection_id": row.collection_id, "name": row.name}

    @app.get("/v1/collections", dependencies=[Depends(require_token)])
    def list_collections(session: Annotated[Session, Depends(get_session)]) -> dict:
        from paperintel.services import read_models

        return read_models.collection_list(session)

    @app.get("/v1/collections/{collection_id}", dependencies=[Depends(require_token)])
    def get_collection(
        collection_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.collection_detail(session, collection_id)

    @app.post("/v1/collections/{collection_id}/papers", dependencies=[Depends(require_token)])
    def add_collection_paper(
        collection_id: str,
        payload: CollectionPaperRequest,
        session: Annotated[Session, Depends(get_session)],
    ) -> dict:
        from paperintel.knowledge.collections import add_paper
        from paperintel.schemas.enums import ResourceTier

        override = None
        if payload.priority_override_tier:
            try:
                override = ResourceTier(payload.priority_override_tier)
            except ValueError as exc:
                raise DomainError(
                    "CFG_002",
                    message=f"Unknown tier: {payload.priority_override_tier!r}",
                    details={},
                ) from exc
        link, changed = add_paper(
            session,
            collection_id=collection_id,
            paper_id=payload.paper_id,
            pinned=payload.pinned,
            priority_override_tier=override,
            relevance_note=payload.relevance_note,
        )
        return {
            "collection_id": collection_id,
            "paper_id": link.paper_id,
            "pinned": link.pinned,
            "changed": changed,
        }

    @app.delete(
        "/v1/collections/{collection_id}/papers/{paper_id}",
        dependencies=[Depends(require_token)],
    )
    def remove_collection_paper(
        collection_id: str, paper_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.knowledge.collections import remove_paper

        removed = remove_paper(session, collection_id=collection_id, paper_id=paper_id)
        return {"collection_id": collection_id, "paper_id": paper_id, "removed": removed}

    @app.post("/v1/collections/{collection_id}/analyze", dependencies=[Depends(require_token)])
    def analyze_collection(
        collection_id: str, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        """Queue analysis jobs for the collection's papers."""
        from paperintel.knowledge.collections import collection_paper_ids, get_collection

        get_collection(session, collection_id)
        queued = []
        for paper_id in collection_paper_ids(session, collection_id):
            try:
                version = _latest_version_or_fail(session, paper_id)
            except DomainError:
                continue
            from pathlib import Path

            from paperintel.schemas.enums import PipelineStage
            from paperintel.workflow import engine

            settings = _state(request).settings
            job = engine.create_job(
                session,
                paper_id=paper_id,
                paper_version_id=version.paper_version_id,
                current_stage=PipelineStage.ANALYZED,
            )
            engine.plan_job(
                session,
                job,
                data_dir=str(settings.core.data_dir),
                content_sha256=version.content_sha256,
                report_path=str(
                    Path(settings.core.data_dir)
                    / "cache"
                    / "extraction_reports"
                    / f"{version.content_sha256}.json"
                ),
            )
            queued.append({"paper_id": paper_id, "job_id": job.job_id})
        return {"collection_id": collection_id, "queued": queued}

    @app.get("/v1/collections/{collection_id}/intelligence", dependencies=[Depends(require_token)])
    def collection_intelligence(
        collection_id: str, session: Annotated[Session, Depends(get_session)]
    ) -> dict:
        from paperintel.services import read_models

        return read_models.collection_intelligence(session, collection_id)


# ---------------------------------------------------------------------------
# operations UI (doc 04 §15)
# ---------------------------------------------------------------------------


def _register_ui(app: FastAPI) -> None:
    @app.get("/ui", response_class=HTMLResponse, dependencies=[Depends(require_token)])
    def operations_ui(request: Request, session: Annotated[Session, Depends(get_session)]) -> str:
        from paperintel.web.ui import render_operations_view

        return render_operations_view(session, settings=_state(request).settings)

    @app.get(
        "/ui/papers/{paper_id}", response_class=HTMLResponse, dependencies=[Depends(require_token)]
    )
    def paper_ui(
        paper_id: str, request: Request, session: Annotated[Session, Depends(get_session)]
    ) -> str:
        from paperintel.web.ui import render_paper_view

        return render_paper_view(session, paper_id, settings=_state(request).settings)

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return (
            "<html><body><h1>Akasha</h1>"
            '<p><a href="/ui">operations view</a> · '
            '<a href="/docs">API docs</a> · <a href="/metrics">metrics</a></p>'
            "</body></html>"
        )


__all__ = ["ApiState", "create_app", "require_token"]

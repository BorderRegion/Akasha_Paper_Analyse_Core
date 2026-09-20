"""workflow.handlers — stage task implementations (P05).

Each handler is a pure function (session, task) → output_manifest dict.
Handlers are idempotent BY CONSTRUCTION: they call the same idempotent
canonical stores as the interactive path (P03/P04 services), so a
re-executed task reuses rows instead of duplicating canonical outputs.

Later-phase stages have no handlers yet; the planner records them as
SKIPPED tasks naming the delivering phase (workflow.engine.FUTURE_STAGE_OWNER).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import TaskRow
from paperintel.errors import DomainError
from paperintel.schemas.enums import DataQualityState

TASK_EXTRACT_REPORT = "extract.rebuild_report"
TASK_STRUCTURE_SECTIONS = "structure.reconstruct_sections"
TASK_EVIDENCE_PERSIST = "evidence.persist"
TASK_TRIAGE_PAPER = "triage.paper"
TASK_RUN_AGENTS = "agents.run_suite"
TASK_RESOLVE_METADATA = "metadata.resolve"
TASK_VERIFY_CLAIMS = "verification.run"
TASK_SYNTHESIZE = "agents.synthesize"
TASK_LINK_ENTITIES = "graph.link_entities"
TASK_INDEX_SEARCH = "search.index"


def run_handler(session: Session, task: TaskRow) -> dict[str, Any]:
    """Dispatch to the task's handler; unknown task types fail loudly
    (never silently skipped)."""
    handler = HANDLERS.get(task.task_type)
    if handler is None:
        raise DomainError(
            "QUEUE_001",
            message=f"No handler registered for task type {task.task_type!r}.",
            details={"task_type": task.task_type},
        )
    return handler(session, task)


def handle_rebuild_report(session: Session, task: TaskRow) -> dict[str, Any]:
    """EXTRACTED stage: guarantee a cached extraction report for the
    version. Reuses the content-addressed cache; rebuilds WITHOUT OCR
    only when the cache is missing (recorded in the manifest)."""
    from paperintel.schemas.extraction import ExtractionReport  # noqa: PLC0415

    manifest = task.input_manifest
    report_path = manifest.get("report_path")
    content_sha = manifest.get("content_sha256")
    if not report_path or not content_sha:
        raise DomainError(
            "CFG_002",
            message="extract task requires report_path and content_sha256.",
            details={"task_id": task.task_id},
        )
    path = Path(report_path)
    rebuilt = False
    if not path.is_file():
        # A missing cache for an IMPORTED version means the import ran
        # before the cache existed; rebuild deterministically without OCR
        # (no provider is available in a stage task — recorded, not hidden).
        from paperintel.database.models import AssetRow, PaperVersionRow  # noqa: PLC0415
        from paperintel.extraction.native import extract_document_units  # noqa: PLC0415
        from paperintel.extraction.page_classifier import classify_page  # noqa: PLC0415
        from paperintel.extraction.pdf_document import inspect_document, open_pdf  # noqa: PLC0415
        from paperintel.extraction.report import build_report  # noqa: PLC0415
        from paperintel.ids import new_run_id  # noqa: PLC0415
        from paperintel.schemas.common import utcnow  # noqa: PLC0415
        from paperintel.schemas.enums import SourceMethod  # noqa: PLC0415
        from paperintel.schemas.extraction import PageExtraction  # noqa: PLC0415
        from paperintel.storage.object_store import LocalObjectStore  # noqa: PLC0415

        version = session.get(PaperVersionRow, task.job.paper_version_id)
        asset = session.get(AssetRow, version.asset_id)
        data_dir = Path(manifest["data_dir"])
        store = LocalObjectStore(data_dir / "objects")
        data = store.get(asset.storage_key)
        doc = open_pdf(data)
        try:
            inspection = inspect_document(doc)
            units = extract_document_units(doc)
            pages = [
                PageExtraction(
                    page_number=page.page_number,
                    classification=classify_page(page),
                    units=units.get(page.page_number, []),
                    page_source_method=SourceMethod.PDF_NATIVE,
                )
                for page in inspection.pages
            ]
            report = build_report(
                run_id=new_run_id(),
                paper_version_id=version.paper_version_id,
                started_at=utcnow(),
                finished_at=utcnow(),
                inspection=inspection,
                pages=pages,
                ocr_failures={},
                warnings=["report rebuilt from canonical asset WITHOUT OCR fallback"],
            )
        finally:
            doc.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(report.model_dump_json())
        tmp.replace(path)
        rebuilt = True
    report = ExtractionReport.model_validate(json.loads(path.read_text()))
    quality = report.quality_state
    return {
        "report_path": str(path),
        "run_id": report.run_id,
        "page_count": report.page_count,
        "unit_total": report.unit_total,
        "quality_state": quality.value,
        "rebuilt_without_ocr": rebuilt,
    }


def handle_structure_sections(session: Session, task: TaskRow) -> dict[str, Any]:
    """STRUCTURED stage: reconstruct + persist the section tree for the
    version (idempotent — existing trees are reused unchanged)."""
    from paperintel.evidence.store import persist_sections  # noqa: PLC0415

    report = _load_report(task)
    mapping, created, reused = persist_sections(session, report, paper_id=task.job.paper_id)
    warnings = ["sections reused (tree already present)"] if reused else []
    return {
        "sections_total": len(mapping),
        "sections_created": created,
        "sections_reused": reused,
        "warnings": warnings,
    }


def handle_evidence_persist(session: Session, task: TaskRow) -> dict[str, Any]:
    """EVIDENCE_INDEXED stage: persist the extraction report into the
    immutable evidence store (idempotent)."""
    from paperintel.evidence.store import persist_extraction  # noqa: PLC0415

    report = _load_report(task)
    result = persist_extraction(session, report, paper_id=task.job.paper_id)
    return {
        "evidence_created": result.evidence_created,
        "evidence_reused": result.evidence_reused,
        "excluded": result.excluded,
        "warnings": result.warnings,
    }


def handle_triage_paper(session: Session, task: TaskRow) -> dict[str, Any]:
    """TRIAGED stage: compute + persist the resource-tier decision
    (doc 03 §16 — resource allocation only, never paper quality)."""
    from paperintel.schemas.enums import ResourceTier  # noqa: PLC0415
    from paperintel.triage.service import compute_triage  # noqa: PLC0415

    requested = task.input_manifest.get("requested_tier", ResourceTier.T1_SCAN.value)
    try:
        tier = ResourceTier(str(requested))
    except ValueError as exc:
        raise DomainError(
            "CFG_002",
            message=f"Invalid requested_tier in task manifest: {requested!r}",
            details={"task_id": task.task_id},
        ) from exc

    row = compute_triage(
        session,
        paper_id=task.job.paper_id,
        requested_tier=tier,
        run_id=None,
    )
    return {
        "triage_id": row.triage_id,
        "recommended_tier": row.recommended_tier.value,
        "effective_tier": row.effective_tier.value,
        "manual_override": row.manual_override,
        "reason_codes": row.reason_codes,
    }


def handle_run_agents(session: Session, task: TaskRow) -> dict[str, Any]:
    """ANALYZED stage: run the registered agent suite for the version and
    persist validated claims into the ledger.

    The LLM provider comes from the configured providers file (no
    process-local state, doc 02 §3): the first configured LLM provider
    serves the analyst role. Failures propagate as task failures with
    their domain codes — nothing is swallowed.
    """
    import asyncio  # noqa: PLC0415

    from paperintel.agents.base import AGENTS, run_agent  # noqa: PLC0415
    from paperintel.agents.builtin import SynthesizerAgent  # noqa: PLC0415
    from paperintel.agents.prompts import ensure_builtin_prompts  # noqa: PLC0415
    from paperintel.knowledge.claims import persist_agent_result  # noqa: PLC0415
    from paperintel.operations.disk import require_capacity  # noqa: PLC0415
    from paperintel.schemas.agent import AgentRequest  # noqa: PLC0415
    from paperintel.schemas.enums import ResourceTier  # noqa: PLC0415
    from paperintel.triage.budget import agent_allowed, budget_for_tier  # noqa: PLC0415
    from paperintel.triage.service import latest_triage  # noqa: PLC0415

    paper_id = task.job.paper_id
    paper_version_id = task.job.paper_version_id

    # Analysis budget policy (P10, doc 07 §2): the paper's tier decides
    # which agents run. Low disk blocks this deep expansion (RESOURCE_001).
    triage_row = latest_triage(session, paper_id)
    effective_tier = triage_row.effective_tier if triage_row else ResourceTier.T2_FULL
    require_capacity(tier=effective_tier, operation="agent suite")
    budget = budget_for_tier(effective_tier)

    provider = _resolve_llm_provider()

    ensure_builtin_prompts(session)

    agent_types = [
        agent_type
        for agent_type in sorted(AGENTS)
        # The synthesizer runs in P08 (SYNTHESIZED) once verified claims
        # exist; ANALYZED never schedules it (stage ownership, not a skip).
        if agent_type != SynthesizerAgent.agent_type and agent_allowed(effective_tier, agent_type)
    ]

    summary: dict[str, Any] = {
        "agents": {},
        "claims_created": 0,
        "runs": [],
        "effective_tier": effective_tier.value,
        "budget": {
            "agent_types": list(budget.agent_types) if budget.agent_types else "all",
            "max_claims": budget.max_claims,
            "context_units": budget.context_units,
        },
    }
    warnings: list[str] = []
    async def run_suite():
        try:
            for agent_type in agent_types:
                request = AgentRequest(
                    paper_id=paper_id,
                    paper_version_id=paper_version_id,
                    agent_type=agent_type,
                    task_id=task.task_id,
                    trace_id=task.job.trace_id,
                )
                try:
                    outcome = await run_agent(request, session, provider=provider)
                except DomainError as exc:
                    raise DomainError(
                        exc.code,
                        message=f"Agent {agent_type} failed: {exc.message}",
                        details={**exc.details, "agent_type": agent_type},
                    ) from exc
                ledger = persist_agent_result(session, request, outcome)
                summary["agents"][agent_type] = {
                    "status": outcome.result.status.value,
                    "claims_created": ledger.claims_created,
                    "run_id": ledger.run_id,
                }
                summary["claims_created"] += ledger.claims_created
                summary["runs"].append(ledger.run_id)
                warnings.extend(f"{agent_type}: {w}" for w in outcome.warnings)
        finally:
            if hasattr(provider, "aclose"):
                await provider.aclose()

    asyncio.run(run_suite())

    if warnings:
        summary["warnings"] = warnings
    return summary


def _resolve_llm_provider():
    """Build the analyst LLM provider from the configured providers file.

    The first configured LLM-family provider serves the analyst role;
    absence is CFG_001 (configuration problem, never a silent fallback to
    a fake provider).
    """
    from paperintel.config.provider_config import load_provider_config  # noqa: PLC0415
    from paperintel.config.settings import get_settings  # noqa: PLC0415
    from paperintel.providers.factory import build_registry  # noqa: PLC0415
    from paperintel.providers.registry import ProviderRegistry  # noqa: PLC0415
    from paperintel.schemas.enums import ProviderFamily  # noqa: PLC0415

    settings = get_settings()
    if settings.providers_file is None:
        raise DomainError(
            "CFG_001",
            message="No providers file configured — the ANALYZED stage needs an LLM provider.",
            details={"providers_file": None},
        )
    config = load_provider_config(settings.providers_file)
    registry: ProviderRegistry = build_registry(config)
    llm_providers = registry.by_family(ProviderFamily.LLM)
    if not llm_providers:
        raise DomainError(
            "CFG_001",
            message="No LLM provider configured — the ANALYZED stage needs one.",
            details={"providers_file": str(settings.providers_file)},
        )
    name = sorted(llm_providers)[0]
    return registry.get_llm(name)


def handle_resolve_metadata(session: Session, task: TaskRow) -> dict[str, Any]:
    """METADATA_RESOLVED stage: enrich the paper from a configured external
    metadata source (spec doc 01 §14).

    Without a metadata provider the task completes with an explicit
    warning in its output manifest — degraded, never a silent success and
    never a fabricated record. External sources being unavailable must not
    fail the pipeline (doc 01 §14).
    """
    import asyncio  # noqa: PLC0415

    from paperintel.database.models import PaperRow  # noqa: PLC0415

    paper = session.get(PaperRow, task.job.paper_id)
    if paper is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown paper ID: {task.job.paper_id}",
            details={"task_id": task.task_id},
        )

    provider = _resolve_metadata_provider()
    if provider is None:
        return {
            "resolved": False,
            "warnings": ["no metadata provider configured — external metadata unavailable"],
            "doi": paper.doi,
        }

    record = None
    lookup = "doi" if paper.doi else "title"
    try:
        if paper.doi:
            record = asyncio.run(provider.fetch_by_doi(paper.doi))
        else:
            hits = asyncio.run(provider.search_by_title(paper.canonical_title, limit=1))
            record = hits[0] if hits else None
    except DomainError as exc:
        # External metadata unavailability degrades, never fails (doc 01 §14).
        return {
            "resolved": False,
            "warnings": [f"metadata lookup failed ({exc.code}); pipeline continues"],
            "doi": paper.doi,
        }

    if record is None:
        return {
            "resolved": False,
            "warnings": [f"no external metadata found for this paper ({lookup} lookup)"],
            "doi": paper.doi,
        }

    data = record.data or {}
    from paperintel.knowledge.external import persist_metadata

    external_claim_ids = persist_metadata(
        session,
        record,
        paper_id=paper.paper_id,
        paper_version_id=task.job.paper_version_id,
        trace_id=task.trace_id,
    )
    if data.get("doi") and not paper.doi:
        paper.doi = str(data["doi"])
    if data.get("title"):
        from paperintel.ingest.fingerprint import normalize_title

        paper.canonical_title = str(data["title"])
        paper.normalized_title = normalize_title(paper.canonical_title)
    if data.get("paper_type"):
        paper.paper_type = str(data["paper_type"])
    return {
        "resolved": True,
        "identifier": record.identifier,
        "provider": record.provider,
        "source_url": record.source_url,
        "fields": sorted(data),
        "external_claim_ids": external_claim_ids,
        "warnings": [],
    }


async def _run_agent_and_close(request, session, *, provider, **kwargs):
    from paperintel.agents.base import run_agent

    try:
        return await run_agent(request, session, provider=provider, **kwargs)
    finally:
        if hasattr(provider, "aclose"):
            await provider.aclose()


def handle_verify_claims(session: Session, task: TaskRow) -> dict[str, Any]:
    """VERIFIED stage: run the tier-applicable verifier set over the
    version's claims and apply support-state transitions."""
    from paperintel.schemas.enums import ResourceTier  # noqa: PLC0415
    from paperintel.verification.service import run_verification  # noqa: PLC0415

    if task.input_manifest.get("model_role"):
        import asyncio

        from paperintel.agents.prompts import ensure_builtin_prompts
        from paperintel.knowledge.claims import persist_agent_result
        from paperintel.schemas.agent import AgentRequest
        from paperintel.schemas.enums import ModelRole

        try:
            role = ModelRole(task.input_manifest["model_role"])
        except ValueError as exc:
            raise DomainError("CFG_002", message="Unknown verification model role.") from exc
        provider = _resolve_llm_provider()
        if hasattr(provider, "models") and role.value not in provider.models:
            raise DomainError(
                "CFG_002", message=f"Selected provider has no configured {role.value} model."
            )
        ensure_builtin_prompts(session)
        request = AgentRequest(
            paper_id=task.job.paper_id,
            paper_version_id=task.job.paper_version_id,
            agent_type="agents.reliability",
            task_id=task.task_id,
            trace_id=task.trace_id,
        )
        outcome = asyncio.run(_run_agent_and_close(request, session, provider=provider, model_role=role))
        persist_agent_result(session, request, outcome)

    tier_value = task.input_manifest.get("tier")
    tier = ResourceTier(str(tier_value)) if tier_value else None
    report = run_verification(
        session, paper_version_id=task.job.paper_version_id, tier=tier,
        claim_ids=task.input_manifest.get("claim_ids"),
    )
    return {
        "verification_run_id": report.run_id,
        "effective_tier": report.effective_tier,
        "claims_verified": report.claims_verified,
        "states": report.states,
        "transitions": report.transitions,
    }


def handle_synthesize(session: Session, task: TaskRow) -> dict[str, Any]:
    """SYNTHESIZED stage: run the synthesizer agent over the VERIFIED
    claim set (doc 01 §9.13 — verified/allowed outputs only)."""
    import asyncio  # noqa: PLC0415

    from paperintel.agents.prompts import ensure_builtin_prompts  # noqa: PLC0415
    from paperintel.knowledge.claims import persist_agent_result  # noqa: PLC0415
    from paperintel.schemas.agent import AgentRequest  # noqa: PLC0415

    ensure_builtin_prompts(session)
    request = AgentRequest(
        paper_id=task.job.paper_id,
        paper_version_id=task.job.paper_version_id,
        agent_type="agents.synthesizer",
        task_id=task.task_id,
        trace_id=task.job.trace_id,
    )
    outcome = asyncio.run(_run_agent_and_close(request, session, provider=_resolve_llm_provider()))
    ledger = persist_agent_result(session, request, outcome)
    return {
        "status": outcome.result.status.value,
        "claims_created": ledger.claims_created,
        "run_id": ledger.run_id,
        "warnings": list(outcome.warnings),
    }


def _resolve_metadata_provider():
    """Configured METADATA-family provider, or None when none is set up
    (the caller records an explicit degraded warning)."""
    from paperintel.config.provider_config import load_provider_config  # noqa: PLC0415
    from paperintel.config.settings import get_settings  # noqa: PLC0415
    from paperintel.providers.factory import build_registry  # noqa: PLC0415
    from paperintel.schemas.enums import ProviderFamily  # noqa: PLC0415

    settings = get_settings()
    if settings.providers_file is None:
        return None
    config = load_provider_config(settings.providers_file)
    registry = build_registry(config)
    providers = registry.by_family(ProviderFamily.METADATA)
    if not providers:
        return None
    return registry.get(sorted(providers)[0])


def handle_link_entities(session: Session, task: TaskRow) -> dict[str, Any]:
    """LINKED stage: build the entity graph from the version's claims plus
    paper-level tags (every relation carries its evidence IDs)."""
    from paperintel.database.models import ClaimRow  # noqa: PLC0415
    from paperintel.graph.service import link_claim_entities  # noqa: PLC0415
    from paperintel.knowledge.tags import attach_tag  # noqa: PLC0415

    claims = list(
        session.scalars(
            select(ClaimRow)
            .where(ClaimRow.paper_version_id == task.job.paper_version_id)
            .order_by(ClaimRow.created_at, ClaimRow.claim_id)
        )
    )
    stats = link_claim_entities(session, paper_id=task.job.paper_id, claims=claims)

    # Paper-level tags come from the claim categories (candidate links: a
    # category is not yet a reviewed canonical tag — doc 01 §15).
    tags_attached = 0
    seen_categories: set[str] = set()
    for claim in claims:
        if claim.category in seen_categories:
            continue
        seen_categories.add(claim.category)
        namespace, _, name = claim.category.partition(".")
        if not name:
            continue
        if namespace not in (
            "domain",
            "task",
            "modality",
            "method",
            "architecture",
            "training",
            "inference",
            "dataset",
            "metric",
            "experimental-trick",
            "hardware",
            "efficiency",
            "novelty",
            "reliability-risk",
            "venue",
            "author",
            "affiliation",
            "application",
            "project",
        ):
            continue
        _, created = attach_tag(
            session,
            paper_id=task.job.paper_id,
            namespace=namespace,
            name=name,
            created_by_run_id=claim.created_by_run_id,
            is_candidate=True,
        )
        tags_attached += 1 if created else 0

    return {
        **stats,
        "claims_considered": len(claims),
        "tags_attached": tags_attached,
    }


def handle_index_search(session: Session, task: TaskRow) -> dict[str, Any]:
    """SEARCH_INDEXED stage: build the derived search projection (FTS +
    embeddings) for the version."""
    from paperintel.search.index import index_version  # noqa: PLC0415

    report = index_version(
        session, task.job.paper_version_id, embedder=_resolve_embedding_provider()
    )
    return {
        "documents_written": report.documents_written,
        "documents_reused": report.documents_reused,
        "embeddings_written": report.embeddings_written,
        "warnings": report.warnings,
    }


def _resolve_embedding_provider():
    """Configured EMBEDDING-family provider; None when absent (the search
    index still builds FTS-only, and the caller records that explicitly)."""
    from paperintel.config.provider_config import load_provider_config  # noqa: PLC0415
    from paperintel.config.settings import get_settings  # noqa: PLC0415
    from paperintel.providers.factory import build_registry  # noqa: PLC0415
    from paperintel.schemas.enums import ProviderFamily  # noqa: PLC0415

    settings = get_settings()
    if settings.providers_file is None:
        return None
    config = load_provider_config(settings.providers_file)
    registry = build_registry(config)
    providers = registry.by_family(ProviderFamily.EMBEDDING)
    if not providers:
        return None
    return registry.get(sorted(providers)[0])


HANDLERS = {
    TASK_EXTRACT_REPORT: handle_rebuild_report,
    TASK_STRUCTURE_SECTIONS: handle_structure_sections,
    TASK_EVIDENCE_PERSIST: handle_evidence_persist,
    TASK_TRIAGE_PAPER: handle_triage_paper,
    TASK_RUN_AGENTS: handle_run_agents,
    TASK_RESOLVE_METADATA: handle_resolve_metadata,
    TASK_VERIFY_CLAIMS: handle_verify_claims,
    TASK_SYNTHESIZE: handle_synthesize,
    TASK_LINK_ENTITIES: handle_link_entities,
    TASK_INDEX_SEARCH: handle_index_search,
}


def _load_report(task: TaskRow):
    from paperintel.ingest.service import _load_cached_report  # noqa: PLC0415

    report_path = task.input_manifest.get("report_path")
    if not report_path:
        raise DomainError(
            "CFG_002",
            message="task input manifest requires report_path.",
            details={"task_id": task.task_id},
        )
    path = Path(report_path)
    if not path.is_file():
        raise DomainError(
            "STORAGE_001",
            message=(f"Extraction report cache missing: {path} (run the EXTRACTED stage first)."),
            details={"task_id": task.task_id, "report_path": report_path},
        )
    report = _load_cached_report(path)
    if report is None:
        raise DomainError(
            "STORAGE_001",
            message=f"Extraction report cache is corrupt: {path}",
            details={"task_id": task.task_id, "report_path": report_path},
        )
    if report.paper_version_id is None:
        report = report.model_copy(update={"paper_version_id": task.job.paper_version_id})
    return report


def quality_from_output(output: dict[str, Any]) -> DataQualityState:
    """Task-level data quality derived from output warnings (execution
    state stays a separate column; doc 02 §10)."""
    warnings = output.get("warnings") or []
    excluded = output.get("excluded") or {}
    if warnings or excluded:
        return DataQualityState.DEGRADED
    return DataQualityState.GOOD

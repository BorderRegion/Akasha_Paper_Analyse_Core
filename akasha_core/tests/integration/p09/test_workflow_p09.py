"""P09 workflow integration: LINKED + SEARCH_INDEXED stages."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from paperintel.database.models import EntityRow, RelationRow, SearchDocumentRow, TaskRow
from paperintel.schemas.enums import PipelineStage, ResourceTier
from paperintel.search.query import SearchFilters, document_counts, search
from paperintel.workflow import engine
from paperintel.workflow.celery_app import run_task_once
from paperintel.workflow.handlers import TASK_INDEX_SEARCH, TASK_LINK_ENTITIES


@pytest.fixture()
def stage_env(session, imported, providers_env):
    """An imported version with category-mapped claims.

    The entity map is driven by claim CATEGORIES (doc 01 §9 ↔ §16), so the
    fixture writes claims whose categories map to real entity types.
    """
    from tests.fixtures.canary import seed_canary_evidence
    from tests.integration.p08.fixtures import make_claim

    from paperintel.database.models import EvidenceRow
    from paperintel.schemas.enums import ClaimType, EvidenceRole

    evidence_id = seed_canary_evidence(session, imported.paper_version_id)
    evidence = session.get(EvidenceRow, evidence_id)
    assert evidence is not None

    claims = [
        make_claim(
            session,
            imported.paper_version_id,
            statement="Method A reaches 82.5% accuracy on the benchmark.",
            evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
            category="method.component",
            claim_type=ClaimType.FACT,
        ),
        make_claim(
            session,
            imported.paper_version_id,
            statement="The dataset contains 1,000 samples.",
            evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
            category="experiment.dataset",
            claim_type=ClaimType.FACT,
        ),
    ]
    session.flush()
    return {"evidence_id": evidence_id, "claims": claims}


def _run_stage(session, imported, task_type: str) -> dict:
    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.SYNTHESIZED,
    )
    task, _ = engine.enqueue_task(
        session,
        job,
        task_type=task_type,
        module_id=task_type,
        input_manifest={"paper_version_id": imported.paper_version_id},
        idempotency_key=f"{task_type}-test-{job.job_id}",
    )
    session.commit()
    outcome = run_task_once(session, task.task_id)
    session.commit()
    return outcome


@pytest.mark.needs_db
def test_linked_stage_builds_graph_and_tags(session, imported, stage_env) -> None:
    outcome = _run_stage(session, imported, TASK_LINK_ENTITIES)
    assert outcome["outcome"] == "succeeded", outcome

    row = session.get(TaskRow, outcome["task_id"])
    manifest = row.output_manifest
    assert manifest["relations_created"] >= 1
    assert manifest["claims_linked"] >= 1

    relations = session.scalars(
        select(RelationRow).where(RelationRow.paper_id == imported.paper_id)
    ).all()
    assert relations
    assert all(relation.evidence_ids for relation in relations)
    entities = session.scalars(select(EntityRow)).all()
    assert any(entity.entity_type.value == "PAPER" for entity in entities)


@pytest.mark.needs_db
def test_search_indexed_stage_builds_projection(session, imported, stage_env) -> None:
    outcome = _run_stage(session, imported, TASK_INDEX_SEARCH)
    assert outcome["outcome"] == "succeeded", outcome

    row = session.get(TaskRow, outcome["task_id"])
    manifest = row.output_manifest
    assert manifest["documents_written"] >= 2  # evidence + claim + paper
    assert manifest["embeddings_written"] >= 1  # mock embedder configured

    counts = document_counts(session)
    assert counts.get("EVIDENCE", 0) >= 1
    assert counts.get("CLAIM", 0) >= 1
    assert counts.get("PAPER", 0) >= 1

    # The projection is searchable immediately (FTS + semantic).
    response = search(
        session,
        query="dataset samples accuracy",
        filters=SearchFilters(paper_ids={imported.paper_id}),
        limit=5,
    )
    assert response.hits
    assert all(hit.paper_id == imported.paper_id for hit in response.hits)


@pytest.mark.needs_db
def test_reindexing_is_idempotent(session, imported, stage_env) -> None:
    """Re-running the search stage reuses unchanged documents (the
    projection is a cache, not a log)."""
    first = _run_stage(session, imported, TASK_INDEX_SEARCH)
    second = _run_stage(session, imported, TASK_INDEX_SEARCH)
    assert second["outcome"] == "succeeded"
    row = session.get(TaskRow, second["task_id"])
    assert row.output_manifest["documents_written"] == 0
    assert row.output_manifest["documents_reused"] >= 2
    assert first["outcome"] == "succeeded"


@pytest.mark.needs_db
def test_linked_and_search_stages_satisfied_after_runs(session, imported, stage_env) -> None:
    """plan_job reflects real progress: after the LINKED and SEARCH_INDEXED
    tasks ran, re-planning reports both satisfied."""
    _run_stage(session, imported, TASK_LINK_ENTITIES)
    _run_stage(session, imported, TASK_INDEX_SEARCH)

    job = engine.create_job(
        session,
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        current_stage=PipelineStage.SYNTHESIZED,
    )
    plan = engine.plan_job(
        session,
        job,
        data_dir=str(Path(imported.report_path).parents[2]),
        content_sha256=Path(imported.report_path).stem,
        report_path=imported.report_path,
    )
    assert "LINKED" in plan["satisfied"]
    assert "SEARCH_INDEXED" in plan["satisfied"]
    assert plan["skipped"] == ["CORPUS_READY"]


@pytest.mark.needs_db
def test_stage_constants_are_registered() -> None:
    from paperintel.workflow import engine as engine_module
    from paperintel.workflow.handlers import HANDLERS

    assert engine_module.IMPLEMENTED_STAGES[PipelineStage.LINKED] == TASK_LINK_ENTITIES
    assert engine_module.IMPLEMENTED_STAGES[PipelineStage.SEARCH_INDEXED] == TASK_INDEX_SEARCH
    assert TASK_LINK_ENTITIES in HANDLERS and TASK_INDEX_SEARCH in HANDLERS
    assert ResourceTier.T3_DEEP.value == "T3_DEEP"
    assert SearchDocumentRow.__tablename__ == "search_documents"

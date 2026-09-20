"""P09 retrieval benchmark: known queries with expected relevant items,
exact filters, and scope safety (doc 05 P09 gate).

The benchmark fixture is a small corpus of papers whose evidence contains
controlled vocabulary and numbers, so expected relevance is decidable.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from tests.fixtures.generators import build_f01_native, build_f04_rich

from paperintel.database.models import ClaimRow, EvidenceRow, SectionRow
from paperintel.ids import IdPrefix, new_claim_id, new_evidence_id, new_id
from paperintel.ingest.service import import_pdf
from paperintel.schemas.enums import (
    ClaimType,
    DataQualityState,
    EvidenceRole,
    EvidenceType,
    SectionClass,
    SourceMethod,
    SupportState,
)
from paperintel.search.index import index_version
from paperintel.search.query import SearchFilters, search
from paperintel.storage.object_store import LocalObjectStore


@pytest.fixture()
def store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(tmp_path / "objects")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def corpus(session, store, data_dir, tmp_path):
    """Two papers with controlled evidence vocabularies + claims."""
    papers = {}
    for name, builder in (("f01.pdf", build_f01_native), ("f04.pdf", build_f04_rich)):
        path = tmp_path / name
        path.write_bytes(builder())
        result = asyncio.run(import_pdf(path, session=session, store=store, data_dir=data_dir))
        papers[name] = result

    alpha = papers["f01.pdf"]
    beta = papers["f04.pdf"]

    # Controlled evidence for paper A (transformer/vision vocabulary).
    alpha_rows = [
        _evidence(
            session,
            alpha.paper_version_id,
            "Vision transformers reach 84.2% top-1 accuracy on ImageNet with 8 GPUs.",
            SectionClass.RESULT,
        ),
        _evidence(
            session,
            alpha.paper_version_id,
            "The dataset contains 1.3 million images collected from web sources.",
            SectionClass.EXPERIMENT,
        ),
    ]
    # Controlled evidence for paper B (graph/chemistry vocabulary).
    beta_rows = [
        _evidence(
            session,
            beta.paper_version_id,
            "Molecular graph networks predict solubility with 0.91 AUC on Tox21.",
            SectionClass.RESULT,
        ),
        _evidence(
            session,
            beta.paper_version_id,
            "Training used 4 GPUs and a batch size of 128 molecules.",
            SectionClass.EXPERIMENT,
        ),
    ]
    _claim(
        session,
        alpha,
        "Vision transformers reach 84.2% top-1 accuracy on ImageNet with 8 GPUs.",
        "result.main",
        [alpha_rows[0]],
        SupportState.SUPPORTED,
    )
    _claim(
        session,
        beta,
        "Molecular graph networks predict solubility with 0.91 AUC on Tox21.",
        "result.main",
        [beta_rows[0]],
        SupportState.PARTIALLY_SUPPORTED,
    )

    embedder = _mock_embedder()
    for result in papers.values():
        index_version(session, result.paper_version_id, embedder=embedder)
    session.commit()
    return {"papers": papers, "alpha": alpha, "beta": beta, "embedder": embedder}


def _mock_embedder():
    from paperintel.providers.mocks.embedding import MockEmbeddingProvider

    return MockEmbeddingProvider()


def _evidence(
    session: Session,
    version_id: str,
    text_value: str,
    section_class: SectionClass,
) -> EvidenceRow:
    from tests.integration.p08.fixtures import ensure_run

    run_id = ensure_run(session, version_id)
    section = SectionRow(
        section_id=new_id(IdPrefix.EVIDENCE).replace("ev_", "sec_"),
        paper_version_id=version_id,
        original_heading=f"{section_class.value} section",
        normalized_class=section_class,
        page_start=1,
        page_end=1,
        ordinal=0,
    )
    session.add(section)
    row = EvidenceRow(
        evidence_id=new_evidence_id(),
        paper_version_id=version_id,
        evidence_type=EvidenceType.PARAGRAPH,
        page_start=1,
        page_end=1,
        section_id=section.section_id,
        text=text_value,
        source_method=SourceMethod.PDF_NATIVE,
        quality_state=DataQualityState.GOOD,
        content_sha256=hashlib.sha256(text_value.encode()).hexdigest(),
        extraction_run_id=run_id,
    )
    session.add(row)
    session.flush()
    return row


def _claim(
    session: Session,
    imported,
    statement: str,
    category: str,
    evidence_rows: list[EvidenceRow],
    support_state: SupportState,
) -> ClaimRow:
    from tests.integration.p08.fixtures import ensure_run

    from paperintel.database.models import ClaimEvidenceRow

    claim = ClaimRow(
        claim_id=new_claim_id(),
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        claim_type=ClaimType.FACT,
        category=category,
        statement=statement,
        support_state=support_state,
        created_by_run_id=ensure_run(session, imported.paper_version_id),
        pipeline_version="1.0.0",
    )
    session.add(claim)
    for row in evidence_rows:
        session.add(
            ClaimEvidenceRow(
                claim_id=claim.claim_id, evidence_id=row.evidence_id, role=EvidenceRole.SUPPORT
            )
        )
    session.flush()
    return claim


# ---------------------------------------------------------------------------
# benchmark: known queries → expected relevant items
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_known_query_returns_expected_relevant_documents(session, corpus) -> None:
    response = search(session, query="vision transformers ImageNet accuracy", limit=5)
    assert response.hits, "expected hits for the benchmark query"
    top = response.hits[0]
    assert top.paper_id == corpus["alpha"].paper_id
    assert "84.2" in top.content_excerpt
    assert "fts" in top.channels


@pytest.mark.needs_db
def test_second_query_prefers_the_other_paper(session, corpus) -> None:
    response = search(session, query="molecular graph solubility Tox21", limit=5)
    assert response.hits
    assert response.hits[0].paper_id == corpus["beta"].paper_id


@pytest.mark.needs_db
def test_semantic_channel_finds_paraphrased_query(session, corpus) -> None:
    """Semantic retrieval matches vocabulary overlap even without exact
    phrase matches (FTS-only would miss some)."""
    response = search(
        session,
        query="image classification accuracy with GPUs",
        limit=5,
        embedder=corpus["embedder"],
    )
    channels = {channel for hit in response.hits for channel in hit.channels}
    assert "semantic" in channels
    assert any(hit.semantic_similarity is not None for hit in response.hits)


# ---------------------------------------------------------------------------
# filters must be exact
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_paper_filter_is_exact(session, corpus) -> None:
    response = search(
        session,
        query="accuracy",
        filters=SearchFilters(paper_ids={corpus["beta"].paper_id}),
        limit=10,
    )
    assert response.hits
    assert {hit.paper_id for hit in response.hits} == {corpus["beta"].paper_id}


@pytest.mark.needs_db
def test_section_class_filter_is_exact(session, corpus) -> None:
    response = search(
        session,
        query="GPUs",
        filters=SearchFilters(section_classes={SectionClass.EXPERIMENT.value}),
        limit=10,
    )
    assert response.hits
    for hit in response.hits:
        document = session.get(
            __import__(
                "paperintel.database.models", fromlist=["SearchDocumentRow"]
            ).SearchDocumentRow,
            hit.document_id,
        )
        section = session.get(SectionRow, document.section_id)
        assert section.normalized_class is SectionClass.EXPERIMENT


@pytest.mark.needs_db
def test_claim_type_and_verification_state_filters(session, corpus) -> None:
    supported = search(
        session,
        query="accuracy",
        filters=SearchFilters(
            document_types={"CLAIM"}, support_states={SupportState.SUPPORTED.value}
        ),
        limit=10,
    )
    assert supported.hits
    assert all(hit.document_type == "CLAIM" for hit in supported.hits)

    partial = search(
        session,
        query="solubility",
        filters=SearchFilters(
            document_types={"CLAIM"},
            support_states={SupportState.PARTIALLY_SUPPORTED.value},
        ),
        limit=10,
    )
    assert partial.hits
    assert {hit.paper_id for hit in partial.hits} == {corpus["beta"].paper_id}


@pytest.mark.needs_db
def test_collection_filter_scopes_results(session, corpus) -> None:
    from paperintel.knowledge.collections import add_paper, create_collection

    collection = create_collection(session, name="vision project", purpose="testing")
    add_paper(
        session,
        collection_id=collection.collection_id,
        paper_id=corpus["alpha"].paper_id,
        pinned=True,
    )
    session.flush()

    response = search(
        session,
        query="accuracy",
        filters=SearchFilters(collection_ids={collection.collection_id}),
        limit=10,
    )
    assert response.hits
    assert {hit.paper_id for hit in response.hits} == {corpus["alpha"].paper_id}


@pytest.mark.needs_db
def test_empty_entity_filter_matches_nothing_not_everything(session, corpus) -> None:
    """A filter that matches no entity must return NOTHING — falling back
    to "no filter" would leak out-of-scope documents."""
    response = search(
        session,
        query="accuracy",
        filters=SearchFilters(entity_ids={"ent_01UNKNOWN000000000000000000"}),
        limit=10,
    )
    assert response.scope_size == 0
    assert response.hits == []


# ---------------------------------------------------------------------------
# semantic channel must not bypass scope
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_semantic_channel_cannot_bypass_scope_filters(session, corpus) -> None:
    """The gate requirement: semantic search must respect scope. Queries
    whose terms belong to the OTHER paper still return only in-scope
    documents, and the scope size proves the restriction."""
    response = search(
        session,
        query="molecular graph solubility Tox21 AUC",
        filters=SearchFilters(paper_ids={corpus["alpha"].paper_id}),
        embedder=corpus["embedder"],
        limit=10,
    )
    assert response.hits, "scope must still return its own documents"
    assert {hit.paper_id for hit in response.hits} == {corpus["alpha"].paper_id}
    # Every hit is inside the scope; no beta document leaked in.
    beta_docs = {
        hit.document_id
        for hit in search(
            session,
            query="molecular graph solubility",
            filters=SearchFilters(paper_ids={corpus["beta"].paper_id}),
            embedder=corpus["embedder"],
            limit=50,
        ).hits
    }
    assert not (beta_docs & {hit.document_id for hit in response.hits})


@pytest.mark.needs_db
def test_semantic_only_search_respects_version_scope(session, corpus) -> None:
    response = search(
        session,
        query="transformers images",
        filters=SearchFilters(paper_version_ids={corpus["alpha"].paper_version_id}),
        embedder=corpus["embedder"],
        limit=20,
    )
    assert all(hit.paper_version_id == corpus["alpha"].paper_version_id for hit in response.hits)


@pytest.mark.needs_db
def test_ranking_is_deterministic(session, corpus) -> None:
    first = search(session, query="accuracy GPUs", limit=5, embedder=corpus["embedder"])
    second = search(session, query="accuracy GPUs", limit=5, embedder=corpus["embedder"])
    assert [hit.document_id for hit in first.hits] == [hit.document_id for hit in second.hits]
    assert [hit.score for hit in first.hits] == [hit.score for hit in second.hits]

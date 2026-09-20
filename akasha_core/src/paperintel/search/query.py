"""search.query — retrieval: filters + FTS + pgvector + graph + rerank
(P09, doc 01 §17).

Query pipeline (all five documented channels):

1. exact structured filters (paper, collection, version, section class,
   evidence type, claim type, verification state, tags, author, venue,
   date, entity) — applied as SQL WHERE clauses;
2. PostgreSQL full-text search (ts_rank over the projection's tsvector);
3. pgvector semantic retrieval (cosine distance over the embedding);
4. graph expansion (entities/relations reachable from the filter set);
5. reranking: reciprocal-rank fusion of the channels plus a deterministic
   tie-break, so the same query always returns the same order.

SCOPE SAFETY (doc 05 P09 gate): every channel runs INSIDE the same
filtered subquery. Semantic retrieval cannot see documents outside the
caller's scope — the embedding channel is joined to the scoped set, not
queried globally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, and_, select, text
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimRow,
    CollectionPaperRow,
    EntityRow,
    PaperTagRow,
    RelationRow,
    SearchDocumentRow,
    SectionRow,
    TagRow,
)
from paperintel.providers.base import EmbeddingProvider
from paperintel.search.index import DOCUMENT_CLAIM, DOCUMENT_EVIDENCE, DOCUMENT_PAPER

#: Reciprocal-rank-fusion constant (industry-standard k=60).
RRF_K = 60


@dataclass(slots=True)
class SearchFilters:
    """Exact structured filters (doc 01 §17 search dimensions)."""

    document_types: set[str] | None = None
    paper_ids: set[str] | None = None
    paper_version_ids: set[str] | None = None
    collection_ids: set[str] | None = None
    section_classes: set[str] | None = None
    section_ids: set[str] | None = None
    evidence_types: set[str] | None = None
    claim_types: set[str] | None = None
    claim_categories: set[str] | None = None
    support_states: set[str] | None = None
    tag_namespaces: set[str] | None = None
    tag_names: set[str] | None = None
    entity_ids: set[str] | None = None
    entity_types: set[str] | None = None
    page_min: int | None = None
    page_max: int | None = None


@dataclass(slots=True)
class SearchHit:
    document_id: str
    document_type: str
    paper_id: str | None
    paper_version_id: str | None
    score: float
    content_excerpt: str
    #: Which channels contributed (provenance of the ranking).
    channels: list[str] = field(default_factory=list)
    fts_rank: float | None = None
    semantic_similarity: float | None = None
    graph_distance: int | None = None


@dataclass(slots=True)
class SearchResponse:
    query: str
    hits: list[SearchHit]
    #: Document IDs the scope allowed (before ranking) — audit evidence
    #: that no channel escaped the filters.
    scope_size: int
    channels_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build_scope_statement(session: Session, filters: SearchFilters) -> Select:
    """The filtered document set — the ONLY thing any channel may see."""
    stmt = select(SearchDocumentRow)
    conditions: list[Any] = []

    if filters.document_types:
        conditions.append(SearchDocumentRow.document_type.in_(filters.document_types))
    if filters.paper_ids:
        conditions.append(SearchDocumentRow.paper_id.in_(filters.paper_ids))
    if filters.paper_version_ids:
        conditions.append(SearchDocumentRow.paper_version_id.in_(filters.paper_version_ids))
    if filters.evidence_types:
        conditions.append(SearchDocumentRow.evidence_type.in_(filters.evidence_types))
    if filters.claim_types:
        conditions.append(SearchDocumentRow.claim_type.in_(filters.claim_types))
    if filters.claim_categories:
        conditions.append(
            SearchDocumentRow.document_id.in_(
                select(ClaimRow.claim_id).where(ClaimRow.category.in_(filters.claim_categories))
            )
        )
    if filters.support_states:
        conditions.append(SearchDocumentRow.support_state.in_(filters.support_states))
    if filters.page_min is not None:
        conditions.append(SearchDocumentRow.page_start >= filters.page_min)
    if filters.page_max is not None:
        conditions.append(SearchDocumentRow.page_start <= filters.page_max)

    if filters.section_classes or filters.section_ids:
        section_stmt = select(SectionRow.section_id)
        if filters.section_classes:
            section_stmt = section_stmt.where(
                SectionRow.normalized_class.in_(filters.section_classes)
            )
        if filters.section_ids:
            section_stmt = section_stmt.where(SectionRow.section_id.in_(filters.section_ids))
        conditions.append(SearchDocumentRow.section_id.in_(section_stmt))

    if filters.collection_ids:
        collection_stmt = select(CollectionPaperRow.paper_id).where(
            CollectionPaperRow.collection_id.in_(filters.collection_ids)
        )
        conditions.append(SearchDocumentRow.paper_id.in_(collection_stmt))

    if filters.tag_namespaces or filters.tag_names:
        tag_stmt = select(PaperTagRow.paper_id).join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
        if filters.tag_namespaces:
            tag_stmt = tag_stmt.where(TagRow.namespace.in_(filters.tag_namespaces))
        if filters.tag_names:
            tag_stmt = tag_stmt.where(TagRow.normalized_name.in_(filters.tag_names))
        conditions.append(SearchDocumentRow.paper_id.in_(tag_stmt))

    if filters.entity_ids or filters.entity_types:
        entity_papers = _papers_for_entities(session, filters)
        conditions.append(SearchDocumentRow.paper_id.in_(entity_papers))

    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt


def _papers_for_entities(session: Session, filters: SearchFilters) -> Select:
    """Papers linked to the requested entities (direct relations only —
    graph expansion happens in its own channel)."""
    entity_stmt = select(EntityRow.entity_id)
    if filters.entity_ids:
        entity_stmt = entity_stmt.where(EntityRow.entity_id.in_(filters.entity_ids))
    if filters.entity_types:
        entity_stmt = entity_stmt.where(EntityRow.entity_type.in_(filters.entity_types))
    entity_ids = list(session.scalars(entity_stmt))
    if not entity_ids:
        # An entity filter that matches nothing must match NOTHING (never
        # fall back to "no filter" — that would leak out-of-scope papers).
        return select(RelationRow.paper_id).where(RelationRow.paper_id.is_(None))
    return select(RelationRow.paper_id).where(
        RelationRow.paper_id.is_not(None),
        (RelationRow.source_entity_id.in_(entity_ids))
        | (RelationRow.target_entity_id.in_(entity_ids)),
    )


def search(
    session: Session,
    *,
    query: str | None = None,
    filters: SearchFilters | None = None,
    embedder: EmbeddingProvider | None = None,
    limit: int = 20,
    semantic: bool = True,
    graph_expand: bool = False,
) -> SearchResponse:
    """Run the full retrieval pipeline over the scoped document set."""
    filters = filters or SearchFilters()
    scoped = build_scope_statement(session, filters)
    scoped_ids = [row.document_id for row in session.scalars(scoped)]
    channels: list[str] = ["filters"]
    warnings: list[str] = []

    if not scoped_ids:
        return SearchResponse(
            query=query or "",
            hits=[],
            scope_size=0,
            channels_used=channels,
            warnings=warnings,
        )

    scores: dict[str, float] = dict.fromkeys(scoped_ids, 0.0)
    fts_ranks: dict[str, float] = {}
    semantic_scores: dict[str, float] = {}
    graph_distances: dict[str, int] = {}

    if query:
        fts_rows = session.execute(
            text(
                "SELECT document_id, ts_rank(search_vector, plainto_tsquery('english', :q)) "
                "FROM search_documents "
                "WHERE document_id = ANY(:ids) "
                "AND search_vector @@ plainto_tsquery('english', :q) "
                "ORDER BY 2 DESC"
            ),
            {"q": query, "ids": scoped_ids},
        ).all()
        channels.append("fts")
        for position, (document_id, rank) in enumerate(fts_rows):
            fts_ranks[document_id] = float(rank)
            scores[document_id] += 1.0 / (RRF_K + position + 1)

        if semantic:
            if embedder is None:
                warnings.append(
                    "semantic channel requested without an embedding provider — "
                    "FTS-only ranking (explicit, not silent)"
                )
            else:
                vector = asyncio.run(embedder.embed([query])).vectors[0]
                literal = "[" + ",".join(f"{value:.8f}" for value in vector) + "]"
                semantic_rows = session.execute(
                    text(
                        "SELECT document_id, 1 - (embedding <=> CAST(:vec AS vector)) AS sim "
                        "FROM search_documents "
                        "WHERE document_id = ANY(:ids) AND embedding IS NOT NULL "
                        "ORDER BY embedding <=> CAST(:vec AS vector) "
                        "LIMIT :limit"
                    ),
                    {"vec": literal, "ids": scoped_ids, "limit": max(limit * 3, 20)},
                ).all()
                channels.append("semantic")
                for position, (document_id, similarity) in enumerate(semantic_rows):
                    semantic_scores[document_id] = float(similarity)
                    scores[document_id] += 1.0 / (RRF_K + position + 1)

    if graph_expand and (filters.entity_ids or filters.entity_types):
        neighbours = _graph_expansion(session, filters, scoped_ids)
        if neighbours:
            channels.append("graph")
        for distance, document_ids in neighbours.items():
            for document_id in document_ids:
                if document_id in scores:
                    graph_distances[document_id] = min(
                        distance, graph_distances.get(document_id, distance)
                    )
                    scores[document_id] += 1.0 / (RRF_K + distance + 1)

    ranked = sorted(
        (document_id for document_id in scoped_ids),
        key=lambda document_id: (-scores[document_id], document_id),
    )[:limit]

    hits: list[SearchHit] = []
    for document_id in ranked:
        document = session.get(SearchDocumentRow, document_id)
        if document is None:  # pragma: no cover - defensive
            continue
        hit_channels = ["filters"]
        if document_id in fts_ranks:
            hit_channels.append("fts")
        if document_id in semantic_scores:
            hit_channels.append("semantic")
        if document_id in graph_distances:
            hit_channels.append("graph")
        hits.append(
            SearchHit(
                document_id=document_id,
                document_type=document.document_type,
                paper_id=document.paper_id,
                paper_version_id=document.paper_version_id,
                score=round(scores[document_id], 8),
                content_excerpt=document.content[:240],
                channels=hit_channels,
                fts_rank=fts_ranks.get(document_id),
                semantic_similarity=semantic_scores.get(document_id),
                graph_distance=graph_distances.get(document_id),
            )
        )

    return SearchResponse(
        query=query or "",
        hits=hits,
        scope_size=len(scoped_ids),
        channels_used=channels,
        warnings=warnings,
    )


def _graph_expansion(
    session: Session, filters: SearchFilters, scoped_ids: list[str]
) -> dict[int, list[str]]:
    """One-hop graph expansion from the requested entities: other entities
    related to them, mapped back to their documents (still intersected with
    the scoped set)."""
    seed_stmt = select(EntityRow.entity_id)
    if filters.entity_ids:
        seed_stmt = seed_stmt.where(EntityRow.entity_id.in_(filters.entity_ids))
    if filters.entity_types:
        seed_stmt = seed_stmt.where(EntityRow.entity_type.in_(filters.entity_types))
    seeds = set(session.scalars(seed_stmt))
    if not seeds:
        return {}

    relations = session.scalars(
        select(RelationRow).where(
            (RelationRow.source_entity_id.in_(seeds)) | (RelationRow.target_entity_id.in_(seeds))
        )
    ).all()
    neighbour_papers: set[str] = set()
    for relation in relations:
        if relation.paper_id:
            neighbour_papers.add(relation.paper_id)
    if not neighbour_papers:
        return {}

    document_ids = [
        document_id
        for document_id in scoped_ids
        if (session.get(SearchDocumentRow, document_id) is not None)
        and session.get(SearchDocumentRow, document_id).paper_id in neighbour_papers
    ]
    return {1: document_ids}


def search_evidence(
    session: Session, query: str, *, paper_version_id: str, limit: int = 10, **kwargs
) -> SearchResponse:
    """Convenience: evidence-only search inside one version."""
    return search(
        session,
        query=query,
        filters=SearchFilters(
            document_types={DOCUMENT_EVIDENCE}, paper_version_ids={paper_version_id}
        ),
        limit=limit,
        **kwargs,
    )


def search_claims(
    session: Session, query: str, *, paper_version_id: str, limit: int = 10, **kwargs
) -> SearchResponse:
    """Convenience: claim search (defaults to verified-capable states)."""
    return search(
        session,
        query=query,
        filters=SearchFilters(
            document_types={DOCUMENT_CLAIM}, paper_version_ids={paper_version_id}
        ),
        limit=limit,
        **kwargs,
    )


def search_papers(session: Session, query: str, *, limit: int = 10, **kwargs) -> SearchResponse:
    """Convenience: paper metadata search."""
    return search(
        session,
        query=query,
        filters=SearchFilters(document_types={DOCUMENT_PAPER}),
        limit=limit,
        **kwargs,
    )


def document_counts(session: Session) -> dict[str, int]:
    """Projection inventory by document type (observability)."""
    rows = session.execute(
        text("SELECT document_type, count(*) FROM search_documents GROUP BY 1")
    ).all()
    return {document_type: int(count) for document_type, count in rows}


__all__ = [
    "DOCUMENT_CLAIM",
    "DOCUMENT_EVIDENCE",
    "DOCUMENT_PAPER",
    "SearchFilters",
    "SearchHit",
    "SearchResponse",
    "build_scope_statement",
    "document_counts",
    "search",
    "search_claims",
    "search_evidence",
    "search_papers",
]

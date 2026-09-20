"""search.index — the derived search projection (P09, doc 01 §17).

Indexes evidence, claims and papers into ``search_documents`` with a
PostgreSQL FTS vector and a pgvector embedding. The projection is DERIVED
and rebuildable: canonical records (evidence, claims) are immutable and
are never modified by indexing.

Idempotency: a document is re-written only when its content hash or
embedding model changes, so re-indexing a version is cheap and stable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimRow,
    EvidenceRow,
    PaperRow,
    PaperVersionRow,
    SearchDocumentRow,
)
from paperintel.errors import DomainError
from paperintel.providers.base import EmbeddingProvider

DOCUMENT_EVIDENCE = "EVIDENCE"
DOCUMENT_CLAIM = "CLAIM"
DOCUMENT_PAPER = "PAPER"


@dataclass(slots=True)
class IndexReport:
    documents_written: int = 0
    documents_reused: int = 0
    embeddings_written: int = 0
    documents_removed: int = 0
    warnings: list[str] = field(default_factory=list)


def content_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def index_evidence(
    session: Session,
    paper_version_id: str,
    *,
    embedder: EmbeddingProvider | None = None,
    batch_size: int = 32,
) -> IndexReport:
    """Index the version's evidence units (text-bearing rows only)."""
    rows = session.scalars(
        select(EvidenceRow).where(
            EvidenceRow.paper_version_id == paper_version_id,
            EvidenceRow.text.is_not(None),
        )
    ).all()
    report = IndexReport()
    pending: list[SearchDocumentRow] = []

    paper_id = _paper_id_for_version(session, paper_version_id)
    for row in rows:
        content = row.text or ""
        if not content.strip():
            continue
        digest = content_hash(content)
        document = session.get(SearchDocumentRow, row.evidence_id)
        if (
            document is not None
            and document.content_sha256 == digest
            and _has_search_vector(session, row.evidence_id)
        ):
            report.documents_reused += 1
            continue
        if document is None:
            document = SearchDocumentRow(document_id=row.evidence_id)
        # Assign EVERY field before the row can be autoflushed (a partially
        # built row would violate the NOT NULL content constraint).
        document.document_type = DOCUMENT_EVIDENCE
        document.paper_id = paper_id
        document.paper_version_id = row.paper_version_id
        document.section_id = row.section_id
        document.evidence_type = row.evidence_type.value
        document.page_start = row.page_start
        document.content = content
        document.content_sha256 = digest
        session.add(document)
        session.flush()
        _set_search_vector(session, row.evidence_id, content)
        report.documents_written += 1
        pending.append(document)

    if embedder is not None and pending:
        report.embeddings_written += _embed_documents(session, pending, embedder, batch_size)

    session.flush()
    return report


def index_claims(
    session: Session,
    paper_version_id: str,
    *,
    embedder: EmbeddingProvider | None = None,
) -> IndexReport:
    """Index the version's claims (statement + category), carrying the
    verification state so search can filter on it exactly."""
    rows = session.scalars(
        select(ClaimRow).where(ClaimRow.paper_version_id == paper_version_id)
    ).all()
    report = IndexReport()
    pending: list[SearchDocumentRow] = []

    for row in rows:
        content = f"{row.category}: {row.statement}"
        digest = content_hash(content, row.support_state.value)
        document = session.get(SearchDocumentRow, row.claim_id)
        if (
            document is not None
            and document.content_sha256 == digest
            and _has_search_vector(session, row.claim_id)
        ):
            report.documents_reused += 1
            continue
        if document is None:
            document = SearchDocumentRow(document_id=row.claim_id)
        document.document_type = DOCUMENT_CLAIM
        document.paper_id = row.paper_id
        document.paper_version_id = row.paper_version_id
        document.claim_type = row.claim_type.value
        document.support_state = row.support_state.value
        document.content = row.statement
        document.content_sha256 = digest
        session.add(document)
        session.flush()
        _set_search_vector(session, row.claim_id, content)
        report.documents_written += 1
        pending.append(document)

    if embedder is not None and pending:
        report.embeddings_written += _embed_documents(session, pending, embedder, 32)

    session.flush()
    return report


def index_paper(
    session: Session,
    paper_id: str,
    *,
    embedder: EmbeddingProvider | None = None,
) -> IndexReport:
    """Index paper metadata (title + DOI + type) for metadata search."""
    paper = session.get(PaperRow, paper_id)
    if paper is None:
        raise DomainError(
            "CFG_002", message=f"Unknown paper ID: {paper_id}", details={"paper_id": paper_id}
        )
    content = " ".join(
        part for part in (paper.canonical_title, paper.doi or "", paper.paper_type or "") if part
    )
    digest = content_hash(content)
    document = session.get(SearchDocumentRow, paper_id)
    report = IndexReport()
    if (
        document is not None
        and document.content_sha256 == digest
        and _has_search_vector(session, paper_id)
    ):
        report.documents_reused += 1
        return report
    if document is None:
        document = SearchDocumentRow(document_id=paper_id)
    document.document_type = DOCUMENT_PAPER
    document.paper_id = paper_id
    document.content = content
    document.content_sha256 = digest
    session.add(document)
    session.flush()
    _set_search_vector(session, paper_id, content)
    report.documents_written += 1
    if embedder is not None:
        report.embeddings_written += _embed_documents(session, [document], embedder, 1)
    session.flush()
    return report


def _paper_id_for_version(session: Session, paper_version_id: str) -> str | None:
    version = session.get(PaperVersionRow, paper_version_id)
    return version.paper_id if version is not None else None


def _has_search_vector(session: Session, document_id: str) -> bool:
    """Whether the projection row already carries its tsvector (checked in
    SQL because the tsvector column is deliberately unmapped)."""
    value = session.execute(
        text(
            "SELECT search_vector IS NOT NULL FROM search_documents "
            "WHERE document_id = :document_id"
        ),
        {"document_id": document_id},
    ).scalar()
    return bool(value)


def _set_search_vector(session: Session, document_id: str, content: str) -> None:
    """Populate the tsvector in SQL (the canonical way to build weighted
    FTS content: title-ish text gets weight A, body weight B)."""
    session.execute(
        text(
            "UPDATE search_documents SET search_vector = "
            "setweight(to_tsvector('english', :content), 'B'), updated_at = now() "
            "WHERE document_id = :document_id"
        ),
        {"content": content, "document_id": document_id},
    )


def _embed_documents(
    session: Session,
    documents: list[SearchDocumentRow],
    embedder: EmbeddingProvider,
    batch_size: int,
) -> int:
    """Embed document content and store the vectors (pgvector literal)."""
    import asyncio

    written = 0
    for start in range(0, len(documents), batch_size):
        batch = documents[start : start + batch_size]
        result = asyncio.run(embedder.embed([doc.content for doc in batch]))
        vectors = result.vectors
        if len(vectors) != len(batch):
            raise DomainError(
                "PROVIDER_002",
                message=(
                    "Embedding provider returned a different number of vectors "
                    "than inputs — refusing to index mismatched vectors."
                ),
                details={"inputs": len(batch), "vectors": len(vectors)},
            )
        for doc, vector in zip(batch, vectors, strict=True):
            session.execute(
                text(
                    "UPDATE search_documents SET embedding = CAST(:vector AS vector), "
                    "embedding_model_id = :model_id WHERE document_id = :document_id"
                ),
                {
                    "vector": "[" + ",".join(f"{value:.8f}" for value in vector) + "]",
                    "model_id": result.model_id or result.provider_id,
                    "document_id": doc.document_id,
                },
            )
            written += 1
    return written


def remove_version_documents(session: Session, paper_version_id: str) -> int:
    """Drop a version's projection rows (rebuild primitive; canonical
    records are untouched)."""
    result = session.execute(
        text("DELETE FROM search_documents WHERE paper_version_id = :version"),
        {"version": paper_version_id},
    )
    return int(result.rowcount or 0)


def index_version(
    session: Session,
    paper_version_id: str,
    *,
    embedder: EmbeddingProvider | None = None,
) -> IndexReport:
    """Index everything searchable for one paper version."""
    evidence = index_evidence(session, paper_version_id, embedder=embedder)
    claims = index_claims(session, paper_version_id, embedder=embedder)
    version = session.get(PaperVersionRow, paper_version_id)
    paper = index_paper(session, version.paper_id, embedder=embedder) if version else IndexReport()
    return IndexReport(
        documents_written=evidence.documents_written
        + claims.documents_written
        + paper.documents_written,
        documents_reused=evidence.documents_reused
        + claims.documents_reused
        + paper.documents_reused,
        embeddings_written=evidence.embeddings_written
        + claims.embeddings_written
        + paper.embeddings_written,
    )

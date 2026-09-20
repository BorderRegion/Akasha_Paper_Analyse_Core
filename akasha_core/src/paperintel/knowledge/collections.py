"""knowledge.collections — research collections (P09, doc 01 §18).

Collections are the primary scope for long-term project memory: purpose,
research questions, preferred tags, relevance notes, pinned papers and
per-paper manual priority overrides. Scope resolution here is what search
and triage filter against.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import CollectionPaperRow, CollectionRow, PaperRow
from paperintel.errors import DomainError
from paperintel.ids import new_collection_id
from paperintel.knowledge.tags import validate_namespace
from paperintel.schemas.enums import ResourceTier


@dataclass(slots=True)
class CollectionStats:
    collection_id: str
    name: str
    paper_count: int
    pinned_count: int
    override_count: int


def create_collection(
    session: Session,
    *,
    name: str,
    purpose: str = "",
    research_questions: list[str] | None = None,
    preferred_tags: list[dict[str, str]] | None = None,
    relevance_notes: str = "",
) -> CollectionRow:
    """Create a collection. Preferred tags are validated against the frozen
    namespace families (a collection cannot declare an invented namespace)."""
    if not name.strip():
        raise DomainError("CFG_002", message="Collection name must be non-empty.", details={})
    for tag in preferred_tags or []:
        validate_namespace(tag.get("namespace", ""))

    row = CollectionRow(
        collection_id=new_collection_id(),
        name=name.strip(),
        purpose=purpose,
        research_questions=research_questions or None,
        preferred_tags=preferred_tags or None,
        relevance_notes=relevance_notes,
    )
    session.add(row)
    session.flush()
    return row


def get_collection(session: Session, collection_id: str) -> CollectionRow:
    row = session.get(CollectionRow, collection_id)
    if row is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown collection ID: {collection_id}",
            details={"collection_id": collection_id},
        )
    return row


def add_paper(
    session: Session,
    *,
    collection_id: str,
    paper_id: str,
    pinned: bool = False,
    priority_override_tier: ResourceTier | None = None,
    relevance_note: str | None = None,
) -> tuple[CollectionPaperRow, bool]:
    """Add a paper to a collection (idempotent). A manual priority override
    recorded here always wins over automatic triage (doc 02 §11)."""
    from paperintel.database.models import CollectionRow

    if session.scalar(select(CollectionRow).where(CollectionRow.collection_id == collection_id)
                      .with_for_update()) is None:
        get_collection(session, collection_id)
    if session.get(PaperRow, paper_id) is None:
        raise DomainError(
            "CFG_002", message=f"Unknown paper ID: {paper_id}", details={"paper_id": paper_id}
        )

    existing = session.scalar(
        select(CollectionPaperRow).where(
            CollectionPaperRow.collection_id == collection_id,
            CollectionPaperRow.paper_id == paper_id,
        )
    )
    if existing is not None:
        changed = False
        if pinned and not existing.pinned:
            existing.pinned = True
            changed = True
        if priority_override_tier is not None:
            existing.priority_override_tier = priority_override_tier
            changed = True
        if relevance_note is not None:
            existing.relevance_note = relevance_note
            changed = True
        session.flush()
        return existing, changed

    link = CollectionPaperRow(
        collection_id=collection_id,
        paper_id=paper_id,
        pinned=pinned,
        priority_override_tier=priority_override_tier,
        relevance_note=relevance_note,
    )
    session.add(link)
    session.flush()
    return link, True


def remove_paper(session: Session, *, collection_id: str, paper_id: str) -> bool:
    link = session.scalar(
        select(CollectionPaperRow).where(
            CollectionPaperRow.collection_id == collection_id,
            CollectionPaperRow.paper_id == paper_id,
        )
    )
    if link is None:
        return False
    session.delete(link)
    session.flush()
    return True


def collection_paper_ids(session: Session, collection_id: str) -> list[str]:
    """Scope resolution for search/triage filters."""
    get_collection(session, collection_id)
    return list(
        session.scalars(
            select(CollectionPaperRow.paper_id)
            .where(CollectionPaperRow.collection_id == collection_id)
            .order_by(CollectionPaperRow.added_at, CollectionPaperRow.paper_id)
        )
    )


def collection_stats(session: Session, collection_id: str) -> CollectionStats:
    rows = session.scalars(
        select(CollectionPaperRow).where(CollectionPaperRow.collection_id == collection_id)
    ).all()
    collection = get_collection(session, collection_id)
    return CollectionStats(
        collection_id=collection_id,
        name=collection.name,
        paper_count=len(rows),
        pinned_count=sum(1 for row in rows if row.pinned),
        override_count=sum(1 for row in rows if row.priority_override_tier is not None),
    )


def list_collections(session: Session) -> list[CollectionStats]:
    return [
        collection_stats(session, row.collection_id)
        for row in session.scalars(select(CollectionRow).order_by(CollectionRow.created_at))
    ]

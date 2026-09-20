"""services.ui.library — the workbench library query (docs/06「文献库与工作台」).

Two rules drive this module:

1. FILTER BEFORE PAGINATE. The search layer produces candidate ids; the typed
   filter/scope narrowing happens in SQL, then the page is cut. A page can
   therefore never be padded with rows that a client-side filter would drop.
2. The cursor is an opaque value bound to (filter_hash, scope_revision, sort
   key). A cursor that no longer matches returns 409 RESET_CURSOR so the client
   re-fetches from the first page instead of silently paging over a changed set.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimRow,
    CollectionPaperRow,
    EntityRow,
    PaperRow,
    PaperTagRow,
    PaperVersionRow,
    RelationRow,
    TagRow,
    TriageResultRow,
)
from paperintel.errors import DomainError
from paperintel.errors.ui_errors import UiError
from paperintel.schemas.enums import ResourceTier, SupportState
from paperintel.schemas.ui.models import LibraryQuery

MAX_PAGE = 100
DEFAULT_PAGE = 50


@dataclass(slots=True)
class LibraryPage:
    paper_ids: list[str]
    next_cursor: str | None
    has_more: bool
    total: int | None
    total_kind: str
    scope_revision: str
    filter_hash: str


def _filter_payload(query: LibraryQuery) -> dict[str, Any]:
    filters = query.filters.model_dump(exclude_none=True)
    return {"kind": query.kind, "filters": filters, "sort": query.sort, "q": query.query.strip()}


def filter_hash(query: LibraryQuery) -> str:
    payload = json.dumps(_filter_payload(query), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def scope_revision(session: Session) -> str:
    """A revision string that changes when the underlying corpus changes.

    Counts + max(created_at) over papers/versions/claims are enough to detect
    a shifted scope without a full scan of every row.
    """
    papers = session.scalar(select(func.count()).select_from(PaperRow)) or 0
    versions = session.scalar(select(func.count()).select_from(PaperVersionRow)) or 0
    claims = session.scalar(select(func.count()).select_from(ClaimRow)) or 0
    latest = session.scalar(select(func.max(PaperRow.created_at)))
    stamp = latest.isoformat() if latest is not None else "none"
    return hashlib.sha256(f"{papers}|{versions}|{claims}|{stamp}".encode()).hexdigest()[:16]


def encode_cursor(*, filter_hash_value: str, scope: str, sort_key: str, offset: int) -> str:
    raw = json.dumps(
        {"f": filter_hash_value, "s": scope, "k": sort_key, "o": offset}, separators=(",", ":")
    )
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    padding = "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(cursor + padding).decode()
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise DomainError(
            "CFG_002", message="Malformed cursor.", details={"cursor": cursor[:40]}
        ) from exc
    if not isinstance(payload, dict) or "o" not in payload:
        raise DomainError("CFG_002", message="Malformed cursor.", details={})
    return payload


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _title_match_condition(term: str):
    from sqlalchemy import or_

    return or_(
        PaperRow.normalized_title.like(f"%{_escape_like(term.lower())}%", escape="\\"),
        PaperRow.canonical_title.ilike(f"%{_escape_like(term)}%", escape="\\"),
    )


def scoped_paper_ids(session: Session, query: LibraryQuery) -> Select:
    """The SQL-narrowed id set — the ONLY thing pagination is allowed to cut."""
    filters = query.filters
    stmt = select(PaperRow.paper_id)

    if query.query.strip():
        # FTS over the projection (papers) when indexed; the title filter keeps
        # the query honest before the index exists for a paper.
        from paperintel.database.models import SearchDocumentRow

        term = query.query.strip()
        indexed = select(SearchDocumentRow.paper_id).where(
            SearchDocumentRow.document_type == "PAPER",
            SearchDocumentRow.content.ilike(f"%{_escape_like(term)}%", escape="\\"),
        )
        from sqlalchemy import or_

        stmt = stmt.where(or_(_title_match_condition(term), PaperRow.paper_id.in_(indexed)))

    if filters.collection_ids:
        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(CollectionPaperRow.paper_id).where(
                    CollectionPaperRow.collection_id.in_(filters.collection_ids)
                )
            )
        )
    if filters.tag_ids:
        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(PaperTagRow.paper_id).where(PaperTagRow.tag_id.in_(filters.tag_ids))
            )
        )
    if filters.tiers:
        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(TriageResultRow.paper_id).where(
                    TriageResultRow.effective_tier.in_(
                        [ResourceTier(tier) for tier in filters.tiers]
                    )
                )
            )
        )
    if filters.years:
        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(PaperVersionRow.paper_id).where(
                    func.extract("year", PaperVersionRow.publication_date).in_(filters.years)
                )
            )
        )
    if filters.audit_states:
        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(ClaimRow.paper_id).where(
                    ClaimRow.support_state.in_(
                        [SupportState(state) for state in filters.audit_states]
                    )
                )
            )
        )
    if filters.author_ids or filters.venue_ids:
        entity_ids = [*(filters.author_ids or []), *(filters.venue_ids or [])]
        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(RelationRow.paper_id).where(
                    RelationRow.paper_id.is_not(None),
                    (RelationRow.source_entity_id.in_(entity_ids))
                    | (RelationRow.target_entity_id.in_(entity_ids)),
                )
            )
        )
    if filters.read_states:
        from paperintel.database.models import UiPersonalPaperStateRow
        from paperintel.services.ui.personal import DEFAULT_OWNER

        stmt = stmt.where(
            PaperRow.paper_id.in_(
                select(UiPersonalPaperStateRow.paper_id).where(
                    UiPersonalPaperStateRow.owner_key == DEFAULT_OWNER,
                    UiPersonalPaperStateRow.read_state.in_(filters.read_states),
                )
            )
            | PaperRow.paper_id.not_in(select(UiPersonalPaperStateRow.paper_id))
            if "UNREAD" in filters.read_states
            else PaperRow.paper_id.in_(
                select(UiPersonalPaperStateRow.paper_id).where(
                    UiPersonalPaperStateRow.owner_key == DEFAULT_OWNER,
                    UiPersonalPaperStateRow.read_state.in_(filters.read_states),
                )
            )
        )
    return stmt


def order_clause(query: LibraryQuery):
    if query.sort == "TITLE":
        return (PaperRow.normalized_title.asc(), PaperRow.paper_id.asc())
    if query.sort == "RECENT":
        return (PaperRow.created_at.desc(), PaperRow.paper_id.asc())
    # RELEVANCE without a ranking model is defined as recency-first, and the
    # UI must not present it as paper quality (docs/02).
    return (PaperRow.created_at.desc(), PaperRow.paper_id.asc())


def query_library(
    session: Session, query: LibraryQuery, *, default_page: int | None = None
) -> LibraryPage:
    limit = min(query.limit or default_page or DEFAULT_PAGE, MAX_PAGE)
    query_filter_hash = filter_hash(query)
    scope = scope_revision(session)

    offset = 0
    sort_key = query.sort
    if query.cursor:
        payload = decode_cursor(query.cursor)
        if payload.get("f") != query_filter_hash or payload.get("s") != scope:
            raise UiError(
                "RESET_CURSOR",
                message=(
                    "The result scope changed since this cursor was issued; "
                    "restart from the first page."
                ),
                details={"filter_hash": query_filter_hash, "scope_revision": scope},
            )
        if payload.get("k") != sort_key:
            raise UiError(
                "RESET_CURSOR",
                message="The sort order changed since this cursor was issued.",
                details={"sort": sort_key},
            )
        offset = int(payload["o"])

    scoped = scoped_paper_ids(session, query)
    total = session.scalar(select(func.count()).select_from(scoped.subquery())) or 0
    rows = session.scalars(
        select(PaperRow.paper_id)
        .where(PaperRow.paper_id.in_(scoped))
        .order_by(*order_clause(query))
        .offset(offset)
        .limit(limit + 1)
    ).all()
    has_more = len(rows) > limit
    page_ids = list(rows[:limit])
    next_cursor = (
        encode_cursor(
            filter_hash_value=query_filter_hash,
            scope=scope,
            sort_key=sort_key,
            offset=offset + limit,
        )
        if has_more
        else None
    )
    return LibraryPage(
        paper_ids=page_ids,
        next_cursor=next_cursor,
        has_more=has_more,
        total=total,
        total_kind="EXACT",
        scope_revision=scope,
        filter_hash=query_filter_hash,
    )


def claim_candidates(session: Session, query: LibraryQuery, *, limit: int) -> list[ClaimRow]:
    """CLAIMS kind: claims inside the scoped papers (never papers themselves)."""
    scoped = scoped_paper_ids(session, query)
    stmt = select(ClaimRow).where(ClaimRow.paper_id.in_(scoped))
    if query.query.strip():
        stmt = stmt.where(
            ClaimRow.statement.ilike(f"%{_escape_like(query.query.strip())}%", escape="\\")
        )
    if query.filters.audit_states:
        stmt = stmt.where(
            ClaimRow.support_state.in_([SupportState(s) for s in query.filters.audit_states])
        )
    return list(
        session.scalars(stmt.order_by(ClaimRow.created_at.desc(), ClaimRow.claim_id).limit(limit))
    )


def technique_candidates(session: Session, query: LibraryQuery, *, limit: int) -> list[dict]:
    """TECHNIQUES kind: technique entities/method claims — not papers."""
    stmt = select(EntityRow).where(EntityRow.entity_type.in_(["TECHNIQUE", "METHOD"]))
    if query.query.strip():
        stmt = stmt.where(EntityRow.normalized_name.ilike(f"%{_escape_like(query.query.strip())}%"))
    rows = session.scalars(stmt.order_by(EntityRow.canonical_name).limit(limit)).all()
    return [
        {
            "entity_id": row.entity_id,
            "entity_type": row.entity_type.value
            if hasattr(row.entity_type, "value")
            else str(row.entity_type),
            "name": row.canonical_name,
            "aliases": list(row.aliases or []),
        }
        for row in rows
    ]


def tags_for_papers(session: Session, paper_ids: list[str]) -> dict[str, list[dict]]:
    if not paper_ids:
        return {}
    rows = session.execute(
        select(
            PaperTagRow.paper_id,
            TagRow.tag_id,
            TagRow.canonical_name,
            TagRow.namespace,
            PaperTagRow.is_candidate,
        )
        .join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
        .where(PaperTagRow.paper_id.in_(paper_ids))
    ).all()
    result: dict[str, list[dict]] = {}
    for paper_id, tag_id, label, namespace, is_candidate in rows:
        result.setdefault(paper_id, []).append(
            {
                "id": tag_id,
                "label": label,
                "namespace": namespace,
                "is_candidate": bool(is_candidate),
            }
        )
    return result


def _and(*conditions):
    return and_(*conditions)


__all__ = [
    "DEFAULT_PAGE",
    "MAX_PAGE",
    "LibraryPage",
    "claim_candidates",
    "decode_cursor",
    "encode_cursor",
    "filter_hash",
    "order_clause",
    "query_library",
    "scope_revision",
    "scoped_paper_ids",
    "tags_for_papers",
    "technique_candidates",
]

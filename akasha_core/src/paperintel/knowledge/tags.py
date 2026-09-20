"""knowledge.tags — canonical tag taxonomy + alias resolution
(P09, doc 01 §15).

Tags use canonical IDs with alias tables. Free-text model tags are
CANDIDATES only until normalization maps them onto a canonical tag (doc 01
§15): candidates are stored with ``is_candidate=True`` and never silently
promoted.

Normalization is deterministic: casefold, unify separators, strip
punctuation, collapse whitespace. Namespace families are the frozen list
from doc 01 §15.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import PaperTagRow, TagAliasRow, TagRow
from paperintel.errors import DomainError
from paperintel.ids import new_tag_id

#: Frozen namespace families (spec doc 01 §15).
TAG_NAMESPACES: tuple[str, ...] = (
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
)

_SEPARATOR_RE = re.compile(r"[\s_/\\]+")
_PUNCT_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_DASH_RE = re.compile(r"-{2,}")


def normalize_tag_name(name: str) -> str:
    """Deterministic tag normalization (casefold + separators + punctuation)."""
    value = _PUNCT_RE.sub(" ", name.strip().casefold())
    value = _SEPARATOR_RE.sub("-", value)
    value = _DASH_RE.sub("-", value)
    return value.strip("-")


def validate_namespace(namespace: str) -> str:
    normalized = namespace.strip().casefold()
    if normalized not in TAG_NAMESPACES:
        raise DomainError(
            "CFG_002",
            message=f"Unknown tag namespace: {namespace!r}",
            details={"namespace": namespace, "allowed": list(TAG_NAMESPACES)},
        )
    return normalized


@dataclass(slots=True)
class TagResolution:
    tag_id: str
    namespace: str
    canonical_name: str
    created: bool
    #: True when the input matched an existing ALIAS (not the canonical name).
    matched_alias: str | None = None


def resolve_tag(
    session: Session,
    *,
    namespace: str,
    name: str,
    aliases: list[str] | None = None,
) -> TagResolution:
    """Resolve a free-text tag to its canonical tag, creating it if new.

    Alias handling: an input matching a registered alias resolves to that
    alias's canonical tag — the input is never duplicated as a new tag.
    """
    namespace = validate_namespace(namespace)
    normalized = normalize_tag_name(name)
    if not normalized:
        raise DomainError(
            "CFG_002",
            message=f"Tag name normalizes to empty: {name!r}",
            details={"name": name, "namespace": namespace},
        )

    alias_row = session.scalar(
        select(TagAliasRow).join(TagRow, TagAliasRow.tag_id == TagRow.tag_id).where(
            TagAliasRow.normalized_alias == normalized, TagRow.namespace == namespace
        )
    )
    if alias_row is not None:
        tag = session.get(TagRow, alias_row.tag_id)
        if tag is not None:
            return TagResolution(
                tag_id=tag.tag_id,
                namespace=tag.namespace,
                canonical_name=tag.canonical_name,
                created=False,
                matched_alias=alias_row.alias,
            )

    tag = session.scalar(
        select(TagRow).where(TagRow.namespace == namespace, TagRow.normalized_name == normalized)
    )
    if tag is not None:
        _register_aliases(session, tag.tag_id, aliases or [])
        return TagResolution(
            tag_id=tag.tag_id,
            namespace=tag.namespace,
            canonical_name=tag.canonical_name,
            created=False,
        )

    tag = TagRow(
        tag_id=new_tag_id(),
        namespace=namespace,
        canonical_name=name.strip(),
        normalized_name=normalized,
    )
    session.add(tag)
    session.flush()
    _register_aliases(session, tag.tag_id, aliases or [])
    return TagResolution(
        tag_id=tag.tag_id,
        namespace=tag.namespace,
        canonical_name=tag.canonical_name,
        created=True,
    )


def _register_aliases(session: Session, tag_id: str, aliases: list[str]) -> int:
    """Register aliases for a tag (idempotent; a conflicting alias that
    already points elsewhere is a hard error — silent re-pointing would
    corrupt the taxonomy)."""
    added = 0
    for alias in aliases:
        normalized = normalize_tag_name(alias)
        if not normalized:
            continue
        existing = session.scalar(
            select(TagAliasRow).where(TagAliasRow.normalized_alias == normalized)
        )
        if existing is not None:
            if existing.tag_id != tag_id:
                raise DomainError(
                    "CFG_002",
                    message=(
                        f"Alias {alias!r} already belongs to tag {existing.tag_id}; "
                        "aliases cannot be silently re-pointed."
                    ),
                    details={"alias": alias, "existing_tag_id": existing.tag_id},
                )
            continue
        session.add(TagAliasRow(tag_id=tag_id, alias=alias.strip(), normalized_alias=normalized))
        added += 1
    session.flush()
    return added


def attach_tag(
    session: Session,
    *,
    paper_id: str,
    namespace: str,
    name: str,
    aliases: list[str] | None = None,
    created_by_run_id: str | None = None,
    is_candidate: bool = False,
) -> tuple[str, bool]:
    """Attach a tag to a paper. Returns (tag_id, paper_link_created).

    ``is_candidate=True`` marks a free-text model tag that has not passed
    normalization review (doc 01 §15).
    """
    resolution = resolve_tag(session, namespace=namespace, name=name, aliases=aliases)
    existing = session.scalar(
        select(PaperTagRow).where(
            PaperTagRow.paper_id == paper_id, PaperTagRow.tag_id == resolution.tag_id
        )
    )
    if existing is not None:
        if is_candidate and not existing.is_candidate:
            # A canonical link already exists — never downgrade it.
            return resolution.tag_id, False
        return resolution.tag_id, False
    session.add(
        PaperTagRow(
            paper_id=paper_id,
            tag_id=resolution.tag_id,
            created_by_run_id=created_by_run_id,
            is_candidate=is_candidate,
        )
    )
    session.flush()
    return resolution.tag_id, True


def promote_candidate(session: Session, *, paper_id: str, tag_id: str) -> bool:
    """Promote a candidate tag link to canonical (explicit review step)."""
    link = session.scalar(
        select(PaperTagRow).where(PaperTagRow.paper_id == paper_id, PaperTagRow.tag_id == tag_id)
    )
    if link is None:
        raise DomainError(
            "CFG_002",
            message="Cannot promote a tag link that does not exist.",
            details={"paper_id": paper_id, "tag_id": tag_id},
        )
    if not link.is_candidate:
        return False
    link.is_candidate = False
    session.flush()
    return True


def paper_tags(session: Session, paper_id: str, *, include_candidates: bool = True) -> list[dict]:
    """List a paper's tags with namespace + canonical name."""
    stmt = (
        select(PaperTagRow, TagRow)
        .join(TagRow, TagRow.tag_id == PaperTagRow.tag_id)
        .where(PaperTagRow.paper_id == paper_id)
    )
    if not include_candidates:
        stmt = stmt.where(PaperTagRow.is_candidate.is_(False))
    rows = session.execute(stmt.order_by(TagRow.namespace, TagRow.normalized_name)).all()
    return [
        {
            "tag_id": tag.tag_id,
            "namespace": tag.namespace,
            "canonical_name": tag.canonical_name,
            "is_candidate": link.is_candidate,
        }
        for link, tag in rows
    ]


def tags_in_namespace(session: Session, namespace: str) -> list[TagRow]:
    namespace = validate_namespace(namespace)
    return list(
        session.scalars(
            select(TagRow).where(TagRow.namespace == namespace).order_by(TagRow.normalized_name)
        )
    )

"""graph.service — entities, relations and graph expansion
(P09, doc 01 §16).

Entity types and the relation framework are the frozen doc 01 §16 lists;
relation types are a generic framework (examples in the spec, plain
strings accepted). Entity identity is (entity_type, normalized_name) with
an alias list, so "ResNet-50" and "resnet50" resolve to one entity.

Entities are derived from VERIFIED-CAPABLE claims (categories are the
contract): a claim build path maps claim categories to entity kinds, and
relations are recorded with their evidence IDs + confidence + run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import ClaimRow, EntityRow, RelationRow
from paperintel.errors import DomainError
from paperintel.ids import new_entity_id, new_relation_id
from paperintel.schemas.enums import EntityType

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

#: Claim category prefix → entity type (doc 01 §9 ↔ §16 mapping).
CATEGORY_ENTITY_MAP: tuple[tuple[str, EntityType], ...] = (
    ("method.", EntityType.METHOD),
    ("experiment.dataset", EntityType.DATASET),
    ("experiment.metric", EntityType.METRIC),
    ("technique.", EntityType.TECHNIQUE),
    ("people.author", EntityType.AUTHOR),
    ("people.affiliation", EntityType.AFFILIATION),
    ("contribution.", EntityType.METHOD),
    ("research.question", EntityType.TASK),
)

#: Statement → entity name extraction (the claim's subject phrase).
_SUBJECT_RE = re.compile(
    r"^(?:the\s+|our\s+|a\s+|an\s+)?(?P<subject>[A-Za-z0-9][A-Za-z0-9 _./+-]{1,60}?)"
    r"\s+(?:is|are|was|were|reaches|reached|achieves|achieved|uses|used|"
    r"contains|contained|outperforms|improves|reports|shows|provides)\b",
    re.IGNORECASE,
)


def _enum_value(value: object) -> str:
    """Enum columns round-trip through VARCHAR; normalise to a string."""
    return getattr(value, "value", str(value))


def normalize_entity_name(name: str) -> str:
    """Deterministic entity normalization (casefold + alphanumeric skeleton)."""
    return _NORMALIZE_RE.sub(" ", name.strip().casefold()).strip()


@dataclass(slots=True)
class EntityResolution:
    entity_id: str
    entity_type: str
    canonical_name: str
    created: bool
    matched_alias: bool = False


def upsert_entity(
    session: Session,
    *,
    entity_type: EntityType | str,
    name: str,
    aliases: list[str] | None = None,
    metadata_json: dict | None = None,
) -> EntityResolution:
    """Idempotent entity upsert by (type, normalized name); aliases are
    accumulated, never dropped."""
    entity_type_value = (
        entity_type.value if isinstance(entity_type, EntityType) else str(entity_type)
    )
    normalized = normalize_entity_name(name)
    if not normalized:
        raise DomainError(
            "CFG_002",
            message=f"Entity name normalizes to empty: {name!r}",
            details={"name": name, "entity_type": entity_type_value},
        )

    entity = session.scalar(
        select(EntityRow).where(
            EntityRow.entity_type == entity_type_value,
            EntityRow.normalized_name == normalized,
        )
    )
    if entity is not None:
        if aliases:
            merged = list(dict.fromkeys([*(entity.aliases or []), *aliases]))
            entity.aliases = merged
            session.flush()
        return EntityResolution(
            entity_id=entity.entity_id,
            entity_type=_enum_value(entity.entity_type),
            canonical_name=entity.canonical_name,
            created=False,
        )

    # Alias lookup: an alias of an existing entity resolves to it.
    for candidate in session.scalars(
        select(EntityRow).where(EntityRow.entity_type == entity_type_value)
    ):
        alias_norms = {normalize_entity_name(alias) for alias in (candidate.aliases or [])}
        if normalized in alias_norms:
            return EntityResolution(
                entity_id=candidate.entity_id,
                entity_type=_enum_value(candidate.entity_type),
                canonical_name=candidate.canonical_name,
                created=False,
                matched_alias=True,
            )

    entity = EntityRow(
        entity_id=new_entity_id(),
        entity_type=entity_type_value,
        canonical_name=name.strip(),
        normalized_name=normalized,
        aliases=list(dict.fromkeys(aliases or [])) or None,
        metadata_json=metadata_json,
    )
    session.add(entity)
    session.flush()
    return EntityResolution(
        entity_id=entity.entity_id,
        entity_type=_enum_value(entity.entity_type),
        canonical_name=entity.canonical_name,
        created=True,
    )


def add_relation(
    session: Session,
    *,
    source_entity_id: str,
    relation_type: str,
    target_entity_id: str,
    paper_id: str | None = None,
    evidence_ids: list[str] | None = None,
    confidence: float | None = None,
    run_id: str | None = None,
) -> tuple[str, bool]:
    """Record a relation (idempotent by source+type+target+paper).

    Returns (relation_id, created). Evidence IDs and confidence are carried
    so a relation is always traceable (doc 01 §16).
    """
    if not relation_type.strip():
        raise DomainError("CFG_002", message="relation_type must be non-empty", details={})
    if source_entity_id == target_entity_id:
        raise DomainError(
            "CFG_002",
            message="Self-relations are rejected (a relation needs two entities).",
            details={"entity_id": source_entity_id, "relation_type": relation_type},
        )
    for entity_id in (source_entity_id, target_entity_id):
        if session.get(EntityRow, entity_id) is None:
            raise DomainError(
                "CFG_002",
                message=f"Unknown entity ID: {entity_id}",
                details={"entity_id": entity_id},
            )

    existing = session.scalar(
        select(RelationRow).where(
            RelationRow.source_entity_id == source_entity_id,
            RelationRow.relation_type == relation_type,
            RelationRow.target_entity_id == target_entity_id,
            RelationRow.paper_id == paper_id,
        )
    )
    if existing is not None:
        merged = list(dict.fromkeys([*(existing.evidence_ids or []), *(evidence_ids or [])]))
        existing.evidence_ids = merged or None
        if confidence is not None and (
            existing.confidence is None or confidence > existing.confidence
        ):
            existing.confidence = confidence
        session.flush()
        return existing.relation_id, False

    relation = RelationRow(
        relation_id=new_relation_id(),
        source_entity_id=source_entity_id,
        relation_type=relation_type,
        target_entity_id=target_entity_id,
        paper_id=paper_id,
        evidence_ids=list(dict.fromkeys(evidence_ids or [])) or None,
        confidence=confidence,
        created_by_run_id=run_id,
    )
    session.add(relation)
    session.flush()
    return relation.relation_id, True


def entity_from_claim(session: Session, claim: ClaimRow) -> EntityResolution | None:
    """Derive the claim's subject entity (category-driven, doc 01 §9 ↔ §16).

    Returns None when the claim's category carries no entity type — an
    honest absence, never a forced mapping.
    """
    entity_type = entity_type_for_category(claim.category)
    if entity_type is None:
        return None
    name = claim_subject(claim.statement)
    if not name:
        return None
    return upsert_entity(session, entity_type=entity_type, name=name)


def entity_type_for_category(category: str) -> EntityType | None:
    for prefix, entity_type in CATEGORY_ENTITY_MAP:
        if category.startswith(prefix):
            return entity_type
    return None


def claim_subject(statement: str) -> str | None:
    """Extract the subject phrase of a claim statement (the entity name)."""
    match = _SUBJECT_RE.match(statement.strip())
    if match is None:
        return None
    subject = match.group("subject").strip(" .,:;")
    return subject or None


def link_claim_entities(
    session: Session,
    *,
    paper_id: str,
    claims: list[ClaimRow],
) -> dict[str, int]:
    """Build the graph from claims: a PAPER entity, the claim's subject
    entity, and REPORTS_METRIC/USES_METHOD-style relations carrying the
    claim's evidence IDs.

    Every relation is evidence-linked; nothing enters the graph without a
    provenance trail.
    """
    paper_entity = upsert_entity(
        session,
        entity_type=EntityType.PAPER,
        name=paper_id,
        metadata_json={"paper_id": paper_id},
    )
    stats = {"entities_created": 0, "relations_created": 0, "claims_linked": 0}

    from paperintel.database.models import ClaimEvidenceRow

    for claim in claims:
        resolution = entity_from_claim(session, claim)
        if resolution is None:
            continue
        stats["entities_created"] += 1 if resolution.created else 0
        evidence_ids = list(
            session.scalars(
                select(ClaimEvidenceRow.evidence_id).where(
                    ClaimEvidenceRow.claim_id == claim.claim_id
                )
            )
        )
        _, created = add_relation(
            session,
            source_entity_id=paper_entity.entity_id,
            relation_type=_relation_for_category(claim.category),
            target_entity_id=resolution.entity_id,
            paper_id=paper_id,
            evidence_ids=evidence_ids,
            confidence=claim.analysis_confidence,
            run_id=claim.created_by_run_id,
        )
        stats["relations_created"] += 1 if created else 0
        stats["claims_linked"] += 1

    return stats


def _relation_for_category(category: str) -> str:
    """Map a claim category to a doc 01 §16 example relation type."""
    if category.startswith(("method.", "technique.")):
        return "USES_METHOD"
    if category.startswith("experiment.dataset"):
        return "USES_DATASET"
    if category.startswith("experiment.metric"):
        return "REPORTS_METRIC"
    if category.startswith("people."):
        return "AUTHORED_BY"
    if category.startswith("critique."):
        return "CONTRADICTS"
    return "RELATED_TO"


def neighbors(
    session: Session,
    entity_id: str,
    *,
    depth: int = 1,
    relation_types: set[str] | None = None,
) -> dict[str, list[dict]]:
    """Breadth-first graph expansion up to ``depth`` hops.

    Returns {entity_id: [relations...]} for every reached entity, starting
    from the seed. Cycles terminate (visited set), and depth is bounded.
    """
    if depth < 1:
        raise DomainError("CFG_002", message="depth must be >= 1", details={"depth": depth})
    if session.get(EntityRow, entity_id) is None:
        raise DomainError(
            "CFG_002", message=f"Unknown entity ID: {entity_id}", details={"entity_id": entity_id}
        )

    visited: set[str] = {entity_id}
    frontier = [entity_id]
    result: dict[str, list[dict]] = {entity_id: []}

    for _ in range(depth):
        next_frontier: list[str] = []
        for current in frontier:
            stmt = select(RelationRow).where(
                (RelationRow.source_entity_id == current)
                | (RelationRow.target_entity_id == current)
            )
            if relation_types:
                stmt = stmt.where(RelationRow.relation_type.in_(relation_types))
            for relation in session.scalars(stmt):
                other = (
                    relation.target_entity_id
                    if relation.source_entity_id == current
                    else relation.source_entity_id
                )
                edge = {
                    "relation_id": relation.relation_id,
                    "relation_type": relation.relation_type,
                    "source_entity_id": relation.source_entity_id,
                    "target_entity_id": relation.target_entity_id,
                    "paper_id": relation.paper_id,
                    "evidence_ids": relation.evidence_ids or [],
                    "confidence": relation.confidence,
                }
                result.setdefault(current, []).append(edge)
                if other not in visited:
                    visited.add(other)
                    result.setdefault(other, [])
                    next_frontier.append(other)
        frontier = next_frontier
        if not frontier:
            break
    return result

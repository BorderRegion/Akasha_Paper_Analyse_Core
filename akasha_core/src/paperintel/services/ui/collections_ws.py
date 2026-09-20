"""services.ui.collections_ws — collection workspace, compare and entities.

All three read models are built from STORED analysis:

- a collection workspace reports its own scope (collection id, selection hash,
  sample size, time range) so a subset's conclusion is never presented as a
  domain-wide trend;
- a comparison only quantifies cells whose protocol_key matches; cells that are
  not comparable say WHY instead of producing a number;
- missing values stay missing (``missing_reason``) and are never turned into 0;
- entity cards use stored identity records (aliases/relations) — the UI never
  guesses who someone is from their name.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    ClaimRow,
    CollectionPaperRow,
    CollectionRow,
    EntityRow,
    PaperVersionRow,
    RelationRow,
)
from paperintel.errors import DomainError
from paperintel.schemas.enums import SupportState

#: Comparison dimensions answered from stored claims (docs/03 §S09).
COMPARE_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("problem", "研究问题"),
    ("method", "方法"),
    ("hypothesis", "假设"),
    ("dataset", "数据"),
    ("cost", "成本"),
    ("evidence", "证据强度"),
    ("limitation", "局限"),
)

#: Category prefixes that feed each dimension.
_DIMENSION_CATEGORIES: dict[str, tuple[str, ...]] = {
    "problem": ("research.", "contribution."),
    "method": ("method.",),
    "hypothesis": ("hypothesis.",),
    "dataset": ("experiment.",),
    "cost": ("experiment.",),
    "evidence": ("result.", "reliability."),
    "limitation": ("limitation.", "critique."),
}


def collection_workspace(session: Session, collection_id: str) -> dict[str, Any]:
    collection = session.get(CollectionRow, collection_id)
    if collection is None:
        raise DomainError(
            "CFG_002",
            message=f"Unknown collection: {collection_id}",
            details={"collection_id": collection_id},
        )
    paper_ids = list(
        session.scalars(
            select(CollectionPaperRow.paper_id).where(
                CollectionPaperRow.collection_id == collection_id
            )
        )
    )
    versions = list(
        session.scalars(
            select(PaperVersionRow).where(PaperVersionRow.paper_id.in_(paper_ids or [""]))
        )
    )
    version_ids = [version.paper_version_id for version in versions]
    claims = list(
        session.scalars(select(ClaimRow).where(ClaimRow.paper_version_id.in_(version_ids or [""])))
    )
    analyzed = {
        claim.paper_version_id
        for claim in claims
        if claim.support_state is not SupportState.UNVERIFIED
    }
    years = [version.publication_date.year for version in versions if version.publication_date]
    selection_hash = hashlib.sha256("|".join(sorted(version_ids)).encode("utf-8")).hexdigest()[:16]

    insights: list[dict[str, Any]] = []
    for state in (SupportState.DISPUTED, SupportState.PARTIALLY_SUPPORTED):
        for claim in claims:
            if claim.support_state is not state:
                continue
            insights.append(
                {
                    "title": "存在争议" if state is SupportState.DISPUTED else "部分支持",
                    "claim_type": claim.claim_type,
                    "statement": claim.statement,
                    "source_refs": [
                        {
                            "paper_id": claim.paper_id,
                            "paper_version_id": claim.paper_version_id,
                            "claim_id": claim.claim_id,
                            "evidence_ids": [],
                        }
                    ],
                }
            )
            if len(insights) >= 6:
                break

    return {
        "collection_id": collection.collection_id,
        "title": collection.name,
        "selection_hash": selection_hash,
        # Scope facts: a subset is a subset, and the UI must be able to say so.
        "selected_paper_count": len(paper_ids),
        "analyzed_paper_count": len(analyzed),
        "analysis_revision": f"{selection_hash}-{len(claims)}",
        "sample": {
            "paper_versions": len(version_ids),
            "claims": len(claims),
            "years": [min(years), max(years)] if years else None,
        },
        "insights": insights,
    }


def compare(session: Session, paper_version_ids: list[str]) -> dict[str, Any]:
    """Side-by-side comparison with explicit comparability per cell.

    A cell carries a value ONLY when the stored claim exists; when two versions
    are not comparable the cell says why and carries no number at all.
    """
    if len(paper_version_ids) < 2:
        raise DomainError(
            "CFG_002",
            message="A comparison needs 2–4 versions.",
            details={"paper_version_ids": paper_version_ids},
        )
    if len(paper_version_ids) > 4:
        raise DomainError(
            "CFG_002",
            message="At most 4 versions can be compared at once.",
            details={"count": len(paper_version_ids)},
        )

    versions: dict[str, PaperVersionRow] = {}
    for version_id in paper_version_ids:
        version = session.get(PaperVersionRow, version_id)
        if version is None:
            raise DomainError(
                "CFG_002",
                message=f"Unknown paper_version_id: {version_id}",
                details={"paper_version_id": version_id},
            )
        versions[version_id] = version

    selection_hash = hashlib.sha256(
        "|".join(sorted(paper_version_ids)).encode("utf-8")
    ).hexdigest()[:16]

    claims_by_version: dict[str, list[ClaimRow]] = {
        version_id: list(
            session.scalars(
                select(ClaimRow)
                .where(ClaimRow.paper_version_id == version_id)
                .order_by(ClaimRow.created_at, ClaimRow.claim_id)
            )
        )
        for version_id in paper_version_ids
    }

    cells: list[dict[str, Any]] = []
    for version_id in paper_version_ids:
        claims = claims_by_version[version_id]
        for dimension, _label in COMPARE_DIMENSIONS:
            prefixes = _DIMENSION_CATEGORIES[dimension]
            matched = [claim for claim in claims if claim.category.startswith(prefixes)]
            if not matched:
                cells.append(
                    {
                        "paper_version_id": version_id,
                        "dimension": dimension,
                        "value": None,
                        "unit": None,
                        "protocol_key": None,
                        "comparable": False,
                        "comparability_reason": "该版本没有该维度的已存结论（未报告，不是 0）",
                        "source_refs": [],
                        "missing_reason": "NOT_EXTRACTED",
                    }
                )
                continue
            claim = matched[0]
            protocol_key = _protocol_key(claim)
            cells.append(
                {
                    "paper_version_id": version_id,
                    "dimension": dimension,
                    "value": claim.statement,
                    "unit": None,
                    "protocol_key": protocol_key,
                    "comparable": protocol_key is not None,
                    "comparability_reason": None
                    if protocol_key is not None
                    else "该结论未记录可比较协议键，不参与量化比较",
                    "source_refs": [
                        {
                            "paper_id": claim.paper_id,
                            "paper_version_id": claim.paper_version_id,
                            "claim_id": claim.claim_id,
                            "evidence_ids": [],
                        }
                    ],
                    "missing_reason": None,
                }
            )

    # A quantitative comparison is allowed only when every version shares the
    # same protocol key for that dimension; otherwise the cell is marked as not
    # comparable and no number is computed (docs/03 §S09).
    for dimension, _label in COMPARE_DIMENSIONS:
        keys = {
            next(
                (
                    cell["protocol_key"]
                    for cell in cells
                    if cell["dimension"] == dimension and cell["paper_version_id"] == version_id
                ),
                None,
            )
            for version_id in paper_version_ids
        }
        if len(keys) > 1:
            for cell in cells:
                if cell["dimension"] != dimension:
                    continue
                cell["comparable"] = False
                cell["comparability_reason"] = (
                    "不同评估协议（dataset/split/metric/单位不同），未合并计算"
                )

    return {
        "paper_version_ids": paper_version_ids,
        "selection_hash": selection_hash,
        "analysis_revision": f"{selection_hash}-{sum(len(v) for v in claims_by_version.values())}",
        "cells": cells,
        "papers": {
            version_id: {
                "paper_id": version.paper_id,
                "version_label": version.version_label,
                "document_sha256": version.content_sha256,
                "publication_date": version.publication_date.isoformat()
                if isinstance(version.publication_date, datetime)
                else None,
            }
            for version_id, version in versions.items()
        },
    }


def _protocol_key(claim: ClaimRow) -> str | None:
    """The comparability key stored with the claim (never inferred from prose)."""
    components = claim.system_confidence_components or {}
    key = components.get("protocol_key") if isinstance(components, dict) else None
    return key if isinstance(key, str) and key else None


def entity_card(session: Session, entity_id: str) -> dict[str, Any]:
    entity = session.get(EntityRow, entity_id)
    if entity is None:
        raise DomainError(
            "GRAPH_001", message=f"Unknown entity: {entity_id}", details={"entity_id": entity_id}
        )
    relations = list(
        session.scalars(
            select(RelationRow).where(
                (RelationRow.source_entity_id == entity_id)
                | (RelationRow.target_entity_id == entity_id)
            )
        )
    )
    metadata = entity.metadata_json if isinstance(entity.metadata_json, dict) else {}
    description = (
        metadata.get("description") if isinstance(metadata.get("description"), str) else None
    )

    # Provenance comes from stored relations/evidence; the UI never guesses who
    # someone is from their name (docs/03 §S08).
    source_refs: list[dict[str, Any]] = []
    for relation in relations:
        if not relation.paper_id:
            continue
        source_refs.append(
            {
                "paper_id": relation.paper_id,
                "paper_version_id": None,
                "claim_id": None,
                "evidence_ids": list(relation.evidence_ids or []),
            }
        )
    return {
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type.value
        if hasattr(entity.entity_type, "value")
        else str(entity.entity_type),
        "name": entity.canonical_name,
        "aliases": sorted(
            {
                str(alias.get("value") if isinstance(alias, dict) else alias)
                for alias in (entity.aliases or [])
                if alias
            }
        ),
        "description": {
            "value": description,
            "missing_reason": None if description else "NOT_EXTRACTED",
            "source_refs": [],
        },
        "relations": [
            {
                "relation_type": relation.relation_type,
                "direction": "OUT" if relation.source_entity_id == entity_id else "IN",
                "other_entity_id": relation.target_entity_id
                if relation.source_entity_id == entity_id
                else relation.source_entity_id,
                "confidence": relation.confidence,
            }
            for relation in relations
        ],
        "source_refs": source_refs,
    }


def query_entities(
    session: Session,
    *,
    query: str = "",
    entity_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    stmt = select(EntityRow)
    if query.strip():
        stmt = stmt.where(EntityRow.normalized_name.like(f"%{query.strip().lower()}%"))
    if entity_type:
        stmt = stmt.where(EntityRow.entity_type == entity_type)
    rows = session.scalars(stmt.order_by(EntityRow.canonical_name).limit(limit))
    return [entity_card(session, row.entity_id) for row in rows]


__all__ = [
    "COMPARE_DIMENSIONS",
    "collection_workspace",
    "compare",
    "entity_card",
    "query_entities",
]

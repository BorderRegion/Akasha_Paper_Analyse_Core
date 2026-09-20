"""evidence.retrieval — evidence retrieval tools (P04).

Read-side contracts for every later consumer (agents P06, verification P07,
API P11, MCP): IDs resolve or fail loudly (EVIDENCE_001), scope mismatches
are rejected (EVIDENCE_002), and the section tree is returned as stable
nested SectionNode models.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import EvidenceRow, PaperVersionRow, SectionRow
from paperintel.errors import DomainError
from paperintel.schemas.common import BBox
from paperintel.schemas.enums import DataQualityState, EvidenceType
from paperintel.schemas.evidence import Evidence
from paperintel.schemas.structure import SectionNode


def get_evidence(session: Session, evidence_id: str) -> EvidenceRow:
    """Resolve one evidence ID or raise EVIDENCE_001 (never return None
    silently — an unresolvable locator is a hard error for callers)."""
    row = session.get(EvidenceRow, evidence_id)
    if row is None:
        raise DomainError(
            "EVIDENCE_001",
            message=f"Unknown evidence ID: {evidence_id}",
            details={"evidence_id": evidence_id},
        )
    return row


def get_evidence_in_scope(session: Session, evidence_id: str, paper_version_id: str) -> EvidenceRow:
    """Resolve + enforce scope (EVIDENCE_002 on mismatch)."""
    row = get_evidence(session, evidence_id)
    if row.paper_version_id != paper_version_id:
        raise DomainError(
            "EVIDENCE_002",
            message="Evidence belongs to a different paper version than the declared scope.",
            details={
                "evidence_id": evidence_id,
                "evidence_version": row.paper_version_id,
                "declared_version": paper_version_id,
            },
        )
    return row


def evidence_to_contract(row: EvidenceRow) -> Evidence:
    """Materialize the frozen Evidence contract model from a row."""
    return Evidence(
        evidence_id=row.evidence_id,
        paper_version_id=row.paper_version_id,
        evidence_type=row.evidence_type,
        page_start=row.page_start,
        page_end=row.page_end,
        bbox=BBox.model_validate(row.bbox) if row.bbox else None,
        section_id=row.section_id,
        text=row.text,
        asset_id=row.asset_id,
        source_method=row.source_method,
        ocr_confidence=row.ocr_confidence,
        quality_state=row.quality_state,
        content_sha256=row.content_sha256,
        extraction_run_id=row.extraction_run_id,
        created_at=row.created_at,
        supersedes_evidence_id=row.supersedes_evidence_id,
    )


def list_evidence(
    session: Session,
    paper_version_id: str,
    *,
    pages: set[int] | None = None,
    section_id: str | None = None,
    evidence_types: set[EvidenceType] | None = None,
    quality_states: set[DataQualityState] | None = None,
    include_superseded: bool = True,
) -> list[EvidenceRow]:
    """Filtered evidence listing in stable locator order (page, then
    evidence_id tiebreak — deterministic output)."""
    stmt = (
        select(EvidenceRow)
        .where(EvidenceRow.paper_version_id == paper_version_id)
        .order_by(EvidenceRow.page_start, EvidenceRow.evidence_id)
    )
    if pages:
        stmt = stmt.where(EvidenceRow.page_start.in_(pages))
    if section_id is not None:
        stmt = stmt.where(EvidenceRow.section_id == section_id)
    if evidence_types:
        stmt = stmt.where(EvidenceRow.evidence_type.in_(evidence_types))
    if quality_states:
        stmt = stmt.where(EvidenceRow.quality_state.in_(quality_states))
    if not include_superseded:
        superseded_ids = select(EvidenceRow.supersedes_evidence_id).where(
            EvidenceRow.supersedes_evidence_id.is_not(None),
            EvidenceRow.paper_version_id == paper_version_id,
        )
        stmt = stmt.where(EvidenceRow.evidence_id.not_in(superseded_ids))
    return list(session.scalars(stmt).all())


def section_tree(session: Session, paper_version_id: str) -> list[SectionNode]:
    """Nested section forest with evidence counts (ordinal-stable)."""
    rows = session.scalars(
        select(SectionRow)
        .where(SectionRow.paper_version_id == paper_version_id)
        .order_by(SectionRow.ordinal)
    ).all()
    counts: dict[str, int] = {}
    for (section_id,) in session.execute(
        select(EvidenceRow.section_id)
        .where(
            EvidenceRow.paper_version_id == paper_version_id,
            EvidenceRow.section_id.is_not(None),
        )
        .order_by(EvidenceRow.section_id)
    ).all():
        counts[section_id] = counts.get(section_id, 0) + 1

    by_id = {row.section_id: row for row in rows}
    levels = {row.section_id: _level_of(row, by_id) for row in rows}

    def build(section_id: str) -> SectionNode:
        row = by_id[section_id]
        child_ids = sorted(
            (sid for sid, other in by_id.items() if other.parent_section_id == section_id),
            key=lambda sid: by_id[sid].ordinal,
        )
        return SectionNode(
            section_id=section_id,
            paper_version_id=row.paper_version_id,
            parent_section_id=row.parent_section_id,
            ordinal=row.ordinal,
            level=levels[section_id],
            original_heading=row.original_heading,
            normalized_class=row.normalized_class,
            page_start=row.page_start,
            page_end=row.page_end,
            evidence_count=counts.get(section_id, 0),
            children=[build(cid) for cid in child_ids],
        )

    root_ids = [
        row.section_id
        for row in rows
        if not row.parent_section_id or row.parent_section_id not in by_id
    ]
    return [build(rid) for rid in sorted(root_ids, key=lambda sid: by_id[sid].ordinal)]


def _level_of(row: SectionRow, by_id: dict[str, SectionRow]) -> int:
    level = 0
    parent_id = row.parent_section_id
    seen = {row.section_id}
    while parent_id and parent_id in by_id and parent_id not in seen:
        seen.add(parent_id)
        level += 1
        parent_id = by_id[parent_id].parent_section_id
    return level


def version_summary(session: Session, version: PaperVersionRow) -> dict[str, int]:
    """Evidence counts per type/quality for one version (inspect command).

    The version row must already be resolved by the caller.
    """
    rows = session.scalars(
        select(EvidenceRow).where(EvidenceRow.paper_version_id == version.paper_version_id)
    ).all()
    by_type: dict[str, int] = {}
    by_quality: dict[str, int] = {}
    superseded = {row.supersedes_evidence_id for row in rows if row.supersedes_evidence_id}
    for row in rows:
        by_type[row.evidence_type.value] = by_type.get(row.evidence_type.value, 0) + 1
        by_quality[row.quality_state.value] = by_quality.get(row.quality_state.value, 0) + 1
    return {
        "evidence_total": len(rows),
        "evidence_active": len(rows) - len(superseded),
        "evidence_superseded": len(superseded),
        "by_type": dict(sorted(by_type.items())),
        "by_quality": dict(sorted(by_quality.items())),
    }

"""evidence.store — immutable evidence persistence (P04, doc 03 §1.4).

Persistence rules:
- evidence rows are INSERT-only at the DB level (the P01 trigger
  ``evidence_immutable_guard`` blocks content UPDATEs and DELETEs);
  corrections create NEW rows linked via ``supersedes_evidence_id``;
- extraction→evidence is IDEMPOTENT: re-persisting the same report (or a
  re-extraction of the same version) reuses rows matched by the identity
  key (type, page, content hash, method, bbox, confidence) — no duplicates;
- units that cannot satisfy the frozen Evidence contract are EXCLUDED BY
  REASON (never forced in, never silently dropped):
  * ocr_confidence_missing — OCR-sourced text without provider confidence
    (plain_text backends) violates "OCR evidence must record confidence";
  * no_text_no_asset — evidence must carry text and/or an asset reference;
  * unknown_asset — unit references an image hash with no asset row.
- every persisted batch records its extraction run as an AnalysisRun row
  (agent_type="extraction", deterministic config hash) so the frozen
  ``extraction_run_id`` contract field always resolves.
- invalid page locations reject the entire report at the sanity boundary;
  they cannot reach per-unit persistence or silently become exclusions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import AnalysisRunRow, AssetRow, EvidenceRow, SectionRow
from paperintel.ids import new_evidence_id
from paperintel.schemas.enums import DataQualityState, SourceMethod, TaskState
from paperintel.schemas.extraction import ExtractedUnit, ExtractionReport
from paperintel.schemas.structure import PersistResult
from paperintel.structure.reconstruct import (
    SectionDraft,
    iter_units_in_reading_order,
    reconstruct_sections,
    unit_key,
)
from paperintel.version import PIPELINE_VERSION

#: Extractor/contract version mixed into the run config hash.
EVIDENCE_STORE_VERSION = "1.0.0"

#: Per-unit OCR confidence below which evidence quality is DEGRADED.
OCR_QUALITY_THRESHOLD = 0.6


def _section_identity(draft: SectionDraft) -> tuple:
    """Stable identity of a section draft within its version."""
    return (
        draft.ordinal,
        draft.original_heading,
        draft.normalized_class.value,
        draft.level,
        draft.page_start,
        draft.page_end,
        draft.parent_ordinal,
    )


def _evidence_identity(
    unit: ExtractedUnit,
    quality_state: DataQualityState,
    asset_id: str | None,
) -> tuple:
    """Stable identity of an evidence row within its version.

    The run id is deliberately NOT part of the identity: re-extraction of
    the same version must reuse existing rows, not fork duplicates.
    """
    bbox = unit.bbox.model_dump() if unit.bbox is not None else None
    return (
        unit.unit_type.value,
        unit.page_number,
        unit.content_sha256,
        unit.source_method.value,
        json.dumps(bbox, sort_keys=True) if bbox else None,
        unit.ocr_confidence,
        quality_state.value,
        asset_id,
    )


def unit_quality_state(unit: ExtractedUnit) -> DataQualityState:
    """Data-quality assessment for one unit (never merged with execution
    state; doc 02 §10)."""
    if unit.source_method is SourceMethod.PDF_NATIVE:
        return DataQualityState.GOOD
    # OCR-sourced (or mixed pages surface OCR units): confidence decides.
    if unit.ocr_confidence is None:
        return DataQualityState.DEGRADED
    if unit.ocr_confidence < OCR_QUALITY_THRESHOLD:
        return DataQualityState.DEGRADED
    return DataQualityState.GOOD


def _config_hash(report: ExtractionReport) -> str:
    from paperintel.config.fingerprint import analysis_config_hash

    return analysis_config_hash(stage="evidence.persist", store_version=EVIDENCE_STORE_VERSION)


def _ensure_analysis_run(
    session: Session, report: ExtractionReport, paper_id: str | None
) -> tuple[AnalysisRunRow, bool]:
    run = session.get(AnalysisRunRow, report.run_id)
    if run is not None:
        return run, False
    run = AnalysisRunRow(
        run_id=report.run_id,
        paper_id=paper_id,
        paper_version_id=report.paper_version_id,
        agent_type="extraction",
        pipeline_version=PIPELINE_VERSION,
        config_hash=_config_hash(report),
        model_id="none",
        provider_id="prv_none",
        status=TaskState.SUCCEEDED,
        started_at=report.started_at,
        finished_at=report.finished_at,
    )
    session.add(run)
    session.flush()
    return run, True


def persist_sections(
    session: Session,
    report: ExtractionReport,
    *,
    paper_id: str | None = None,
) -> tuple[dict[int, str], int, bool]:
    """STRUCTURED stage: reconstruct + persist the section forest for a
    version. Idempotent — existing trees are reused unchanged (stable tree
    contract). Returns ({ordinal → section_id}, created, reused)."""
    drafts, _assignment = reconstruct_sections(report)
    return _ensure_sections(session, report, drafts)


def _ensure_sections(
    session: Session,
    report: ExtractionReport,
    drafts: list[SectionDraft],
) -> tuple[dict[int, str], int, bool]:
    """Persist (or reuse) the section forest. Returns
    ({draft ordinal → section_id}, created_count, reused_flag)."""
    existing = session.scalars(
        select(SectionRow)
        .where(SectionRow.paper_version_id == report.paper_version_id)
        .order_by(SectionRow.ordinal)
    ).all()
    if existing:
        mapping = {row.ordinal: row.section_id for row in existing}
        return mapping, 0, True

    mapping: dict[int, str] = {}
    created = 0
    # Two passes: mint ids, then insert with resolved parents (the FK is
    # self-referential; parents always precede children in reading order).
    for draft in drafts:
        section_id = _new_section_id()
        mapping[draft.ordinal] = section_id
    for draft in drafts:
        parent_id = mapping.get(draft.parent_ordinal) if draft.parent_ordinal is not None else None
        session.add(
            SectionRow(
                section_id=mapping[draft.ordinal],
                paper_version_id=report.paper_version_id,
                parent_section_id=parent_id,
                ordinal=draft.ordinal,
                original_heading=draft.original_heading,
                normalized_class=draft.normalized_class,
                page_start=draft.page_start,
                page_end=draft.page_end,
            )
        )
        created += 1
    session.flush()
    return mapping, created, False


def _new_section_id() -> str:
    """Internal `sec_`+ULID convention (P01 documented; not a frozen public
    prefix — the structure contract only requires a stable opaque string)."""
    from ulid import ULID  # noqa: PLC0415

    return f"sec_{ULID()}"


def persist_extraction(
    session: Session,
    report: ExtractionReport,
    *,
    paper_id: str | None = None,
) -> PersistResult:
    """Persist one extraction report as immutable evidence + section tree.

    Idempotent: safe to call repeatedly for the same version/report.
    The caller owns the transaction (flush only, no commit).
    """
    if report.paper_version_id is None:
        raise ValueError("report.paper_version_id is required to persist evidence")

    from paperintel.evidence.sanity import require_safe_extraction

    warnings: list[str] = require_safe_extraction(report)
    excluded: dict[str, int] = {}

    def _exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    run, run_created = _ensure_analysis_run(session, report, paper_id)

    drafts, assignment = reconstruct_sections(report)
    if not drafts:
        warnings.append("STRUCTURE_001: no sections reconstructed (no units in report)")
    section_ids, sections_created, sections_reused = _ensure_sections(session, report, drafts)

    # Pre-load existing evidence identities for this version (idempotency).
    existing_rows = session.scalars(
        select(EvidenceRow).where(EvidenceRow.paper_version_id == report.paper_version_id)
    ).all()
    existing_by_identity: dict[tuple, EvidenceRow] = {}
    for row in existing_rows:
        identity = (
            row.evidence_type.value,
            row.page_start,
            row.content_sha256,
            row.source_method.value,
            json.dumps(row.bbox, sort_keys=True) if row.bbox else None,
            row.ocr_confidence,
            row.quality_state.value,
            row.asset_id,
        )
        existing_by_identity.setdefault(identity, row)

    # Asset lookup for figure/table units (crops persisted during import).
    asset_cache: dict[str, AssetRow | None] = {}

    def _asset_for(sha: str | None) -> AssetRow | None:
        if sha is None:
            return None
        if sha not in asset_cache:
            asset_cache[sha] = session.scalar(select(AssetRow).where(AssetRow.sha256 == sha))
        return asset_cache[sha]

    evidence_created = 0
    evidence_reused = 0
    for unit in iter_units_in_reading_order(report):
        quality = unit_quality_state(unit)

        # --- contract-level exclusions (frozen Evidence rules) ---------------
        if unit.source_method is SourceMethod.OCR and unit.ocr_confidence is None:
            _exclude("ocr_confidence_missing")
            continue

        asset_id: str | None = None
        if unit.asset_sha256:
            asset = _asset_for(unit.asset_sha256)
            if asset is None:
                _exclude("unknown_asset")
                continue
            asset_id = asset.asset_id
        if unit.text is None and asset_id is None:
            _exclude("no_text_no_asset")
            continue

        identity = _evidence_identity(unit, quality, asset_id)
        existing = existing_by_identity.get(identity)
        if existing is not None:
            evidence_reused += 1
            continue

        section_ordinal = assignment.get(unit_key(unit))
        section_id = section_ids.get(section_ordinal) if section_ordinal is not None else None
        row = EvidenceRow(
            evidence_id=new_evidence_id(),
            paper_version_id=report.paper_version_id,
            evidence_type=unit.unit_type,
            page_start=unit.page_number,
            page_end=unit.page_number,
            bbox=(unit.bbox.model_dump() if unit.bbox is not None else None),
            section_id=section_id,
            text=unit.text,
            asset_id=asset_id,
            source_method=unit.source_method,
            ocr_confidence=unit.ocr_confidence,
            quality_state=quality,
            content_sha256=unit.content_sha256,
            extraction_run_id=run.run_id,
        )
        session.add(row)
        existing_by_identity[identity] = row
        evidence_created += 1

    session.flush()

    if "ocr_confidence_missing" in excluded:
        warnings.append(
            f"{excluded['ocr_confidence_missing']} OCR unit(s) excluded: confidence-less "
            "backends cannot source canonical evidence (frozen contract); re-OCR with a "
            "confidence-capable provider to canonicalize"
        )
    if sections_reused:
        warnings.append("sections already present for this version; tree reused unchanged")

    return PersistResult(
        run_id=run.run_id,
        paper_version_id=report.paper_version_id,
        analysis_run_created=run_created,
        sections_created=sections_created,
        sections_reused=sections_reused,
        evidence_created=evidence_created,
        evidence_reused=evidence_reused,
        excluded=dict(sorted(excluded.items())),
        warnings=warnings,
    )


def record_correction(
    session: Session,
    superseded_evidence_id: str,
    *,
    corrected_text: str | None = None,
    corrected_bbox: dict[str, Any] | None = None,
    corrected_quality: DataQualityState | None = None,
    source_method: SourceMethod | None = None,
    ocr_confidence: float | None = None,
) -> EvidenceRow:
    """Correction = NEW evidence row linked via supersedes_evidence_id.

    The superseded row is never modified (trigger-enforced). At least one
    correction field is required; unspecified fields are copied from the
    original so the new row stands alone as complete evidence.
    """
    original = session.get(EvidenceRow, superseded_evidence_id)
    if original is None:
        from paperintel.errors import DomainError  # noqa: PLC0415

        raise DomainError(
            "EVIDENCE_001",
            message=f"Unknown evidence ID: {superseded_evidence_id}",
            details={"evidence_id": superseded_evidence_id},
        )
    if (
        corrected_text is None
        and corrected_bbox is None
        and corrected_quality is None
        and source_method is None
        and ocr_confidence is None
    ):
        raise ValueError("a correction must change at least one field")

    text = corrected_text if corrected_text is not None else original.text
    bbox = corrected_bbox if corrected_bbox is not None else original.bbox
    quality = corrected_quality if corrected_quality is not None else original.quality_state
    method = source_method if source_method is not None else original.source_method
    confidence = ocr_confidence if ocr_confidence is not None else original.ocr_confidence
    if method is SourceMethod.OCR and confidence is None:
        raise ValueError("OCR-derived evidence must record ocr_confidence")
    if text is None and original.asset_id is None:
        raise ValueError("evidence must carry text and/or an asset reference")

    content_sha = (
        hashlib.sha256((text or "").encode("utf-8")).hexdigest()
        if corrected_text is not None
        else original.content_sha256
    )
    row = EvidenceRow(
        evidence_id=new_evidence_id(),
        paper_version_id=original.paper_version_id,
        evidence_type=original.evidence_type,
        page_start=original.page_start,
        page_end=original.page_end,
        bbox=bbox,
        section_id=original.section_id,
        text=text,
        asset_id=original.asset_id,
        source_method=method,
        ocr_confidence=confidence,
        quality_state=quality,
        content_sha256=content_sha,
        extraction_run_id=original.extraction_run_id,
        supersedes_evidence_id=original.evidence_id,
    )
    session.add(row)
    session.flush()
    return row

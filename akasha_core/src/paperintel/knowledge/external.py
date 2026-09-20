"""Trusted metadata → EXTERNAL claims; LLMs cannot assert trusted provenance."""

import hashlib
import json

from sqlalchemy import select

from paperintel.config.fingerprint import analysis_config_hash
from paperintel.database.models import (
    AnalysisRunRow,
    ClaimRow,
    ExternalProvenanceRow,
    PaperVersionRow,
)
from paperintel.errors import DomainError
from paperintel.ids import new_claim_id, new_run_id
from paperintel.schemas.common import utcnow
from paperintel.schemas.enums import ClaimType, SupportState, TaskState
from paperintel.version import PIPELINE_VERSION


def persist_metadata(session, record, *, paper_id, paper_version_id, trace_id=None):
    version = session.get(PaperVersionRow, paper_version_id)
    if version is None or version.paper_id != paper_id:
        raise DomainError("EVIDENCE_002", message="External metadata target version mismatch.")
    if not record.provider or not record.identifier or record.retrieved_at is None:
        raise DomainError("CLAIM_001", message="External metadata is missing provenance.")
    canonical = json.dumps(record.data, sort_keys=True, ensure_ascii=False, default=str)
    content_hash = record.content_hash or hashlib.sha256(canonical.encode()).hexdigest()
    existing = session.scalars(
        select(ClaimRow.claim_id)
        .join(ExternalProvenanceRow)
        .where(
            ClaimRow.paper_version_id == paper_version_id,
            ExternalProvenanceRow.source_provider == record.provider,
            ExternalProvenanceRow.source_identifier == record.identifier,
            ExternalProvenanceRow.content_hash == content_hash,
        )
    ).all()
    if existing:
        return list(existing)
    run_id = new_run_id()
    now = utcnow()
    session.add(
        AnalysisRunRow(
            run_id=run_id,
            paper_id=paper_id,
            paper_version_id=paper_version_id,
            agent_type="metadata.resolve",
            pipeline_version=PIPELINE_VERSION,
            config_hash=analysis_config_hash(stage="metadata", provider=record.provider),
            provider_id=record.provider,
            model_id="none",
            status=TaskState.SUCCEEDED,
            started_at=now,
            finished_at=now,
            trace_id=trace_id,
        )
    )
    session.flush()
    claim_ids = []
    for key in ("title", "authors", "venue", "year", "doi"):
        if key not in record.data:
            continue
        claim_id = new_claim_id()
        statement = f"{key}: {json.dumps(record.data[key], ensure_ascii=False)}"
        session.add(
            ClaimRow(
                claim_id=claim_id,
                paper_id=paper_id,
                paper_version_id=paper_version_id,
                claim_type=ClaimType.EXTERNAL,
                category=f"external.metadata.{key}",
                statement=statement,
                normalized_statement=" ".join(statement.casefold().split()),
                support_state=SupportState.UNVERIFIED,
                created_by_run_id=run_id,
                pipeline_version=PIPELINE_VERSION,
            )
        )
        session.flush()
        session.add(
            ExternalProvenanceRow(
                claim_id=claim_id,
                source_provider=record.provider,
                source_identifier=record.identifier,
                source_url=record.source_url,
                retrieved_at=record.retrieved_at,
                content_hash=content_hash,
                citation_text=canonical,
            )
        )
        claim_ids.append(claim_id)
    session.flush()
    return claim_ids

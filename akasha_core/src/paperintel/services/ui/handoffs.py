"""services.ui.handoffs — external-agent handoff bundles (docs/06 §审查与外部Agent).

The bundle is a MANIFEST plus the requested content, built from stored records:

- ``scope``      — explicit paper version ids (never "the whole library");
- ``include``    — brief / claims / evidence_refs / audit / notes;
- ``limit`` and ``max_bytes`` are enforced BEFORE writing, and a request that
  would exceed them is refused with the real numbers instead of silently
  producing a truncated file;
- PDF binaries and raw model output are NOT included unless the user explicitly
  asks for them;
- every item carries the version ids and source hashes, so the receiving agent
  can verify what it got.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import ClaimEvidenceRow, ClaimRow
from paperintel.errors import DomainError
from paperintel.ids import new_ui_operation_id
from paperintel.schemas.common import utcnow

HANDOFF_SCHEMA_VERSION = "1.0.0"
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
MAX_ITEMS = 200
ALLOWED_INCLUDE = ("brief", "claims", "evidence_refs", "audit", "notes", "pdf", "model_raw")


@dataclass(slots=True)
class HandoffBundle:
    asset_id: str
    path: Path
    size_bytes: int
    manifest: dict[str, Any]


def build_handoff(
    session: Session,
    *,
    paper_version_ids: list[str],
    include: list[str],
    data_dir: Path,
    limit: int | None = None,
    max_bytes: int | None = None,
    owner_key: str = "local",
) -> HandoffBundle:
    from paperintel.database.models import PaperRow, PaperVersionRow, UiNoteRow

    if not paper_version_ids:
        raise DomainError(
            "CFG_002",
            message="A handoff needs an explicit scope (paper_version_ids).",
            details={},
        )
    unknown = [item for item in include if item not in ALLOWED_INCLUDE]
    if unknown:
        raise DomainError(
            "CFG_002",
            message=f"Unknown include entries: {unknown}",
            details={"allowed": list(ALLOWED_INCLUDE)},
        )
    limit = min(limit or MAX_ITEMS, MAX_ITEMS)
    max_bytes = max_bytes or DEFAULT_MAX_BYTES
    if len(paper_version_ids) > limit:
        raise DomainError(
            "CFG_002",
            message=f"The scope holds {len(paper_version_ids)} versions, above the limit {limit}.",
            details={"limit": limit, "requested": len(paper_version_ids)},
        )

    manifest: dict[str, Any] = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "generated_at": utcnow().isoformat(),
        "include": sorted(set(include)),
        "scope": {"paper_version_ids": list(paper_version_ids)},
        "notes": [
            "PDF binaries and raw model output are excluded unless requested.",
            "每一篇都带 source_hashes，接收方可以核对拿到的内容。",
        ],
        "papers": [],
    }
    for version_id in paper_version_ids:
        version = session.get(PaperVersionRow, version_id)
        if version is None:
            raise DomainError(
                "CFG_002",
                message=f"Unknown paper_version_id: {version_id}",
                details={"paper_version_id": version_id},
            )
        paper = session.get(PaperRow, version.paper_id)
        entry: dict[str, Any] = {
            "paper_id": version.paper_id,
            "paper_version_id": version_id,
            "source_hashes": {"document_sha256": version.content_sha256},
        }
        if paper is not None and "brief" in include:
            entry["brief"] = {
                "title": paper.canonical_title,
                "doi": paper.doi,
            }
            latest_run = session.scalar(
                select(ClaimRow.created_by_run_id)
                .where(ClaimRow.paper_version_id == version_id)
                .order_by(ClaimRow.created_at.desc(), ClaimRow.claim_id.desc()).limit(1)
            )
            entry["brief"]["analysis_run_id"] = latest_run
        claims = list(
            session.scalars(
                select(ClaimRow)
                .where(ClaimRow.paper_version_id == version_id)
                .order_by(ClaimRow.created_at, ClaimRow.claim_id)
                .limit(limit + 1)
            )
        )
        if len(claims) > limit and {"claims", "evidence_refs", "audit"}.intersection(include):
            raise DomainError(
                "STORAGE_001", message="The handoff claim count exceeds the item limit; reduce the scope.",
                details={"paper_version_id": version_id, "limit": limit, "claims_at_least": len(claims)},
            )
        if "claims" in include:
            entry["claims"] = [
                {
                    "claim_id": claim.claim_id,
                    "statement": claim.statement,
                    "claim_type": claim.claim_type.value,
                    "support_state": claim.support_state.value,
                    "category": claim.category,
                }
                for claim in claims
            ]
        if "evidence_refs" in include:
            entry["evidence_refs"] = [
                {
                    "claim_id": link.claim_id,
                    "evidence_id": link.evidence_id,
                    "role": link.role.value if hasattr(link.role, "value") else str(link.role),
                }
                for link in session.scalars(
                    select(ClaimEvidenceRow).where(
                        ClaimEvidenceRow.claim_id.in_([claim.claim_id for claim in claims] or [""])
                    )
                )
            ]
        if "audit" in include:
            from paperintel.database.models import VerificationRow

            entry["audit"] = [
                {
                    "verification_id": row.verification_id,
                    "verifier_type": row.verifier_type.value,
                    "verdict": row.verdict.value
                    if hasattr(row.verdict, "value")
                    else str(row.verdict),
                    "reason_summary": row.reason_summary,
                    "run_id": row.created_by_run_id,
                }
                for row in session.scalars(
                    select(VerificationRow)
                    .where(VerificationRow.claim_id.in_([c.claim_id for c in claims] or [""]))
                    .order_by(VerificationRow.created_at.desc())
                )
            ]
        if "notes" in include:
            entry["notes"] = [
                {
                    "note_id": row.note_id,
                    "body": row.body,
                    "revision": row.revision,
                    "claim_id": row.claim_id,
                }
                for row in session.scalars(
                    select(UiNoteRow).where(
                        UiNoteRow.owner_key == owner_key,
                        UiNoteRow.paper_version_id == version_id,
                    )
                )
            ]
        if "pdf" in include:
            # Explicit opt-in only. The path is reported, NOT embedded.
            entry["pdf"] = {"asset_id": version.asset_id, "included": "by_reference"}
        if "model_raw" in include:
            entry["model_raw"] = {"included": False, "reason": "需要显式导出作业，默认不带"}
        manifest["papers"].append(entry)

    encoded = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    if len(encoded) > max_bytes:
        raise DomainError(
            "STORAGE_001",
            message=f"The handoff is {len(encoded)} bytes, above max_bytes={max_bytes}.",
            details={"size_bytes": len(encoded), "max_bytes": max_bytes},
        )

    directory = Path(data_dir) / "exports" / "handoffs"
    directory.mkdir(parents=True, exist_ok=True)
    asset_id = new_ui_operation_id().replace("uop_", "ho_")
    target = directory / f"{asset_id}.json"
    target.write_bytes(encoded)
    manifest["size_bytes"] = len(encoded)
    manifest["asset_id"] = asset_id
    return HandoffBundle(asset_id=asset_id, path=target, size_bytes=len(encoded), manifest=manifest)


__all__ = [
    "ALLOWED_INCLUDE",
    "DEFAULT_MAX_BYTES",
    "HANDOFF_SCHEMA_VERSION",
    "MAX_ITEMS",
    "HandoffBundle",
    "build_handoff",
]

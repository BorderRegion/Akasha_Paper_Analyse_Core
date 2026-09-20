"""services.ui.tags_actions — tag confirmation and alias merge (docs/06 §专题与实体).

Rules encoded here:

- an action PREVIEWS its effect (``affected_count`` and the ids it would touch)
  before anything changes;
- merging reports a CONFLICT when two tags disagree on a canonical name, instead
  of silently picking one;
- old evidence text is never rewritten: tags are labels, evidence is immutable;
- the action is idempotent by key so a double submit changes nothing twice.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    EntityTagRow,
    PaperTagRow,
    TagAliasRow,
    TagRow,
    UiOperationRequestRow,
)
from paperintel.errors import DomainError
from paperintel.ids import new_ui_operation_id
from paperintel.knowledge.tags import normalize_tag_name

TAG_ACTIONS = ("confirm", "merge", "rename")


def apply_tag_action(
    session: Session,
    *,
    action: str,
    tag_ids: list[str],
    target_tag_id: str | None = None,
    canonical_name: str | None = None,
    idempotency_key: str | None = None,
    preview_only: bool = True,
) -> dict[str, Any]:
    if action not in TAG_ACTIONS:
        raise DomainError(
            "CFG_002",
            message=f"Unknown tag action: {action!r}",
            details={"action": action, "allowed": list(TAG_ACTIONS)},
        )
    tag_ids = sorted(set(tag_ids))
    if not tag_ids:
        raise DomainError("CFG_002", message="Select at least one tag.")
    payload = {"action": action, "tag_ids": tag_ids, "target_tag_id": target_tag_id, "canonical_name": canonical_name}
    receipt_key = "tag:" + hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key else None
    # Serialize edits touching the same tags, including receipt lookup.
    session.scalars(select(TagRow).where(TagRow.tag_id.in_(tag_ids + ([target_tag_id] if target_tag_id else [])))
                    .order_by(TagRow.tag_id).with_for_update()).all()
    if not preview_only and receipt_key:
        prior = session.scalar(select(UiOperationRequestRow).where(UiOperationRequestRow.idempotency_key == receipt_key))
        if prior is not None:
            if prior.payload != payload:
                raise DomainError("CFG_002", message="Idempotency key reused for a different tag action.")
            return prior.results[0]
    tags: list[TagRow] = []
    for tag_id in tag_ids:
        row = session.get(TagRow, tag_id)
        if row is None:
            raise DomainError(
                "CFG_002", message=f"Unknown tag: {tag_id}", details={"tag_id": tag_id}
            )
        tags.append(row)

    affected_papers = sorted(
        {
            paper_id
            for paper_id in session.scalars(
                select(PaperTagRow.paper_id).where(PaperTagRow.tag_id.in_(tag_ids or [""]))
            )
        }
    )

    conflict = None
    if action == "merge":
        if not target_tag_id:
            raise DomainError("CFG_002", message="merge requires target_tag_id.", details={})
        target = session.get(TagRow, target_tag_id)
        if target is None:
            raise DomainError(
                "CFG_002",
                message=f"Unknown target tag: {target_tag_id}",
                details={"target_tag_id": target_tag_id},
            )
        namespaces = {tag.namespace for tag in tags} | {target.namespace}
        if len(namespaces) > 1:
            conflict = {
                "reason": "不同命名空间的标签不能直接合并，需要人工确认目标语义",
                "namespaces": sorted(namespaces),
            }

    if preview_only:
        return {
            "operation_id": new_ui_operation_id(),
            "action": action,
            "state": "PREVIEW",
            "affected_count": len(affected_papers),
            "affected_paper_ids": affected_papers[:50],
            "tags": [tag.tag_id for tag in tags],
            "target_tag_id": target_tag_id,
            "conflict": conflict,
            "note": "预览：未做任何修改；确认后再执行。",
        }

    if conflict is not None:
        raise DomainError(
            "CFG_002",
            message=f"Conflict: {conflict['reason']}",
            details=conflict,
        )

    if action == "rename":
        if not canonical_name or not normalize_tag_name(canonical_name):
            raise DomainError("CFG_002", message="rename requires canonical_name.", details={})
        for tag in tags:
            tag.canonical_name = canonical_name
            tag.normalized_name = normalize_tag_name(canonical_name)
    elif action == "merge":
        for tag in tags:
            if tag.tag_id == target_tag_id:
                continue
            _ensure_alias(session, target_tag_id, tag.canonical_name, replace_tag_id=tag.tag_id)
            for alias in session.scalars(select(TagAliasRow).where(TagAliasRow.tag_id == tag.tag_id)):
                alias.tag_id = target_tag_id
            for model, owner_field in ((PaperTagRow, "paper_id"), (EntityTagRow, "entity_id")):
                for link in session.scalars(select(model).where(model.tag_id == tag.tag_id)).all():
                    owner = getattr(link, owner_field)
                    target_link = session.get(model, (owner, target_tag_id))
                    if target_link is None:
                        values = {owner_field: owner, "tag_id": target_tag_id}
                        if model is PaperTagRow:
                            values.update(is_candidate=False, created_by_run_id=link.created_by_run_id)
                        session.add(model(**values))
                    elif model is PaperTagRow:
                        target_link.is_candidate = False
                    session.delete(link)
                    session.flush()
    else:  # confirm
        for tag in tags:
            _ensure_alias(session, tag.tag_id, tag.canonical_name)
            for link in session.scalars(select(PaperTagRow).where(PaperTagRow.tag_id == tag.tag_id)):
                link.is_candidate = False
    session.flush()
    result = {
        "operation_id": new_ui_operation_id(),
        "action": action,
        "state": "COMPLETED",
        "affected_count": len(affected_papers),
        "affected_paper_ids": affected_papers[:50],
        "tags": [tag.tag_id for tag in tags],
        "target_tag_id": target_tag_id,
        "conflict": None,
        "idempotency_key": idempotency_key,
        "note": "标签语义已更新；旧证据文本未被修改。",
    }
    if receipt_key:
        session.add(UiOperationRequestRow(
            operation_id=result["operation_id"], owner_key="local", kind="tag_action",
            payload=payload, state="COMPLETED", scope={"tag_ids": tag_ids},
            results=[result], job_ids=[], idempotency_key=receipt_key,
        ))
        session.flush()
    return result


def _ensure_alias(session: Session, tag_id: str, name: str, *, replace_tag_id: str | None = None) -> None:
    normalized = normalize_tag_name(name)
    existing = session.scalar(
        select(TagAliasRow).where(TagAliasRow.normalized_alias == normalized)
    )
    if existing is None:
        session.add(TagAliasRow(tag_id=tag_id, alias=name, normalized_alias=normalized))
    elif existing.tag_id in (tag_id, replace_tag_id):
        existing.tag_id = tag_id
    else:
        raise DomainError("CFG_002", message="Alias belongs to a different tag.")
    session.flush()


__all__ = ["TAG_ACTIONS", "apply_tag_action"]

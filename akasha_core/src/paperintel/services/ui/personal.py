"""services.ui.personal — personal state, notes and review decisions.

Personal state is separated from scientific state by construction: nothing in
this module can write `claims.support_state`, `claims.claim_type` or any
verification row. "I have seen this" is recorded as a review decision and the
claim's support state stays exactly as the verification engine left it
(frontend spec docs/02、docs/06).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import (
    PaperRow,
    PaperVersionRow,
    UiNoteRow,
    UiPersonalPaperStateRow,
    UiPreferencesRow,
    UiReviewDecisionRow,
)
from paperintel.errors import DomainError
from paperintel.errors.ui_errors import UiError
from paperintel.ids import (
    new_ui_note_id,
    new_ui_review_decision_id,
)
from paperintel.schemas.common import utcnow
from paperintel.schemas.ui.models import ReadStateValue

DEFAULT_OWNER = "local"
REVIEW_DECISIONS = ("SEEN", "NEEDS_REVIEW", "RESERVATION")


@dataclass(slots=True)
class PersonalPatchResult:
    paper_id: str
    saved: bool
    read_state: str
    revision: int
    reading_anchor: dict | None


def _state(session: Session, paper_id: str, owner_key: str) -> UiPersonalPaperStateRow | None:
    return session.get(UiPersonalPaperStateRow, {"owner_key": owner_key, "paper_id": paper_id})


def personal_state(session: Session, paper_id: str, *, owner_key: str = DEFAULT_OWNER) -> dict:
    row = _state(session, paper_id, owner_key)
    if row is None:
        return {
            "saved": False,
            "read_state": ReadStateValue.UNREAD.value,
            "revision": 0,
            "reading_anchor": None,
        }
    return {
        "saved": row.saved,
        "read_state": row.read_state,
        "revision": row.revision,
        "reading_anchor": row.reading_anchor,
    }


def patch_personal_state(
    session: Session,
    paper_id: str,
    *,
    saved: bool | None = None,
    read_state: str | None = None,
    reading_anchor: dict | None = None,
    expected_revision: int | None = None,
    owner_key: str = DEFAULT_OWNER,
) -> PersonalPatchResult:
    """PATCH semantics with an explicit concurrency guard.

    ``expected_revision`` mismatch → 409 REVISION_CONFLICT so the client can
    merge instead of silently overwriting (docs/06「并发编辑」).
    """
    if session.scalar(select(PaperRow).where(PaperRow.paper_id == paper_id).with_for_update()) is None:
        raise DomainError(
            "CFG_002", message=f"Unknown paper ID: {paper_id}", details={"paper_id": paper_id}
        )
    if read_state is not None:
        try:
            ReadStateValue(read_state)
        except ValueError as exc:
            raise DomainError(
                "CFG_002",
                message=f"Unknown read state: {read_state!r}",
                details={"read_state": read_state},
            ) from exc
    anchor_model = None
    if reading_anchor is not None:
        # Validated at WRITE time: an anchor that later fails to parse would
        # break every read of this paper (the reader pins version/page/section).
        from pydantic import ValidationError

        from paperintel.schemas.ui.models import ReadingAnchor

        try:
            anchor_model = ReadingAnchor.model_validate(reading_anchor)
        except ValidationError as exc:
            raise DomainError(
                "CFG_002",
                message="reading_anchor does not match the anchor contract.",
                details={"errors": [str(error["msg"]) for error in exc.errors()]},
            ) from exc
        version = (
            session.get(PaperVersionRow, anchor_model.paper_version_id)
            if anchor_model.paper_version_id
            else None
        )
        if version is None or version.paper_id != paper_id:
            raise DomainError(
                "CFG_002",
                message="reading_anchor.paper_version_id must belong to this paper.",
                details={"paper_id": paper_id, "paper_version_id": anchor_model.paper_version_id},
            )

    row = session.scalar(select(UiPersonalPaperStateRow).where(
        UiPersonalPaperStateRow.paper_id == paper_id,
        UiPersonalPaperStateRow.owner_key == owner_key,
    ).with_for_update().execution_options(populate_existing=True))
    if row is None:
        if expected_revision is not None and expected_revision != 0:
            # Nothing is stored yet: any other baseline means the caller read a
            # state that has since changed. Refuse instead of creating on top.
            raise UiError(
                "REVISION_CONFLICT",
                message=(
                    "No personal state is stored for this paper (expected revision "
                    f"{expected_revision}); reload before writing."
                ),
                details={"expected_revision": expected_revision, "current_revision": 0},
            )
        row = UiPersonalPaperStateRow(
            owner_key=owner_key,
            paper_id=paper_id,
            saved=bool(saved),
            read_state=read_state or ReadStateValue.UNREAD.value,
            reading_anchor=anchor_model.model_dump(mode="json") if anchor_model else None,
            revision=1,
        )
        session.add(row)
        session.flush()
        return PersonalPatchResult(
            paper_id, row.saved, row.read_state, row.revision, row.reading_anchor
        )

    if expected_revision is not None and expected_revision != row.revision:
        raise UiError(
            "REVISION_CONFLICT",
            message=(
                f"Personal state changed (expected revision {expected_revision}, "
                f"current {row.revision})."
            ),
            details={"expected_revision": expected_revision, "current_revision": row.revision},
        )
    if saved is not None:
        row.saved = saved
    if read_state is not None:
        row.read_state = read_state
    if anchor_model is not None:
        row.reading_anchor = anchor_model.model_dump(mode="json")
    row.revision = row.revision + 1
    row.updated_at = utcnow()
    session.flush()
    return PersonalPatchResult(
        paper_id, row.saved, row.read_state, row.revision, row.reading_anchor
    )


def list_notes(
    session: Session,
    *,
    paper_id: str | None = None,
    paper_version_id: str | None = None,
    owner_key: str = DEFAULT_OWNER,
) -> list[UiNoteRow]:
    stmt = select(UiNoteRow).where(UiNoteRow.owner_key == owner_key)
    if paper_id:
        stmt = stmt.where(UiNoteRow.paper_id == paper_id)
    if paper_version_id:
        stmt = stmt.where(UiNoteRow.paper_version_id == paper_version_id)
    return list(session.scalars(stmt.order_by(UiNoteRow.updated_at.desc(), UiNoteRow.note_id)))


def create_note(
    session: Session,
    *,
    paper_id: str,
    paper_version_id: str,
    body: str,
    claim_id: str | None = None,
    evidence_id: str | None = None,
    owner_key: str = DEFAULT_OWNER,
    paper_wide: bool = False,
) -> UiNoteRow:
    if not body.strip():
        raise DomainError("CFG_002", message="Note body must not be empty.", details={})
    paper = session.scalar(select(PaperRow).where(PaperRow.paper_id == paper_id).with_for_update())
    if paper is None:
        raise DomainError(
            "CFG_002", message=f"Unknown paper ID: {paper_id}", details={"paper_id": paper_id}
        )
    version = session.get(PaperVersionRow, paper_version_id)
    if version is None or version.paper_id != paper_id:
        raise DomainError(
            "CFG_002",
            message="paper_version_id must belong to the note's paper.",
            details={"paper_id": paper_id, "paper_version_id": paper_version_id},
        )
    if paper_wide:
        if claim_id or evidence_id:
            raise DomainError("CFG_002", message="A paper-wide note cannot have a claim/evidence anchor.")
        existing = session.scalar(select(UiNoteRow).where(
            UiNoteRow.paper_id == paper_id, UiNoteRow.paper_version_id == paper_version_id,
            UiNoteRow.owner_key == owner_key, UiNoteRow.claim_id.is_(None),
            UiNoteRow.evidence_id.is_(None),
        ).order_by(UiNoteRow.created_at, UiNoteRow.note_id).with_for_update()
          .execution_options(populate_existing=True))
        if existing is not None:
            if existing.body == body:
                return existing
            raise UiError("REVISION_CONFLICT", message="A paper note already exists; keep and merge your draft.",
                          details={"note_id": existing.note_id, "server_body": existing.body,
                                   "current_revision": existing.revision})
    row = UiNoteRow(
        note_id=new_ui_note_id(),
        owner_key=owner_key,
        paper_id=paper_id,
        paper_version_id=paper_version_id,
        claim_id=claim_id,
        evidence_id=evidence_id,
        body=body,
        revision=1,
    )
    session.add(row)
    session.flush()
    return row


def patch_note(
    session: Session,
    note_id: str,
    *,
    body: str | None = None,
    expected_revision: int | None = None,
    owner_key: str = DEFAULT_OWNER,
) -> UiNoteRow:
    row = session.scalar(select(UiNoteRow).where(UiNoteRow.note_id == note_id)
                         .with_for_update().execution_options(populate_existing=True))
    if row is None or row.owner_key != owner_key:
        raise DomainError(
            "CFG_002", message=f"Unknown note ID: {note_id}", details={"note_id": note_id}
        )
    if expected_revision is not None and expected_revision != row.revision:
        raise UiError(
            "REVISION_CONFLICT",
            message=(
                f"Note changed on the server (expected revision {expected_revision}, "
                f"current {row.revision}); keep your draft and merge."
            ),
            details={
                "expected_revision": expected_revision,
                "current_revision": row.revision,
                "server_body": row.body,
            },
        )
    if body is not None:
        row.body = body
    row.revision = row.revision + 1
    row.updated_at = utcnow()
    session.flush()
    return row


def delete_note(session: Session, note_id: str, *, owner_key: str = DEFAULT_OWNER) -> bool:
    row = session.get(UiNoteRow, note_id)
    if row is None or row.owner_key != owner_key:
        raise DomainError(
            "CFG_002", message=f"Unknown note ID: {note_id}", details={"note_id": note_id}
        )
    session.delete(row)
    session.flush()
    return True


def record_review_decision(
    session: Session,
    *,
    claim_id: str,
    decision: str,
    note: str | None = None,
    idempotency_key: str | None = None,
    expected_claim_revision: str | None = None,
    owner_key: str = DEFAULT_OWNER,
) -> tuple[UiReviewDecisionRow, bool]:
    """Record a HUMAN decision. It never touches support_state.

    Returns (row, created). A repeated idempotency key returns the existing
    decision so a double-click cannot create two records. When
    ``expected_claim_revision`` is supplied it must match the claim's current
    revision: a decision taken on text that has since changed (or that was
    superseded) is refused with a conflict instead of being attached to it.
    """
    from paperintel.database.models import ClaimRow

    if decision not in REVIEW_DECISIONS:
        raise DomainError(
            "CFG_002",
            message=f"Unknown review decision: {decision!r}",
            details={"decision": decision, "allowed": list(REVIEW_DECISIONS)},
        )
    if idempotency_key:
        existing = session.scalar(
            select(UiReviewDecisionRow).where(
                UiReviewDecisionRow.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return existing, False
    claim = session.get(ClaimRow, claim_id)
    if claim is None:
        raise DomainError(
            "CFG_002", message=f"Unknown claim ID: {claim_id}", details={"claim_id": claim_id}
        )
    if expected_claim_revision is not None:
        from paperintel.services.ui.review import claim_revision

        current = claim_revision(claim)
        if expected_claim_revision != current:
            raise UiError(
                "REVISION_CONFLICT",
                message=(
                    "This claim changed since you opened it; reload before recording a decision."
                ),
                details={
                    "expected_claim_revision": expected_claim_revision,
                    "current_revision": current,
                    "claim_id": claim_id,
                },
            )
    row = UiReviewDecisionRow(
        decision_id=new_ui_review_decision_id(),
        owner_key=owner_key,
        claim_id=claim_id,
        paper_version_id=claim.paper_version_id,
        decision=decision,
        note=note,
        idempotency_key=idempotency_key,
    )
    session.add(row)
    session.flush()
    return row, True


def latest_decisions(session: Session, *, owner_key: str = DEFAULT_OWNER) -> dict[str, str]:
    rows = session.scalars(
        select(UiReviewDecisionRow)
        .where(UiReviewDecisionRow.owner_key == owner_key)
        .order_by(UiReviewDecisionRow.created_at)
    )
    latest: dict[str, str] = {}
    for row in rows:
        latest[row.claim_id] = row.decision
    return latest


# ---------------------------------------------------------------------------
# preferences
# ---------------------------------------------------------------------------


def get_preferences(session: Session, *, owner_key: str = DEFAULT_OWNER) -> UiPreferencesRow:
    row = session.get(UiPreferencesRow, owner_key)
    if row is None:
        row = UiPreferencesRow(owner_key=owner_key)
        session.add(row)
        session.flush()
    return row


def patch_preferences(
    session: Session,
    *,
    theme: str | None = None,
    density: str | None = None,
    reduce_motion: bool | None = None,
    single_key_shortcuts: bool | None = None,
    reader_font_px: int | None = None,
    focus_default: bool | None = None,
    expected_revision: int | None = None,
    owner_key: str = DEFAULT_OWNER,
) -> UiPreferencesRow:
    row = get_preferences(session, owner_key=owner_key)
    if expected_revision is not None and expected_revision != row.revision:
        raise UiError(
            "REVISION_CONFLICT",
            message="Preferences changed elsewhere.",
            details={"expected_revision": expected_revision, "current_revision": row.revision},
        )
    if theme is not None:
        if theme not in ("LIGHT", "DARK", "SYSTEM"):
            raise DomainError("CFG_002", message=f"Unknown theme: {theme!r}", details={})
        row.theme = theme
    if density is not None:
        if density not in ("COMFORTABLE", "COMPACT"):
            raise DomainError("CFG_002", message=f"Unknown density: {density!r}", details={})
        row.density = density
    if reduce_motion is not None:
        row.reduce_motion = reduce_motion
    if single_key_shortcuts is not None:
        row.single_key_shortcuts = single_key_shortcuts
    if reader_font_px is not None:
        if not 12 <= reader_font_px <= 28:
            raise DomainError(
                "CFG_002",
                message="reader_font_px must be between 12 and 28.",
                details={"reader_font_px": reader_font_px},
            )
        row.reader_font_px = reader_font_px
    if focus_default is not None:
        row.focus_default = focus_default
    row.revision = row.revision + 1
    row.updated_at = utcnow()
    session.flush()
    return row


__all__ = [
    "DEFAULT_OWNER",
    "REVIEW_DECISIONS",
    "PersonalPatchResult",
    "create_note",
    "delete_note",
    "get_preferences",
    "latest_decisions",
    "list_notes",
    "patch_note",
    "patch_personal_state",
    "patch_preferences",
    "personal_state",
    "record_review_decision",
]

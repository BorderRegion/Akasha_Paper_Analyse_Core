"""ingest.fingerprint — content fingerprint and duplicate detection.

Identity rules (doc 03 §1.1/§1.2):
- an Asset is content-addressed: one row per sha256 (DB UNIQUE); re-importing
  identical bytes REUSES the existing asset row and object;
- a PaperVersion is unique per (paper_id, content_sha256): re-importing the
  identical file returns the existing version — versions never overwrite
  one another and are never duplicated;
- a Paper is the intellectual-work identity: matched by DOI when supplied,
  otherwise by normalized title.

Title normalization is deterministic: NFKC, casefold, punctuation stripped,
whitespace collapsed.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import AssetRow, PaperRow, PaperVersionRow
from paperintel.storage.object_store import sha256_bytes, sha256_file

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    """Deterministic normalized title used for work-identity matching."""
    text = unicodedata.normalize("NFKC", title).casefold()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def fingerprint_bytes(data: bytes) -> str:
    return sha256_bytes(data)


def fingerprint_path(path) -> str:
    return sha256_file(path)


def find_asset_by_sha256(session: Session, sha256: str) -> AssetRow | None:
    return session.scalar(select(AssetRow).where(AssetRow.sha256 == sha256))


def find_version_by_content(session: Session, content_sha256: str) -> PaperVersionRow | None:
    """Any version whose content hash matches — identical bytes are the same
    version of the same work regardless of import path/filename."""
    return session.scalar(
        select(PaperVersionRow).where(PaperVersionRow.content_sha256 == content_sha256)
    )


def find_paper_by_doi(session: Session, doi: str) -> PaperRow | None:
    return session.scalar(select(PaperRow).where(PaperRow.doi == doi))


def find_paper_by_normalized_title(session: Session, normalized_title: str) -> PaperRow | None:
    return session.scalar(select(PaperRow).where(PaperRow.normalized_title == normalized_title))

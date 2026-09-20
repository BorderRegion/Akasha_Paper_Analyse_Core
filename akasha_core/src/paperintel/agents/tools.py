"""agents.tools — paper tools: evidence-scoped context retrieval (P06).

The ONLY retrieval surface agents get. Scope is structural: every tool
takes the declared paper_version_id and never returns evidence outside it
(doc 02 §6 — agents cannot even see other versions' evidence).

Context is delivered as evidence blocks carrying their IDs so agents can
cite them; the firewall later validates every cited ID against the same
scope.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.database.models import EvidenceRow, SectionRow
from paperintel.evidence.retrieval import get_evidence_in_scope
from paperintel.schemas.enums import DataQualityState, EvidenceType, SectionClass

#: Context budget defaults (doc 07 §3 evidence-scoped context policy).
DEFAULT_MAX_UNITS = 60
DEFAULT_MAX_CHARS = 24_000
#: Preview length per evidence text inside the context block.
_EVIDENCE_PREVIEW_CHARS = 900


@dataclass(slots=True)
class EvidenceContext:
    """Structured, scope-locked context handed to an agent."""

    paper_version_id: str
    evidence_ids: list[str]
    blocks: list[str]
    total_available: int
    truncated: bool

    @property
    def rendered(self) -> str:
        if not self.blocks:
            return "(no evidence available for this version)"
        return "\n\n".join(self.blocks)

    def restrict_to(self, evidence_ids: list[str]) -> EvidenceContext:
        """Narrow the context to an explicit evidence ID whitelist (the
        AgentRequest.evidence_scope); unknown IDs are dropped from the
        context (the firewall still validates every ID the model cites)."""
        keep = set(evidence_ids)
        blocks = [b for b in self.blocks if b.split("]", 1)[0][1:] in keep]
        ids = [i for i in self.evidence_ids if i in keep]
        return EvidenceContext(
            paper_version_id=self.paper_version_id,
            evidence_ids=ids,
            blocks=blocks,
            total_available=self.total_available,
            truncated=self.truncated or len(ids) < len(self.evidence_ids),
        )


def build_evidence_context(
    session: Session,
    paper_version_id: str,
    *,
    focus_sections: set[str] | None = None,
    focus_classes: set[SectionClass] | None = None,
    focus_types: set[EvidenceType] | None = None,
    include_quality: set[DataQualityState] | None = None,
    max_units: int = DEFAULT_MAX_UNITS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> EvidenceContext:
    """Build the evidence-scoped context for one paper version.

    Ordering is the stable locator order (page, evidence_id) so the same
    scope always yields the same context (deterministic prompts →
    request_hash stability). Truncation is honest: the flag reports it.

    focus_sections: explicit section IDs (evict everything else);
    focus_classes: canonical SectionClass filter (e.g. METHOD+THEORY for
    the method agent). The title/abstract spine always stays in so the
    agent still knows what the paper is about even when its focus
    sections are missing.
    """
    from paperintel.evidence.retrieval import list_evidence

    rows = list_evidence(
        session,
        paper_version_id,
        section_id=next(iter(focus_sections))
        if focus_sections and len(focus_sections) == 1
        else None,
        evidence_types=focus_types,
        quality_states=include_quality,
    )
    if focus_sections and len(focus_sections) > 1:
        rows = [row for row in rows if row.section_id in focus_sections]

    section_rows = session.scalars(
        select(SectionRow).where(SectionRow.paper_version_id == paper_version_id)
    ).all()
    section_titles = {row.section_id: row.original_heading for row in section_rows}
    if focus_classes:
        matching = {row.section_id for row in section_rows if row.normalized_class in focus_classes}
        focused = [row for row in rows if row.section_id in matching]
        spine = {
            row.section_id
            for row in section_rows
            if row.normalized_class in (SectionClass.TITLE, SectionClass.ABSTRACT)
        }
        focused_ids = {row.evidence_id for row in focused}
        spine_rows = [
            row for row in rows if row.section_id in spine and row.evidence_id not in focused_ids
        ]
        rows = focused + spine_rows

    blocks: list[str] = []
    ids: list[str] = []
    used_chars = 0
    truncated = False
    for row in rows:
        if len(ids) >= max_units or used_chars >= max_chars:
            truncated = True
            break
        preview = (row.text or "")[:_EVIDENCE_PREVIEW_CHARS]
        section = section_titles.get(row.section_id, "?") if row.section_id else "(unsectioned)"
        quality = row.quality_state.value
        block = (
            f"[{row.evidence_id}] type={row.evidence_type.value} page={row.page_start} "
            f"section={section} quality={quality}\n{preview}"
        )
        if used_chars + len(block) > max_chars and ids:
            truncated = True
            break
        blocks.append(block)
        ids.append(row.evidence_id)
        used_chars += len(block)

    return EvidenceContext(
        paper_version_id=paper_version_id,
        evidence_ids=ids,
        blocks=blocks,
        total_available=len(rows),
        truncated=truncated,
    )


def resolve_evidence_ids(
    session: Session,
    paper_version_id: str,
    evidence_ids: list[str],
) -> list[EvidenceRow]:
    """Resolve cited evidence IDs within the declared scope.

    Unknown ID → EVIDENCE_001; evidence of another version → EVIDENCE_002
    (both hard rejections: the claim candidate never persists).
    """
    resolved: list[EvidenceRow] = []
    for evidence_id in evidence_ids:
        resolved.append(get_evidence_in_scope(session, evidence_id, paper_version_id))
    return resolved


def statement_numbers(statement: str) -> set[str]:
    """Normalized numeric literals in a statement (commas stripped so
    '1,000' matches '1000'; percent/decimal forms kept verbatim)."""
    import re

    raw = re.findall(r"\d[\d,._]*\d|\d", statement)
    return {value.replace(",", "").rstrip(".") for value in raw}


def significant_tokens(text: str) -> set[str]:
    """Significant (non-stopword) alphabetic tokens of a text."""
    import re

    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
        if token.lower() not in STOPWORDS
    }


def statement_supported_by_evidence(statement: str, evidence_rows: list[EvidenceRow]) -> bool:
    """Coarse support check for FACT candidates (doc 03 §3 rule 5):

    1. every numeric literal in the statement must appear in the cited
       evidence text (an invented number is never supported);
    2. at least a third of the significant tokens must appear (the claim
       must be about the cited content at all).

    The P07 numeric verifier does the rigorous check; the firewall refuses
    claims whose citation shares no signal or carries an unsupported
    factual number."""
    import re

    evidence_text = " ".join((row.text or "") for row in evidence_rows)
    evidence_flat = evidence_text.replace(",", "").lower()

    for number in statement_numbers(statement):
        if number not in evidence_flat:
            return False

    tokens = {
        token
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", statement)
        if token.lower() not in STOPWORDS
    }
    if not tokens:
        return True
    hits = sum(1 for token in tokens if token.lower() in evidence_text.lower())
    return hits >= max(1, len(tokens) // 3)


STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "have",
        "been",
        "were",
        "which",
        "their",
        "there",
        "than",
        "then",
        "them",
        "into",
        "such",
        "some",
        "more",
        "most",
        "over",
        "under",
        "about",
        "after",
        "before",
        "between",
        "during",
        "through",
        "against",
        "without",
        "within",
        "also",
        "both",
        "each",
        "other",
        "because",
        "while",
        "where",
        "these",
        "those",
        "very",
        "just",
        "only",
        "using",
        "used",
        "based",
    }
)

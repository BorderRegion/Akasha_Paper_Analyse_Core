"""verification.verifiers — the frozen verifier modules (P08, doc 01 §10).

Deterministic, evidence-grounded checks. Every hallucination class from
the doc 05 P08 gate list has a designated detector here:

- invented numerical improvement   → numeric (invented_number)
- claim unsupported by cited text  → citation (unsupported_statement)
- OCR digit corruption             → numeric/ocr_sensitivity (ocr_digit_corruption)
- two agents disagreeing           → independent_consensus (disagreement)
- hypothesis misreported as result → claim_scope (hypothesis_as_result)
- baseline budget mismatch         → claim_scope (baseline_budget_mismatch)
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paperintel.agents.tools import significant_tokens, statement_numbers
from paperintel.database.models import ClaimEvidenceRow, ClaimRow, EvidenceRow
from paperintel.schemas.enums import (
    ClaimType,
    EvidenceRole,
    SectionClass,
    SourceMethod,
    VerifierType,
)
from paperintel.verification.base import VerifierResult, register_verifier

_HYPOTHESIS_RE = re.compile(
    r"\b(hypothes(?:is|ize|es|ized)|we expect|we aim to|we propose to|"
    r"if .* then|we conjecture|it remains to be seen)\b",
    re.IGNORECASE,
)
_COMPARATIVE_RE = re.compile(
    r"\b(outperform\w*|beat\w*|surpass\w*|better than|exceed\w*|"
    r"improv\w* over|superior to|state[- ]of[- ]the[- ]art)\b",
    re.IGNORECASE,
)
_BUDGET_DISPARITY_RE = re.compile(
    r"\b(\d+\s*(?:×|x)\s*(?:more|larger|bigger|compute|budget))\b|"
    r"\b(\d+\s*(?:GPUs?|A100s?|H100s?|V100s?))\b|"
    r"\b(budget mismatch|unequal (?:training )?(?:budget|compute))\b|"
    r"\b(\d+\s*×\s*\d+\s*(?:epochs?|steps?))\b",
    re.IGNORECASE,
)
#: OCR mean-confidence below which digit-bearing evidence is flagged.
OCR_CONFIDENCE_THRESHOLD = 0.9


def _cited_rows(session: Session, claim: ClaimRow) -> list[EvidenceRow]:
    links = session.scalars(
        select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id == claim.claim_id)
    ).all()
    rows = []
    for link in links:
        row = session.get(EvidenceRow, link.evidence_id)
        if row is not None:
            rows.append(row)
    return rows


def _cited_text(rows: list[EvidenceRow]) -> str:
    return " ".join((row.text or "") for row in rows)


def _significant_tokens(statement: str) -> set[str]:
    return significant_tokens(statement)


def _numbers_of(text: str) -> set[str]:
    return statement_numbers(text)


def _edit_distance_one_digit(a: str, b: str) -> bool:
    """Single-digit substitution between two numeric literals
    (same length, exactly one differing character) — the classic OCR
    digit corruption signature."""
    if len(a) != len(b) or not a or not b:
        return False
    return sum(1 for x, y in zip(a, b, strict=True) if x != y) == 1


# ---------------------------------------------------------------------------
# 1. evidence existence
# ---------------------------------------------------------------------------


@register_verifier(VerifierType.EVIDENCE_EXISTENCE)
def verify_evidence_existence(session: Session, claim: ClaimRow) -> VerifierResult:
    """Every linked evidence ID must resolve to a row (doc 05 P08 gate)."""
    links = session.scalars(
        select(ClaimEvidenceRow).where(ClaimEvidenceRow.claim_id == claim.claim_id)
    ).all()
    if not links:
        return VerifierResult(
            VerifierType.EVIDENCE_EXISTENCE,
            "FAIL",
            "claim has no evidence links",
            {"link_count": 0},
        )
    missing = [
        link.evidence_id for link in links if session.get(EvidenceRow, link.evidence_id) is None
    ]
    if missing:
        return VerifierResult(
            VerifierType.EVIDENCE_EXISTENCE,
            "FAIL",
            f"missing evidence rows: {missing}",
            {"missing": missing},
        )
    return VerifierResult(
        VerifierType.EVIDENCE_EXISTENCE,
        "PASS",
        f"all {len(links)} evidence links resolve",
        {"link_count": len(links)},
    )


# ---------------------------------------------------------------------------
# 2. citation support
# ---------------------------------------------------------------------------


@register_verifier(VerifierType.CITATION)
def verify_citation(session: Session, claim: ClaimRow) -> VerifierResult:
    """The claim must be ABOUT its cited paragraphs (doc 05 P08:
    'claim unsupported by cited paragraph')."""
    rows = _cited_rows(session, claim)
    if not rows:
        return VerifierResult(
            VerifierType.CITATION, "FAIL", "no cited evidence to support the claim", {}
        )
    text = _cited_text(rows).lower()
    tokens = _significant_tokens(claim.statement)
    if not tokens:
        return VerifierResult(
            VerifierType.CITATION, "WARN", "statement carries no significant tokens", {}
        )
    hits = sum(1 for token in tokens if token in text)
    ratio = hits / len(tokens)
    if ratio >= 0.5:
        return VerifierResult(
            VerifierType.CITATION,
            "PASS",
            f"statement is grounded in the cited text ({hits}/{len(tokens)} tokens)",
            {"token_hits": hits, "token_total": len(tokens)},
        )
    if ratio >= 0.25:
        return VerifierResult(
            VerifierType.CITATION,
            "WARN",
            f"weak citation support ({hits}/{len(tokens)} tokens)",
            {"token_hits": hits, "token_total": len(tokens)},
        )
    return VerifierResult(
        VerifierType.CITATION,
        "FAIL",
        f"claim unsupported by cited paragraph ({hits}/{len(tokens)} tokens)",
        {"token_hits": hits, "token_total": len(tokens)},
    )


# ---------------------------------------------------------------------------
# 3. numeric consistency (+ OCR digit corruption)
# ---------------------------------------------------------------------------


@register_verifier(VerifierType.NUMERIC)
def verify_numeric(session: Session, claim: ClaimRow) -> VerifierResult:
    """Every numeric literal in the statement must appear in the cited
    evidence. A missing number exactly one digit away from an evidence
    number is OCR digit corruption when the evidence is OCR-derived;
    otherwise it is an invented number (doc 05 P08 gate list)."""
    rows = _cited_rows(session, claim)
    evidence_text = _cited_text(rows)
    evidence_flat = evidence_text.replace(",", "")
    evidence_numbers = _numbers_of(evidence_flat)
    statement_numbers = _numbers_of(claim.statement)

    if not statement_numbers:
        return VerifierResult(
            VerifierType.NUMERIC, "PASS", "statement carries no numeric literals", {}
        )

    ocr_sourced = any(row.source_method is SourceMethod.OCR for row in rows)
    problems: list[dict[str, Any]] = []
    for number in sorted(statement_numbers):
        if number in evidence_flat:
            continue
        near = [
            candidate
            for candidate in evidence_numbers
            if _edit_distance_one_digit(number, candidate)
        ]
        if near and ocr_sourced:
            problems.append({"number": number, "kind": "ocr_digit_corruption", "near": near})
        elif near:
            problems.append({"number": number, "kind": "numeric_mismatch", "near": near})
        else:
            problems.append({"number": number, "kind": "invented_number", "near": []})

    if problems:
        kinds = {problem["kind"] for problem in problems}
        if "ocr_digit_corruption" in kinds:
            reason = "OCR digit corruption detected (number differs by one digit from OCR evidence)"
        elif "numeric_mismatch" in kinds:
            reason = "numeric mismatch with cited evidence"
        else:
            reason = "invented numerical value (not present in cited evidence)"
        return VerifierResult(
            VerifierType.NUMERIC,
            "FAIL",
            reason,
            {"problems": problems, "ocr_sourced": ocr_sourced},
        )
    return VerifierResult(
        VerifierType.NUMERIC,
        "PASS",
        f"all {len(statement_numbers)} numeric literals appear in cited evidence",
        {"numbers": sorted(statement_numbers)},
    )


# ---------------------------------------------------------------------------
# 4. claim scope (hypothesis-as-result + baseline budget mismatch)
# ---------------------------------------------------------------------------


def _section_classes(session: Session, rows: list[EvidenceRow]) -> set[str]:
    from paperintel.database.models import SectionRow

    classes: set[str] = set()
    for row in rows:
        if not row.section_id:
            continue
        section = session.get(SectionRow, row.section_id)
        if section is not None:
            classes.add(section.normalized_class.value)
    return classes


@register_verifier(VerifierType.CLAIM_SCOPE)
def verify_claim_scope(session: Session, claim: ClaimRow) -> VerifierResult:
    """Scope discipline: result claims must rest on result/experiment
    evidence (a hypothesis from the introduction is not a result);
    comparative claims must not rest on evidence showing unequal budgets
    (doc 05 P08: hypothesis misreported as result; baseline budget
    mismatch)."""
    rows = _cited_rows(session, claim)
    evidence_text = _cited_text(rows)
    classes = _section_classes(session, rows)

    if claim.category.startswith("result.") and claim.claim_type is ClaimType.FACT:
        result_classes = {SectionClass.RESULT.value, SectionClass.EXPERIMENT.value}
        if not classes & result_classes:
            if _HYPOTHESIS_RE.search(evidence_text):
                return VerifierResult(
                    VerifierType.CLAIM_SCOPE,
                    "FAIL",
                    "hypothesis misreported as result (hypothesis-phrased "
                    "evidence outside result/experiment sections)",
                    {"section_classes": sorted(classes)},
                )
            return VerifierResult(
                VerifierType.CLAIM_SCOPE,
                "WARN",
                "result claim cites no result/experiment evidence",
                {"section_classes": sorted(classes)},
            )

    if _COMPARATIVE_RE.search(claim.statement) and _BUDGET_DISPARITY_RE.search(evidence_text):
        return VerifierResult(
            VerifierType.CLAIM_SCOPE,
            "FAIL",
            "baseline budget mismatch: comparative claim rests on evidence "
            "with unequal training/compute budgets",
            {},
        )

    return VerifierResult(
        VerifierType.CLAIM_SCOPE,
        "PASS",
        "claim scope consistent with cited evidence",
        {"section_classes": sorted(classes)},
    )


# ---------------------------------------------------------------------------
# 5. contradiction (within the version)
# ---------------------------------------------------------------------------


def _claims_with_numbers(session: Session, version_id: str) -> list[ClaimRow]:
    rows = session.scalars(select(ClaimRow).where(ClaimRow.paper_version_id == version_id)).all()
    return [row for row in rows if _numbers_of(row.statement)]


@register_verifier(VerifierType.CONTRADICTION)
def verify_contradiction(session: Session, claim: ClaimRow) -> VerifierResult:
    """Two claims about the same subject with DIFFERENT numbers contradict
    each other (both get FAIL — the disagreement is mutual)."""
    tokens = _significant_tokens(claim.statement)
    numbers = _numbers_of(claim.statement)
    if not numbers or not tokens:
        return VerifierResult(
            VerifierType.CONTRADICTION,
            "PASS",
            "no assertable numeric subject to contradict",
            {},
        )
    for other in _claims_with_numbers(session, claim.paper_version_id):
        if other.claim_id == claim.claim_id:
            continue
        other_tokens = _significant_tokens(other.statement)
        overlap = tokens & other_tokens
        if not overlap or len(overlap) < max(2, len(tokens) // 2):
            continue
        other_numbers = _numbers_of(other.statement)
        if other_numbers and other_numbers != numbers:
            return VerifierResult(
                VerifierType.CONTRADICTION,
                "FAIL",
                f"contradicts claim {other.claim_id} "
                f"(same subject, different numbers: {sorted(numbers)} vs "
                f"{sorted(other_numbers)})",
                {
                    "conflicting_claim_id": other.claim_id,
                    "claim_numbers": sorted(numbers),
                    "other_numbers": sorted(other_numbers),
                },
            )
    return VerifierResult(
        VerifierType.CONTRADICTION, "PASS", "no contradicting claim in this version", {}
    )


# ---------------------------------------------------------------------------
# 6. independent consensus
# ---------------------------------------------------------------------------


def _independent_claims(session: Session, claim: ClaimRow) -> list[ClaimRow]:
    """Claims about the same subject produced by OTHER agent runs
    (independent generation, doc 01 §5)."""
    others = session.scalars(
        select(ClaimRow).where(
            ClaimRow.paper_version_id == claim.paper_version_id,
            ClaimRow.created_by_run_id != claim.created_by_run_id,
        )
    ).all()
    tokens = _significant_tokens(claim.statement)
    result = []
    for other in others:
        other_tokens = _significant_tokens(other.statement)
        overlap = tokens & other_tokens
        if overlap and len(overlap) >= max(2, len(tokens) // 2):
            result.append(other)
    return result


@register_verifier(VerifierType.INDEPENDENT_CONSENSUS)
def verify_independent_consensus(session: Session, claim: ClaimRow) -> VerifierResult:
    """Independent agents must agree: same subject + different numbers from
    a different run → disagreement (both DISPUTED); agreement → consensus
    PASS (doc 05 P08: two agents disagreeing)."""
    numbers = _numbers_of(claim.statement)
    independent = _independent_claims(session, claim)
    if not independent:
        return VerifierResult(
            VerifierType.INDEPENDENT_CONSENSUS,
            "INCONCLUSIVE",
            "no independent agent claim on this subject",
            {"independent_count": 0},
        )
    for other in independent:
        other_numbers = _numbers_of(other.statement)
        if numbers and other_numbers and other_numbers != numbers:
            return VerifierResult(
                VerifierType.INDEPENDENT_CONSENSUS,
                "FAIL",
                f"independent agents disagree ({sorted(numbers)} vs "
                f"{sorted(other_numbers)}; run {other.created_by_run_id})",
                {
                    "disagreeing_claim_id": other.claim_id,
                    "claim_numbers": sorted(numbers),
                    "other_numbers": sorted(other_numbers),
                },
            )
    return VerifierResult(
        VerifierType.INDEPENDENT_CONSENSUS,
        "PASS",
        f"independent agents agree ({len(independent)} other-run claim(s))",
        {"independent_count": len(independent)},
    )


# ---------------------------------------------------------------------------
# 7. falsification
# ---------------------------------------------------------------------------


@register_verifier(VerifierType.FALSIFICATION)
def verify_falsification(session: Session, claim: ClaimRow) -> VerifierResult:
    """A supported CRITIQUE citing counter-evidence against this claim's
    subject weakens it (falsification pressure → WARN)."""
    tokens = _significant_tokens(claim.statement)
    if not tokens:
        return VerifierResult(VerifierType.FALSIFICATION, "PASS", "no falsifiable subject", {})
    critiques = session.scalars(
        select(ClaimRow).where(
            ClaimRow.paper_version_id == claim.paper_version_id,
            ClaimRow.claim_type == ClaimType.CRITIQUE,
        )
    ).all()
    for critique in critiques:
        critique_tokens = _significant_tokens(critique.statement)
        overlap = tokens & critique_tokens
        if not overlap or len(overlap) < max(2, len(tokens) // 2):
            continue
        counter_links = session.scalars(
            select(ClaimEvidenceRow).where(
                ClaimEvidenceRow.claim_id == critique.claim_id,
                ClaimEvidenceRow.role == EvidenceRole.COUNTER_EVIDENCE,
            )
        ).all()
        if counter_links:
            return VerifierResult(
                VerifierType.FALSIFICATION,
                "WARN",
                f"falsification pressure from critique {critique.claim_id} citing counter-evidence",
                {"critique_claim_id": critique.claim_id},
            )
    return VerifierResult(
        VerifierType.FALSIFICATION, "PASS", "no supported falsification found", {}
    )


# ---------------------------------------------------------------------------
# 8. OCR sensitivity
# ---------------------------------------------------------------------------


@register_verifier(VerifierType.OCR_SENSITIVITY)
def verify_ocr_sensitivity(session: Session, claim: ClaimRow) -> VerifierResult:
    """Digit-bearing claims resting on low-confidence OCR evidence are
    fragile: WARN so the confidence components can carry it (doc 03 §13)."""
    rows = _cited_rows(session, claim)
    ocr_rows = [row for row in rows if row.source_method is SourceMethod.OCR]
    if not ocr_rows:
        return VerifierResult(
            VerifierType.OCR_SENSITIVITY,
            "PASS",
            "claim does not rest on OCR-derived evidence",
            {},
        )
    low = [
        row.evidence_id
        for row in ocr_rows
        if row.ocr_confidence is None or row.ocr_confidence < OCR_CONFIDENCE_THRESHOLD
    ]
    if low and _numbers_of(claim.statement):
        return VerifierResult(
            VerifierType.OCR_SENSITIVITY,
            "WARN",
            f"numeric claim rests on low-confidence OCR evidence: {low}",
            {"low_confidence_evidence": low, "threshold": OCR_CONFIDENCE_THRESHOLD},
        )
    return VerifierResult(
        VerifierType.OCR_SENSITIVITY,
        "PASS",
        "OCR evidence confidence is adequate",
        {"ocr_rows": len(ocr_rows)},
    )


# ---------------------------------------------------------------------------
# 9. external novelty (requires an external source)
# ---------------------------------------------------------------------------


@register_verifier(VerifierType.EXTERNAL_NOVELTY)
def verify_external_novelty(session: Session, claim: ClaimRow) -> VerifierResult:
    from paperintel.verification.novelty import audit_novelty

    return audit_novelty(session, claim)

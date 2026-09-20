"""P08 hallucination fixtures: every failure class from the doc 05 P08
gate list MUST be detected (the gate must demonstrate detection).

Fixtures are built from real persisted evidence rows with controlled
properties (section class, source method, OCR confidence, numbers) so the
detectors are exercised against the same shapes production sees.
"""

from __future__ import annotations

import pytest
from tests.integration.p08.fixtures import ensure_run, make_claim, make_evidence

from paperintel.schemas.enums import (
    ClaimType,
    EvidenceRole,
    ResourceTier,
    SectionClass,
    SourceMethod,
    SupportState,
    VerifierType,
)
from paperintel.verification.base import run_verifier
from paperintel.verification.service import run_verification

# ---------------------------------------------------------------------------
# 1. invented numerical improvement
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_invented_numerical_improvement_detected(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 97.3% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    result = run_verifier(VerifierType.NUMERIC, session, claim)
    assert result.verdict == "FAIL"
    assert result.details["problems"][0]["kind"] == "invented_number"
    assert "97.3" in result.details["problems"][0]["number"]


# ---------------------------------------------------------------------------
# 2. claim unsupported by cited paragraph
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_claim_unsupported_by_cited_paragraph_detected(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="The dataset contains 1,000 samples collected from five domains.",
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement=(
            "The transformer architecture eliminates catastrophic forgetting "
            "in continual learning settings."
        ),
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    result = run_verifier(VerifierType.CITATION, session, claim)
    assert result.verdict == "FAIL"
    assert "unsupported by cited paragraph" in result.reason_summary
    assert result.details["token_hits"] < result.details["token_total"] / 4


# ---------------------------------------------------------------------------
# 3. OCR digit corruption
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_ocr_digit_corruption_detected(session, imported) -> None:
    """The OCR'd evidence reads 82.5%; the claim says 82.6% — a classic
    single-digit OCR corruption, distinguishable from invention."""
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
        source_method=SourceMethod.OCR,
        ocr_confidence=0.72,
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.6% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    result = run_verifier(VerifierType.NUMERIC, session, claim)
    assert result.verdict == "FAIL"
    assert result.details["problems"][0]["kind"] == "ocr_digit_corruption"
    assert result.details["ocr_sourced"] is True

    # The OCR sensitivity verifier additionally warns on low confidence.
    sensitivity = run_verifier(VerifierType.OCR_SENSITIVITY, session, claim)
    assert sensitivity.verdict == "WARN"


@pytest.mark.needs_db
def test_invented_number_on_native_evidence_is_not_labelled_ocr(session, imported) -> None:
    """A number one digit away from NATIVE evidence is a mismatch, not OCR
    corruption — mislabelling would hide a real hallucination."""
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
        source_method=SourceMethod.PDF_NATIVE,
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.6% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    result = run_verifier(VerifierType.NUMERIC, session, claim)
    assert result.verdict == "FAIL"
    assert result.details["problems"][0]["kind"] == "numeric_mismatch"
    assert result.details["ocr_sourced"] is False


# ---------------------------------------------------------------------------
# 4. two agents disagreeing
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_two_agents_disagreeing_detected(session, imported) -> None:
    """Independent runs asserting different values for the same subject are
    detected as disagreement and end DISPUTED."""
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
    )
    run_a = ensure_run(session, imported.paper_version_id)
    run_b = ensure_run(session, imported.paper_version_id)
    claim_a = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.5% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        run_id=run_a,
    )
    claim_b = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 85.2% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        run_id=run_b,
    )

    result_a = run_verifier(VerifierType.INDEPENDENT_CONSENSUS, session, claim_a)
    assert result_a.verdict == "FAIL"
    assert result_a.details["other_numbers"] == ["85.2"]

    report = run_verification(
        session,
        paper_version_id=imported.paper_version_id,
        tier=ResourceTier.T3_DEEP,
    )
    session.refresh(claim_a)
    session.refresh(claim_b)
    assert claim_a.support_state is SupportState.DISPUTED
    assert claim_b.support_state is SupportState.DISPUTED
    assert report.states.get("DISPUTED", 0) >= 2


# ---------------------------------------------------------------------------
# 5. hypothesis misreported as result
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_hypothesis_misreported_as_result_detected(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text=(
            "We hypothesize that Method A will outperform the baseline. "
            "This remains to be tested in our experiments."
        ),
        section_class=SectionClass.INTRODUCTION,
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A outperforms the baseline.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        category="result.main",
    )
    result = run_verifier(VerifierType.CLAIM_SCOPE, session, claim)
    assert result.verdict == "FAIL"
    assert "hypothesis misreported as result" in result.reason_summary
    assert result.details["section_classes"] == ["INTRODUCTION"]


@pytest.mark.needs_db
def test_result_claim_on_result_section_passes_scope(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
        section_class=SectionClass.RESULT,
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.5% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        category="result.main",
    )
    result = run_verifier(VerifierType.CLAIM_SCOPE, session, claim)
    assert result.verdict == "PASS"


# ---------------------------------------------------------------------------
# 6. baseline budget mismatch
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_baseline_budget_mismatch_detected(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text=(
            "Our method was trained for 4x more compute than the baseline, "
            "using 8 GPUs against the baseline's 2 GPUs."
        ),
        section_class=SectionClass.EXPERIMENT,
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Our method outperforms the baseline on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        category="result.superiority",
        claim_type=ClaimType.FACT,
    )
    result = run_verifier(VerifierType.CLAIM_SCOPE, session, claim)
    assert result.verdict == "FAIL"
    assert "baseline budget mismatch" in result.reason_summary


# ---------------------------------------------------------------------------
# falsification pressure + clean claims
# ---------------------------------------------------------------------------


@pytest.mark.needs_db
def test_falsification_pressure_weakens_claim(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
    )
    counter = make_evidence(
        session,
        imported.paper_version_id,
        text="Under identical budgets Method A reaches only 71.0% accuracy.",
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.5% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    make_claim(
        session,
        imported.paper_version_id,
        statement="Method A accuracy on the benchmark is overstated.",
        evidence_rows=[(counter, EvidenceRole.COUNTER_EVIDENCE)],
        category="critique.efficiency",
        claim_type=ClaimType.CRITIQUE,
        run_id=ensure_run(session, imported.paper_version_id),
    )
    result = run_verifier(VerifierType.FALSIFICATION, session, claim)
    assert result.verdict == "WARN"
    assert "falsification pressure" in result.reason_summary


@pytest.mark.needs_db
def test_clean_claim_reaches_supported(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="Method A reaches 82.5% accuracy on the benchmark.",
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="Method A reaches 82.5% accuracy on the benchmark.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    report = run_verification(
        session,
        paper_version_id=imported.paper_version_id,
        tier=ResourceTier.T3_DEEP,
    )
    session.refresh(claim)
    assert claim.support_state is SupportState.SUPPORTED
    assert report.states.get("SUPPORTED", 0) >= 1

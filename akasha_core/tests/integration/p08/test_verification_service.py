"""P08 verification service + audit bundle tests: tier gating, append-only
verification records, support-state transitions, provenance chain."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from tests.integration.p08.fixtures import make_claim, make_evidence

from paperintel.database.models import VerificationRow
from paperintel.schemas.enums import (
    ClaimType,
    EvidenceRole,
    ResourceTier,
    SupportState,
    VerifierType,
)
from paperintel.verification.audit import (
    build_claim_audit_bundle,
    build_version_audit_bundle,
)
from paperintel.verification.service import (
    latest_verdicts,
    run_verification,
    verifiers_for_tier,
)
from paperintel.verification.support import support_state_for


@pytest.mark.needs_db
def test_tier_gating_runs_only_applicable_verifiers(session, imported) -> None:
    """Doc 01 §10: not every verifier runs at every tier; T3 runs the
    complete applicable set."""
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

    assert verifiers_for_tier(ResourceTier.T1_SCAN) == (VerifierType.EVIDENCE_EXISTENCE,)
    assert len(verifiers_for_tier(ResourceTier.T3_DEEP)) == 9

    run_verification(session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T1_SCAN)
    first_pass = session.scalars(
        select(VerificationRow).where(VerificationRow.claim_id == claim.claim_id)
    ).all()
    assert {row.verifier_type for row in first_pass} == {VerifierType.EVIDENCE_EXISTENCE}

    run_verification(session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T3_DEEP)
    all_rows = session.scalars(
        select(VerificationRow).where(VerificationRow.claim_id == claim.claim_id)
    ).all()
    # Append-only: the T1 row is still there, plus the full T3 set.
    assert len(all_rows) == 1 + 9
    assert len({row.verifier_type for row in all_rows}) == 9


@pytest.mark.needs_db
def test_verification_pass_is_audited_with_its_own_run(session, imported) -> None:
    evidence = make_evidence(
        session,
        imported.paper_version_id,
        text="The dataset contains 1,000 samples.",
    )
    make_claim(
        session,
        imported.paper_version_id,
        statement="The dataset contains 1,000 samples.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        category="experiment.dataset",
    )
    report = run_verification(
        session,
        paper_version_id=imported.paper_version_id,
        tier=ResourceTier.T2_FULL,
    )
    assert report.run_id.startswith("run_")
    assert report.effective_tier == "T2_FULL"
    assert report.claims_verified == 1
    assert report.states == {"SUPPORTED": 1}
    assert report.transitions == 1  # UNVERIFIED → SUPPORTED

    rows = session.scalars(
        select(VerificationRow).where(VerificationRow.created_by_run_id == report.run_id)
    ).all()
    assert len(rows) == len(verifiers_for_tier(ResourceTier.T2_FULL))
    assert all(row.status == "COMPLETED" for row in rows)


@pytest.mark.needs_db
def test_support_state_transition_policy() -> None:
    """The transition table is explicit and total (doc 03 §1.5)."""
    # all PASS → SUPPORTED
    assert (
        support_state_for({VerifierType.CITATION: "PASS", VerifierType.NUMERIC: "PASS"})
        is SupportState.SUPPORTED
    )
    # WARN only → PARTIALLY_SUPPORTED
    assert (
        support_state_for({VerifierType.OCR_SENSITIVITY: "WARN"})
        is SupportState.PARTIALLY_SUPPORTED
    )
    # evidence failure → UNSUPPORTED
    assert support_state_for({VerifierType.CITATION: "FAIL"}) is SupportState.UNSUPPORTED
    # disagreement → DISPUTED (wins over UNSUPPORTED semantics)
    assert (
        support_state_for(
            {
                VerifierType.CONTRADICTION: "FAIL",
                VerifierType.CITATION: "FAIL",
            }
        )
        is SupportState.DISPUTED
    )
    # only inconclusive → INSUFFICIENT_EVIDENCE
    assert (
        support_state_for({VerifierType.EXTERNAL_NOVELTY: "INCONCLUSIVE"})
        is SupportState.INSUFFICIENT_EVIDENCE
    )
    # nothing ran → unchanged
    assert support_state_for({}) is SupportState.UNVERIFIED
    # RETRACTED is never produced automatically
    assert all(
        support_state_for({vt: verdict}) is not SupportState.RETRACTED
        for vt in VerifierType
        for verdict in ("PASS", "WARN", "FAIL", "INCONCLUSIVE")
    )


@pytest.mark.needs_db
def test_unverified_claim_stays_unverified_until_verified(session, imported) -> None:

    evidence = make_evidence(
        session, imported.paper_version_id, text="The dataset contains 1,000 samples."
    )
    claim = make_claim(
        session,
        imported.paper_version_id,
        statement="The dataset contains 1,000 samples.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
    )
    assert claim.support_state is SupportState.UNVERIFIED
    assert latest_verdicts(session, claim.claim_id) == {}


@pytest.mark.needs_db
def test_audit_bundle_carries_full_provenance(session, imported) -> None:
    """Doc 00 §7.16: which model call / prompt / model version created a
    claim, what evidence it cites, and what verifiers concluded."""
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
    run_verification(
        session,
        paper_version_id=imported.paper_version_id,
        tier=ResourceTier.T2_FULL,
        claim_ids=[claim.claim_id],
    )

    bundle = build_claim_audit_bundle(session, claim.claim_id)
    assert bundle.claim_id == claim.claim_id
    assert bundle.created_by_run is not None
    assert bundle.created_by_run["pipeline_version"]
    assert bundle.evidence and bundle.evidence[0]["content_sha256"]
    assert bundle.evidence[0]["text_excerpt"].startswith("Method A reaches")
    assert bundle.evidence[0]["source_method"] == "PDF_NATIVE"
    assert bundle.verifications
    verdicts = {v["verifier_type"]: v["verdict"] for v in bundle.verifications}
    assert verdicts[VerifierType.CITATION.value] == "PASS"
    assert bundle.complete is True


@pytest.mark.needs_db
def test_dangling_evidence_links_are_impossible_by_schema(session, imported) -> None:
    """The claim_evidence FK refuses links to nonexistent evidence — the
    database itself protects the provenance invariant (a dangling link can
    never exist to be audited)."""
    from sqlalchemy.exc import IntegrityError
    from tests.integration.p08.fixtures import ensure_run

    from paperintel.database.models import ClaimEvidenceRow, ClaimRow
    from paperintel.ids import new_claim_id

    claim = ClaimRow(
        claim_id=new_claim_id(),
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        claim_type=ClaimType.FACT,
        category="result.main",
        statement="A claim whose evidence vanished.",
        support_state=SupportState.UNVERIFIED,
        created_by_run_id=ensure_run(session, imported.paper_version_id),
        pipeline_version="1.0.0",
    )
    session.add(claim)
    session.flush()

    with pytest.raises(IntegrityError):
        session.add(
            ClaimEvidenceRow(
                claim_id=claim.claim_id,
                evidence_id="ev_missing00000000000000000000",
                role=EvidenceRole.SUPPORT,
            )
        )
        session.flush()
    session.rollback()


@pytest.mark.needs_db
def test_audit_bundle_flags_evidence_free_claim(session, imported) -> None:
    """A claim with no evidence links (impossible through the ledger, but
    reachable via direct writes/legacy data) must be VISIBLE as an
    incomplete bundle, never silently reported as fine."""
    from tests.integration.p08.fixtures import ensure_run

    from paperintel.database.models import ClaimRow
    from paperintel.ids import new_claim_id

    claim = ClaimRow(
        claim_id=new_claim_id(),
        paper_id=imported.paper_id,
        paper_version_id=imported.paper_version_id,
        claim_type=ClaimType.FACT,
        category="result.main",
        statement="An evidence-free claim.",
        support_state=SupportState.UNVERIFIED,
        created_by_run_id=ensure_run(session, imported.paper_version_id),
        pipeline_version="1.0.0",
    )
    session.add(claim)
    session.flush()

    bundle = build_claim_audit_bundle(session, claim.claim_id)
    assert any("no evidence links" in warning for warning in bundle.warnings)
    assert bundle.complete is False
    assert bundle.evidence == []


@pytest.mark.needs_db
def test_version_audit_overview(session, imported) -> None:
    evidence = make_evidence(
        session, imported.paper_version_id, text="The dataset contains 1,000 samples."
    )
    make_claim(
        session,
        imported.paper_version_id,
        statement="The dataset contains 1,000 samples.",
        evidence_rows=[(evidence, EvidenceRole.SUPPORT)],
        category="experiment.dataset",
    )
    run_verification(session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T2_FULL)
    overview = build_version_audit_bundle(session, imported.paper_version_id)
    assert overview["claim_count"] == 1
    assert overview["support_states"] == {"SUPPORTED": 1}
    assert overview["unverified_claims"] == []
    assert overview["incomplete_bundles"] == []
    assert overview["verification_total"] == len(verifiers_for_tier(ResourceTier.T2_FULL))


@pytest.mark.needs_db
def test_verification_of_version_without_claims_is_recorded(session, imported) -> None:
    """No claims is a legitimate outcome; the pass is still recorded so the
    stage is explainable."""
    report = run_verification(
        session, paper_version_id=imported.paper_version_id, tier=ResourceTier.T2_FULL
    )
    assert report.claims_verified == 0
    assert report.states == {}
    assert (
        session.scalar(
            select(func.count())
            .select_from(VerificationRow)
            .where(VerificationRow.created_by_run_id == report.run_id)
        )
        == 0
    )

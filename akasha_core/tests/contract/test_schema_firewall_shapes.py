"""Schema firewall shape tests (spec doc 06 §4, P00 layer).

P00 covers the Pydantic layer of the firewall: valid payloads accepted;
missing/unknown/invalid fields rejected; impossible geometry rejected;
unsupported FACT shapes rejected. Evidence existence/scope checks run in P06
against the database.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from paperintel.ids import (
    new_claim_id,
    new_evidence_id,
    new_job_id,
    new_paper_id,
    new_paper_version_id,
    new_run_id,
    new_task_id,
)
from paperintel.schemas.claims import CandidateEvidenceRef, Claim, ClaimCandidate
from paperintel.schemas.common import BBox, ExternalProvenance
from paperintel.schemas.evidence import Evidence
from paperintel.schemas.execution import TaskContract

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_valid_claim_candidate_accepted() -> None:
    candidate = ClaimCandidate.model_validate(
        {
            "claim_type": "INFERENCE",
            "category": "experiment_reliability",
            "statement": "The main comparison may be confounded by unequal training budget.",
            "evidence": [{"evidence_id": "ev_x1", "role": "SUPPORT"}],
            "uncertainties": ["Training compute for one baseline is not fully disclosed."],
        }
    )
    assert candidate.claim_type.value == "INFERENCE"


def test_claim_candidate_empty_statement_rejected() -> None:
    with pytest.raises(ValidationError):
        ClaimCandidate.model_validate(
            {
                "claim_type": "INFERENCE",
                "category": "x",
                "statement": "",
                "evidence": [],
                "uncertainties": [],
            }
        )


def test_fact_without_support_evidence_rejected_at_shape_layer() -> None:
    """Spec doc 03 §3 rule 4: FACT must have direct supporting evidence."""
    with pytest.raises(ValidationError, match="FACT"):
        ClaimCandidate.model_validate(
            {
                "claim_type": "FACT",
                "category": "dataset_size",
                "statement": "The dataset contains 1,000 samples.",
                "evidence": [],
                "uncertainties": [],
            }
        )
    with pytest.raises(ValidationError):
        ClaimCandidate.model_validate(
            {
                "claim_type": "FACT",
                "category": "dataset_size",
                "statement": "The dataset contains 1,000 samples.",
                "evidence": [{"evidence_id": "ev_x1", "role": "CONTEXT"}],
                "uncertainties": [],
            }
        )


def test_invalid_enum_rejected() -> None:
    with pytest.raises(ValidationError):
        CandidateEvidenceRef.model_validate({"evidence_id": "ev_x1", "role": "MAYBE"})
    with pytest.raises(ValidationError):
        ClaimCandidate.model_validate(
            {
                "claim_type": "OPINION",
                "category": "x",
                "statement": "y",
                "evidence": [],
                "uncertainties": [],
            }
        )


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        ClaimCandidate.model_validate(
            {
                "claim_type": "INFERENCE",
                "category": "x",
                "statement": "y",
                "evidence": [],
                "uncertainties": [],
                "totally_new_critical_field": "surprise",
            }
        )


def test_evidence_reference_requires_ev_prefix() -> None:
    with pytest.raises(ValidationError):
        CandidateEvidenceRef.model_validate({"evidence_id": "clm_x1", "role": "SUPPORT"})
    with pytest.raises(ValidationError):
        CandidateEvidenceRef.model_validate({"evidence_id": "", "role": "SUPPORT"})


def test_impossible_bbox_rejected() -> None:
    with pytest.raises(ValidationError):
        BBox.model_validate({"x0": 100, "y0": 10, "x1": 50, "y1": 20})
    with pytest.raises(ValidationError):
        BBox.model_validate({"x0": -1, "y0": 0, "x1": 10, "y1": 10})
    valid = BBox.model_validate({"x0": 10, "y0": 10, "x1": 50, "y1": 20})
    assert valid.as_tuple() == (10.0, 10.0, 50.0, 20.0)


def test_evidence_requires_content_and_valid_pages() -> None:
    base = {
        "evidence_id": new_evidence_id(),
        "paper_version_id": new_paper_version_id(),
        "evidence_type": "PARAGRAPH",
        "page_start": 3,
        "page_end": 3,
        "source_method": "PDF_NATIVE",
        "quality_state": "GOOD",
        "content_sha256": "a" * 64,
        "extraction_run_id": new_run_id(),
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(ValidationError):
        Evidence.model_validate(base)  # neither text nor asset
    with pytest.raises(ValidationError):
        Evidence.model_validate({**base, "text": "x", "page_start": 5, "page_end": 3})
    with pytest.raises(ValidationError):
        Evidence.model_validate({**base, "text": "x", "page_start": 0})
    with pytest.raises(ValidationError):
        # OCR evidence must carry ocr_confidence (spec doc 01 §7).
        Evidence.model_validate({**base, "text": "x", "source_method": "OCR"})
    ok = Evidence.model_validate({**base, "text": "extracted paragraph"})
    assert ok.bbox is None


def test_native_claim_requires_external_provenance() -> None:
    with pytest.raises(ValidationError, match="external_provenance"):
        Claim.model_validate(
            {
                "claim_id": new_claim_id(),
                "paper_id": new_paper_id(),
                "paper_version_id": new_paper_version_id(),
                "claim_type": "EXTERNAL",
                "category": "novelty",
                "statement": "First method to do X.",
                "support_state": "UNVERIFIED",
                "created_by_run_id": new_run_id(),
                "pipeline_version": "1.0.0",
                "created_at": NOW.isoformat(),
            }
        )
    ok = Claim.model_validate(
        {
            "claim_id": new_claim_id(),
            "paper_id": new_paper_id(),
            "paper_version_id": new_paper_version_id(),
            "claim_type": "EXTERNAL",
            "category": "novelty",
            "statement": "First method to do X.",
            "support_state": "UNVERIFIED",
            "created_by_run_id": new_run_id(),
            "pipeline_version": "1.0.0",
            "created_at": NOW.isoformat(),
            "external_provenance": ExternalProvenance(
                source_provider="openalex",
                source_identifier="W123",
                retrieved_at=NOW,
            ).model_dump(mode="json"),
        }
    )
    assert ok.external_provenance is not None


def test_confidence_bounds_enforced() -> None:
    payload = {
        "claim_id": new_claim_id(),
        "paper_id": new_paper_id(),
        "paper_version_id": new_paper_version_id(),
        "claim_type": "INFERENCE",
        "category": "x",
        "statement": "y",
        "support_state": "UNVERIFIED",
        "analysis_confidence": 1.5,
        "created_by_run_id": new_run_id(),
        "pipeline_version": "1.0.0",
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(ValidationError):
        Claim.model_validate(payload)


def test_task_contract_retries_bounded() -> None:
    base = {
        "task_id": new_task_id(),
        "job_id": new_job_id(),
        "task_type": "extraction.native",
        "module_id": "extraction.pdf",
        "state": "PENDING",
        "priority": 0,
        "idempotency_key": "extraction.native|pver|1.0.0|cfg|scope",
        "attempt": 4,
        "max_attempts": 3,
        "created_at": NOW.isoformat(),
    }
    with pytest.raises(ValidationError, match="bounded"):
        TaskContract.model_validate(base)
    base["attempt"] = 2
    assert TaskContract.model_validate(base).attempt == 2


def test_frozen_models_reject_mutation() -> None:
    candidate = ClaimCandidate.model_validate(
        {
            "claim_type": "INFERENCE",
            "category": "x",
            "statement": "y",
            "evidence": [],
            "uncertainties": [],
        }
    )
    with pytest.raises(ValidationError):
        candidate.statement = "mutated"  # type: ignore[misc]

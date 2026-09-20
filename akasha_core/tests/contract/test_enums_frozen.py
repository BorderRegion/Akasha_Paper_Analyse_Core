"""Frozen enum conformance (P00 gate check P00-C13).

Each frozen list is transcribed from the specification text; any drift between
spec and code fails here.
"""

from __future__ import annotations

from paperintel.schemas.enums import (
    PIPELINE_STAGE_ORDER,
    TAG_NAMESPACES,
    TIER_ORDER,
    AgentStatus,
    ClaimType,
    DataQualityState,
    EntityType,
    EvidenceRole,
    EvidenceType,
    LlmMockMode,
    ModuleHealthState,
    OcrMockMode,
    PageMode,
    PipelineStage,
    ProviderFamily,
    QualityDimension,
    ResourceTier,
    RetentionClass,
    SectionClass,
    SupportState,
    SynthesisSupportClass,
    TaskState,
    VerifierType,
    VerifierVerdict,
)


def _values(enum) -> list[str]:
    return [member.value for member in enum]


def test_pipeline_stages_exact_sequence() -> None:
    """Spec doc 02 §7 / doc 03 §8 (order is semantic)."""
    assert _values(PipelineStage) == [
        "IMPORTED",
        "FINGERPRINTED",
        "METADATA_RESOLVED",
        "PDF_INSPECTED",
        "EXTRACTED",
        "STRUCTURED",
        "EVIDENCE_INDEXED",
        "TRIAGED",
        "ANALYZED",
        "VERIFIED",
        "SYNTHESIZED",
        "LINKED",
        "SEARCH_INDEXED",
        "CORPUS_READY",
    ]
    assert list(PIPELINE_STAGE_ORDER) == list(PipelineStage)


def test_task_states_exact_set() -> None:
    """Spec doc 02 §8."""
    assert _values(TaskState) == [
        "PENDING",
        "QUEUED",
        "RUNNING",
        "WAITING",
        "RETRYING",
        "SUCCEEDED",
        "SUCCEEDED_WITH_WARNINGS",
        "BLOCKED",
        "FAILED",
        "CANCELLED",
        "SKIPPED",
    ]


def test_module_health_states_exact_set() -> None:
    """Spec doc 02 §9."""
    assert _values(ModuleHealthState) == [
        "HEALTHY",
        "DEGRADED",
        "UNAVAILABLE",
        "MISCONFIGURED",
        "FAILED",
        "UNKNOWN",
    ]


def test_data_quality_states_exact_set() -> None:
    """Spec doc 02 §10 — separate from execution states."""
    assert _values(DataQualityState) == ["GOOD", "DEGRADED", "POOR", "UNKNOWN"]


def test_resource_tiers_exact_set_and_order() -> None:
    """Spec doc 02 §11."""
    assert _values(ResourceTier) == ["T0_INDEX", "T1_SCAN", "T2_FULL", "T3_DEEP"]
    assert list(TIER_ORDER) == list(ResourceTier)


def test_claim_types_exact_set() -> None:
    """Spec doc 01 §11."""
    assert _values(ClaimType) == ["FACT", "INFERENCE", "CRITIQUE", "EXTERNAL"]


def test_support_states_exact_set() -> None:
    """Spec doc 01 §11."""
    assert _values(SupportState) == [
        "UNVERIFIED",
        "SUPPORTED",
        "PARTIALLY_SUPPORTED",
        "DISPUTED",
        "UNSUPPORTED",
        "RETRACTED",
        "INSUFFICIENT_EVIDENCE",
    ]


def test_evidence_types_exact_set() -> None:
    """Spec doc 03 §1.4."""
    assert _values(EvidenceType) == [
        "PARAGRAPH",
        "HEADING",
        "CAPTION",
        "TABLE",
        "TABLE_CELL",
        "FIGURE",
        "FIGURE_REGION",
        "EQUATION",
        "REFERENCE",
        "FOOTNOTE",
        "METADATA",
    ]


def test_evidence_roles_exact_set() -> None:
    """Spec doc 03 §1.5."""
    assert _values(EvidenceRole) == ["SUPPORT", "COUNTER_EVIDENCE", "CONTEXT"]


def test_verifier_verdicts_exact_set() -> None:
    """Spec doc 03 §1.7."""
    assert _values(VerifierVerdict) == ["PASS", "WARN", "FAIL", "INCONCLUSIVE"]


def test_verifier_modules_exact_set() -> None:
    """Spec doc 01 §10."""
    assert _values(VerifierType) == [
        "verification.citation",
        "verification.evidence_existence",
        "verification.numeric",
        "verification.claim_scope",
        "verification.contradiction",
        "verification.independent_consensus",
        "verification.falsification",
        "verification.ocr_sensitivity",
        "verification.external_novelty",
    ]


def test_agent_statuses_exact_set() -> None:
    """Spec doc 03 §2."""
    assert _values(AgentStatus) == [
        "SUCCESS",
        "SUCCESS_WITH_WARNINGS",
        "INSUFFICIENT_EVIDENCE",
        "FAILED",
    ]


def test_section_classes_exact_set() -> None:
    """Spec doc 01 §8."""
    assert _values(SectionClass) == [
        "TITLE",
        "ABSTRACT",
        "INTRODUCTION",
        "RELATED_WORK",
        "BACKGROUND",
        "METHOD",
        "THEORY",
        "EXPERIMENT",
        "RESULT",
        "DISCUSSION",
        "LIMITATION",
        "CONCLUSION",
        "ACKNOWLEDGEMENT",
        "REFERENCES",
        "APPENDIX",
        "SUPPLEMENTARY",
        "OTHER",
    ]


def test_page_modes_exact_set() -> None:
    """Spec doc 01 §7."""
    assert _values(PageMode) == [
        "NATIVE_TEXT",
        "SCANNED",
        "MIXED",
        "IMAGE_HEAVY",
        "BROKEN_TEXT_LAYER",
    ]


def test_entity_types_exact_set() -> None:
    """Spec doc 01 §16."""
    assert _values(EntityType) == [
        "PAPER",
        "PAPER_VERSION",
        "AUTHOR",
        "AFFILIATION",
        "VENUE",
        "METHOD",
        "DATASET",
        "METRIC",
        "TECHNIQUE",
        "TASK",
        "DOMAIN",
        "PROJECT",
        "CODE_REPOSITORY",
        "CLAIM",
    ]


def test_retention_classes_exact_set() -> None:
    """Spec doc 07 §4."""
    assert _values(RetentionClass) == ["KEEP", "CACHE", "TEMP", "DEBUG_TTL"]


def test_quality_dimensions_exact_set() -> None:
    """Spec doc 01 §12."""
    assert _values(QualityDimension) == [
        "methodological_rigor",
        "experimental_rigor",
        "baseline_fairness",
        "ablation_completeness",
        "statistical_reliability",
        "reproducibility",
        "data_quality",
        "claim_evidence_alignment",
        "novelty",
        "engineering_usefulness",
        "transferability",
        "clarity",
    ]


def test_synthesis_classes_exact_set() -> None:
    """Spec doc 07 §11."""
    assert _values(SynthesisSupportClass) == [
        "DIRECTLY_SUPPORTED",
        "SUPPORTED_INFERENCE",
        "DISPUTED",
        "UNCERTAIN",
        "INSUFFICIENT_EVIDENCE",
    ]


def test_provider_families_exact_set() -> None:
    """Spec doc 02 §5."""
    assert _values(ProviderFamily) == [
        "LLMProvider",
        "OCRProvider",
        "EmbeddingProvider",
        "MetadataProvider",
        "ExternalSearchProvider",
        "StorageProvider",
    ]


def test_tag_namespaces_exact_set() -> None:
    """Spec doc 01 §15."""
    assert TAG_NAMESPACES == (
        "domain/",
        "task/",
        "modality/",
        "method/",
        "architecture/",
        "training/",
        "inference/",
        "dataset/",
        "metric/",
        "experimental-trick/",
        "hardware/",
        "efficiency/",
        "novelty/",
        "reliability-risk/",
        "venue/",
        "author/",
        "affiliation/",
        "application/",
        "project/",
    )


def test_llm_mock_modes_exact_set() -> None:
    """Spec doc 06 §3."""
    assert _values(LlmMockMode) == [
        "VALID",
        "NON_JSON",
        "MISSING_REQUIRED_FIELD",
        "UNKNOWN_EVIDENCE",
        "WRONG_PAPER_EVIDENCE",
        "INVENTED_NUMBER",
        "DUPLICATE_CLAIMS",
        "TAG_SPAM",
        "REFUSAL",
        "TIMEOUT",
        "RATE_LIMIT",
        "SERVER_ERROR",
    ]


def test_ocr_mock_modes_exact_set() -> None:
    """Spec doc 06 §3."""
    assert _values(OcrMockMode) == [
        "VALID",
        "LOW_CONFIDENCE",
        "DIGIT_CORRUPTION",
        "EMPTY",
        "TIMEOUT",
        "MALFORMED_RESPONSE",
    ]

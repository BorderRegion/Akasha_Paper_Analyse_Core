"""Frozen enumerations (spec docs 01/02/03/07).

Values here mirror the FROZEN specification exactly. Changing an existing
value or removing one requires a spec major-version bump (spec doc 02 §13).
Adding members to explicitly non-exhaustive enums (marked below) is allowed.
"""

from __future__ import annotations

from enum import StrEnum

# ---------------------------------------------------------------------------
# Pipeline / execution
# ---------------------------------------------------------------------------


class PipelineStage(StrEnum):
    """Frozen paper pipeline progression (spec doc 02 §7, doc 03 §8)."""

    IMPORTED = "IMPORTED"
    FINGERPRINTED = "FINGERPRINTED"
    METADATA_RESOLVED = "METADATA_RESOLVED"
    PDF_INSPECTED = "PDF_INSPECTED"
    EXTRACTED = "EXTRACTED"
    STRUCTURED = "STRUCTURED"
    EVIDENCE_INDEXED = "EVIDENCE_INDEXED"
    TRIAGED = "TRIAGED"
    ANALYZED = "ANALYZED"
    VERIFIED = "VERIFIED"
    SYNTHESIZED = "SYNTHESIZED"
    LINKED = "LINKED"
    SEARCH_INDEXED = "SEARCH_INDEXED"
    CORPUS_READY = "CORPUS_READY"


PIPELINE_STAGE_ORDER: tuple[PipelineStage, ...] = tuple(PipelineStage)


def pipeline_stage_index(stage: PipelineStage) -> int:
    return PIPELINE_STAGE_ORDER.index(stage)


class TaskState(StrEnum):
    """Frozen task execution states (spec doc 02 §8)."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_WITH_WARNINGS = "SUCCEEDED_WITH_WARNINGS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


#: Task transition table. Contains every transition listed in spec doc 03 §9
#: (the spec list is explicitly "valid examples") plus the minimal additions
#: required by mandated behavior elsewhere in the spec:
#: - RUNNING -> BLOCKED, BLOCKED -> QUEUED/FAILED/CANCELLED  (state BLOCKED is
#:   frozen in doc 02 §8 and must be enterable/exitable; doc 04 §14 exposes
#:   blocked tasks),
#: - RETRYING -> FAILED  (doc 05 P05 "retry exhaustion"),
#: - WAITING -> FAILED  (doc 04 §2.6 replay/cancel semantics for waiting work).
TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.QUEUED, TaskState.CANCELLED, TaskState.SKIPPED}),
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.CANCELLED}),
    TaskState.RUNNING: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.SUCCEEDED_WITH_WARNINGS,
            TaskState.RETRYING,
            TaskState.WAITING,
            TaskState.BLOCKED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING: frozenset({TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED}),
    TaskState.RETRYING: frozenset({TaskState.QUEUED, TaskState.FAILED}),
    TaskState.BLOCKED: frozenset({TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED}),
    # Terminal states.
    TaskState.SUCCEEDED: frozenset(),
    TaskState.SUCCEEDED_WITH_WARNINGS: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.SKIPPED: frozenset(),
}

TERMINAL_TASK_STATES: frozenset[TaskState] = frozenset(
    state for state, targets in TASK_TRANSITIONS.items() if not targets
)


def is_valid_task_transition(from_state: TaskState, to_state: TaskState) -> bool:
    return to_state in TASK_TRANSITIONS[from_state]


def validate_task_transition(
    from_state: TaskState, to_state: TaskState, *, machine: str = "task"
) -> None:
    """Enforce the task transition table.

    Spec doc 03 §9: invalid transitions must raise a domain error.
    """
    from paperintel.errors.exceptions import InvalidStateTransitionError  # noqa: PLC0415

    if not is_valid_task_transition(from_state, to_state):
        raise InvalidStateTransitionError(from_state.value, to_state.value, machine=machine)


class ResourceTier(StrEnum):
    """Frozen resource tiers (spec doc 02 §11, doc 07 §2)."""

    T0_INDEX = "T0_INDEX"
    T1_SCAN = "T1_SCAN"
    T2_FULL = "T2_FULL"
    T3_DEEP = "T3_DEEP"


TIER_ORDER: tuple[ResourceTier, ...] = (
    ResourceTier.T0_INDEX,
    ResourceTier.T1_SCAN,
    ResourceTier.T2_FULL,
    ResourceTier.T3_DEEP,
)


class RetentionClass(StrEnum):
    """Frozen disk retention classes (spec doc 07 §4)."""

    KEEP = "KEEP"
    CACHE = "CACHE"
    TEMP = "TEMP"
    DEBUG_TTL = "DEBUG_TTL"


# ---------------------------------------------------------------------------
# Health / quality
# ---------------------------------------------------------------------------


class ModuleHealthState(StrEnum):
    """Frozen module health states (spec doc 02 §9)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    MISCONFIGURED = "MISCONFIGURED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DataQualityState(StrEnum):
    """Frozen data quality states (spec doc 02 §10).

    Execution state and data quality MUST NOT be merged.
    """

    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    POOR = "POOR"
    UNKNOWN = "UNKNOWN"


class SanityOutcome(StrEnum):
    """Sanity check outcomes (spec doc 06 §5)."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class CanaryState(StrEnum):
    """Provider canary states (spec doc 06 §10)."""

    UNKNOWN = "UNKNOWN"
    OK = "OK"
    FAILED = "FAILED"
    STALE = "STALE"


class CircuitState(StrEnum):
    """Circuit breaker states (spec doc 05 P02)."""

    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


# ---------------------------------------------------------------------------
# Evidence / claims / verification
# ---------------------------------------------------------------------------


class EvidenceType(StrEnum):
    """Typical evidence types (spec doc 03 §1.4; list is explicitly typical,
    additions require a documented contract-compatible change)."""

    PARAGRAPH = "PARAGRAPH"
    HEADING = "HEADING"
    CAPTION = "CAPTION"
    TABLE = "TABLE"
    TABLE_CELL = "TABLE_CELL"
    FIGURE = "FIGURE"
    FIGURE_REGION = "FIGURE_REGION"
    EQUATION = "EQUATION"
    REFERENCE = "REFERENCE"
    FOOTNOTE = "FOOTNOTE"
    METADATA = "METADATA"


class SourceMethod(StrEnum):
    """How evidence content was obtained (spec doc 01 §7 provenance).

    Not value-frozen by the spec; semantics are: native text layer, OCR,
    merged native+OCR, or provider/import metadata.
    """

    PDF_NATIVE = "PDF_NATIVE"
    OCR = "OCR"
    MIXED_NATIVE_OCR = "MIXED_NATIVE_OCR"
    IMPORTED_METADATA = "IMPORTED_METADATA"


class PageMode(StrEnum):
    """Frozen page classification modes (spec doc 01 §7)."""

    NATIVE_TEXT = "NATIVE_TEXT"
    SCANNED = "SCANNED"
    MIXED = "MIXED"
    IMAGE_HEAVY = "IMAGE_HEAVY"
    BROKEN_TEXT_LAYER = "BROKEN_TEXT_LAYER"


class ClaimType(StrEnum):
    """Frozen claim taxonomy (spec doc 01 §11, doc 02 §1)."""

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    CRITIQUE = "CRITIQUE"
    EXTERNAL = "EXTERNAL"


class SupportState(StrEnum):
    """Frozen claim support states (spec doc 01 §11)."""

    UNVERIFIED = "UNVERIFIED"
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    DISPUTED = "DISPUTED"
    UNSUPPORTED = "UNSUPPORTED"
    RETRACTED = "RETRACTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EvidenceRole(StrEnum):
    """Frozen claim-evidence roles (spec doc 03 §1.5)."""

    SUPPORT = "SUPPORT"
    COUNTER_EVIDENCE = "COUNTER_EVIDENCE"
    CONTEXT = "CONTEXT"


class VerifierVerdict(StrEnum):
    """Frozen verifier verdicts (spec doc 03 §1.7)."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class VerifierType(StrEnum):
    """Frozen verifier modules (spec doc 01 §10)."""

    CITATION = "verification.citation"
    EVIDENCE_EXISTENCE = "verification.evidence_existence"
    NUMERIC = "verification.numeric"
    CLAIM_SCOPE = "verification.claim_scope"
    CONTRADICTION = "verification.contradiction"
    INDEPENDENT_CONSENSUS = "verification.independent_consensus"
    FALSIFICATION = "verification.falsification"
    OCR_SENSITIVITY = "verification.ocr_sensitivity"
    EXTERNAL_NOVELTY = "verification.external_novelty"


class SynthesisSupportClass(StrEnum):
    """Final synthesis policy classes (spec doc 07 §11)."""

    DIRECTLY_SUPPORTED = "DIRECTLY_SUPPORTED"
    SUPPORTED_INFERENCE = "SUPPORTED_INFERENCE"
    DISPUTED = "DISPUTED"
    UNCERTAIN = "UNCERTAIN"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class QualityDimension(StrEnum):
    """Frozen quality vector dimensions (spec doc 01 §12)."""

    METHODOLOGICAL_RIGOR = "methodological_rigor"
    EXPERIMENTAL_RIGOR = "experimental_rigor"
    BASELINE_FAIRNESS = "baseline_fairness"
    ABLATION_COMPLETENESS = "ablation_completeness"
    STATISTICAL_RELIABILITY = "statistical_reliability"
    REPRODUCIBILITY = "reproducibility"
    DATA_QUALITY = "data_quality"
    CLAIM_EVIDENCE_ALIGNMENT = "claim_evidence_alignment"
    NOVELTY = "novelty"
    ENGINEERING_USEFULNESS = "engineering_usefulness"
    TRANSFERABILITY = "transferability"
    CLARITY = "clarity"


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


class SectionClass(StrEnum):
    """Canonical normalized top-level section classes (spec doc 01 §8)."""

    TITLE = "TITLE"
    ABSTRACT = "ABSTRACT"
    INTRODUCTION = "INTRODUCTION"
    RELATED_WORK = "RELATED_WORK"
    BACKGROUND = "BACKGROUND"
    METHOD = "METHOD"
    THEORY = "THEORY"
    EXPERIMENT = "EXPERIMENT"
    RESULT = "RESULT"
    DISCUSSION = "DISCUSSION"
    LIMITATION = "LIMITATION"
    CONCLUSION = "CONCLUSION"
    ACKNOWLEDGEMENT = "ACKNOWLEDGEMENT"
    REFERENCES = "REFERENCES"
    APPENDIX = "APPENDIX"
    SUPPLEMENTARY = "SUPPLEMENTARY"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class AgentStatus(StrEnum):
    """Frozen agent result statuses (spec doc 03 §2).

    INSUFFICIENT_EVIDENCE is a valid analytical result, not a software failure.
    """

    SUCCESS = "SUCCESS"
    SUCCESS_WITH_WARNINGS = "SUCCESS_WITH_WARNINGS"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    FAILED = "FAILED"


class ModelRole(StrEnum):
    """Configured model roles (spec templates/provider_config.example.yaml)."""

    ANALYST = "analyst"
    VERIFIER = "verifier"
    SYNTHESIZER = "synthesizer"


# ---------------------------------------------------------------------------
# Graph / entities
# ---------------------------------------------------------------------------


class EntityType(StrEnum):
    """Frozen required entity types (spec doc 01 §16)."""

    PAPER = "PAPER"
    PAPER_VERSION = "PAPER_VERSION"
    AUTHOR = "AUTHOR"
    AFFILIATION = "AFFILIATION"
    VENUE = "VENUE"
    METHOD = "METHOD"
    DATASET = "DATASET"
    METRIC = "METRIC"
    TECHNIQUE = "TECHNIQUE"
    TASK = "TASK"
    DOMAIN = "DOMAIN"
    PROJECT = "PROJECT"
    CODE_REPOSITORY = "CODE_REPOSITORY"
    CLAIM = "CLAIM"


class RelationType(StrEnum):
    """Example relation types (spec doc 01 §16 — framework is generic and this
    list is explicitly examples; relation_type fields accept plain strings)."""

    AUTHORED_BY = "AUTHORED_BY"
    AFFILIATED_WITH = "AFFILIATED_WITH"
    USES_METHOD = "USES_METHOD"
    EXTENDS_METHOD = "EXTENDS_METHOD"
    COMPARES_WITH = "COMPARES_WITH"
    USES_DATASET = "USES_DATASET"
    REPORTS_METRIC = "REPORTS_METRIC"
    USES_TECHNIQUE = "USES_TECHNIQUE"
    CITES = "CITES"
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    RELATED_TO = "RELATED_TO"


#: Canonical tag namespace families (spec doc 01 §15).
TAG_NAMESPACES: tuple[str, ...] = (
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


# ---------------------------------------------------------------------------
# Providers / assets
# ---------------------------------------------------------------------------


class ProviderFamily(StrEnum):
    """Frozen provider families (spec doc 02 §5)."""

    LLM = "LLMProvider"
    OCR = "OCRProvider"
    EMBEDDING = "EmbeddingProvider"
    METADATA = "MetadataProvider"
    EXTERNAL_SEARCH = "ExternalSearchProvider"
    STORAGE = "StorageProvider"


class ProviderKind(StrEnum):
    """Concrete provider implementations (non-exhaustive; new adapters that
    implement an existing contract are allowed changes, spec doc 02 §13)."""

    OPENAI_COMPATIBLE = "openai_compatible"
    PADDLE_HTTP = "paddle_http"
    HTTP_EMBEDDING = "http_embedding"
    CROSSREF = "crossref"
    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    LOCAL_OBJECT_STORE = "local_object_store"
    MOCK_LLM = "mock_llm"
    MOCK_OCR = "mock_ocr"
    MOCK_EMBEDDING = "mock_embedding"
    MOCK_METADATA = "mock_metadata"


class AssetKind(StrEnum):
    """Object store asset kinds (spec doc 01 §6.2)."""

    PDF = "pdf"
    FIGURE = "figures"
    TABLE_IMAGE = "table_images"
    EVIDENCE_CROP = "evidence_crops"
    LLM_OUTPUT = "llm_outputs"
    DEBUG = "debug"


class TransportStatus(StrEnum):
    """Model-call transport outcome classification (spec doc 03 §1.8)."""

    OK = "OK"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    HTTP_CLIENT_ERROR = "HTTP_CLIENT_ERROR"
    HTTP_SERVER_ERROR = "HTTP_SERVER_ERROR"
    CONNECTION_ERROR = "CONNECTION_ERROR"
    UNKNOWN = "UNKNOWN"


class SchemaStatus(StrEnum):
    """Schema-firewall outcome for a model response (spec doc 02 §6)."""

    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    PASSED_AFTER_REPAIR = "PASSED_AFTER_REPAIR"
    JSON_INVALID = "JSON_INVALID"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    SEMANTIC_INVALID = "SEMANTIC_INVALID"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"


# ---------------------------------------------------------------------------
# Mock provider modes (spec doc 06 §3)
# ---------------------------------------------------------------------------


class LlmMockMode(StrEnum):
    """Required deterministic LLM mock modes (spec doc 06 §3)."""

    VALID = "VALID"
    NON_JSON = "NON_JSON"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"
    WRONG_PAPER_EVIDENCE = "WRONG_PAPER_EVIDENCE"
    INVENTED_NUMBER = "INVENTED_NUMBER"
    DUPLICATE_CLAIMS = "DUPLICATE_CLAIMS"
    TAG_SPAM = "TAG_SPAM"
    REFUSAL = "REFUSAL"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    SERVER_ERROR = "SERVER_ERROR"


class OcrMockMode(StrEnum):
    """Required OCR mock modes (spec doc 06 §3)."""

    VALID = "VALID"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    DIGIT_CORRUPTION = "DIGIT_CORRUPTION"
    EMPTY = "EMPTY"
    TIMEOUT = "TIMEOUT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class GateResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class CheckResult(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    #: SKIP is only legal together with a warning entry explaining why the
    #: check did not run (e.g. optional real-provider credentials absent).
    SKIP = "SKIP"


__all__ = [n for n in dir() if not n.startswith("_")]
